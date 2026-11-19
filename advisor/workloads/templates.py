"""Query templates: the queries the ground truth measures and random workloads are drawn from.

A template file (configs/templates/*.yaml) lists named projections and *families*. Each family
has a `type` naming a registered family class that expands it, for one data set, into concrete
Templates. Add a family type with @FAMILIES.register("name") and a class with expand().

Predicate constants are derived from target pass rates through the data set's distribution
models (data_profile.py), so the same template file works for any table width or value domain.
Predicate order is an *execution* choice (space file, exec.predicate_order): a multi-predicate
query is expanded once per order, with the same logical_id.
"""
import itertools
from dataclasses import asdict, dataclass

import yaml

from ..data_profile import make_model
from ..registry import Registry

FAMILIES = Registry("template family")


@dataclass
class Template:
    id: str                 # unique per data set; also the engine's query label
    logical_id: str         # the query regardless of predicate order
    family: str
    op: str                 # sum | materialize | lookup
    projection: str         # projection name from the template file
    cols: str               # engine column list, e.g. "0-3", "0,4,8", "all"
    n_project: int
    where: str = ""         # engine conjunction, e.g. "c0<10,c1<50"; "" = no filter
    n_predicates: int = 0
    pred_rates: str = ""    # target pass rate of each predicate, in evaluation order
    target_selectivity: float = 1.0
    order: str = "-"        # predicate order (execution option) or "-"
    lookups: int = 0
    id_dist: str = "-"

    def engine_query(self, seed=1):
        parts = [f"op={self.op}", f"cols={self.cols}", f"label={self.id}"]
        if self.op == "lookup":
            parts += [f"n={self.lookups}", f"dist={self.id_dist}", f"seed={seed}"]
        else:
            parts.append(f"where={self.where or 'none'}")
        return ";".join(parts)

    def row(self):
        return asdict(self)


# --------------------------------------------------------------------------- projections

def resolve_projection(defn, table_cols):
    """A projection definition -> (engine cols string, number of columns). Definitions:
    {prefix: k}, {prefix_frac: f}, {all: true}, {stride: s} (c0, cs, c2s, ...), {cols: "0,4"}."""
    if defn.get("all"):
        return "all", table_cols
    if "prefix" in defn or "prefix_frac" in defn:
        k = defn["prefix"] if "prefix" in defn else round(defn["prefix_frac"] * table_cols)
        k = max(1, min(int(k), table_cols))
        return (f"0-{k - 1}" if k > 1 else "0"), k
    if "stride" in defn:
        cols = list(range(0, table_cols, int(defn["stride"])))
        return ",".join(map(str, cols)), len(cols)
    if "cols" in defn:
        cols = str(defn["cols"])
        return cols, len(cols.split(","))
    raise ValueError(f"bad projection definition {defn}")


# --------------------------------------------------------------------------- families

class Family:
    def __init__(self, name, spec, projections):
        self.name = name
        self.spec = spec
        self.projections = projections  # name -> definition

    def project(self, data):
        for pname in self.spec["projections"]:
            cols, k = resolve_projection(self.projections[pname], data.cols)
            yield pname, cols, k

    def expand(self, data, orders):
        raise NotImplementedError


@FAMILIES.register("scan")
class ScanFamily(Family):
    """No filter: every row is processed."""

    def expand(self, data, orders):
        for op in self.spec["ops"]:
            for pname, cols, k in self.project(data):
                tid = f"{self.name}/{op}/{pname}"
                yield Template(tid, tid, self.name, op, pname, cols, k)


@FAMILIES.register("filter")
class FilterFamily(Family):
    """Conjunctions of `value < K` predicates; spec.predicates = [{col, rates: [...]}, ...]."""

    def expand(self, data, orders):
        preds = self.spec["predicates"]
        for p in preds:
            if p["col"] >= data.cols:
                raise ValueError(f"family {self.name}: c{p['col']} beyond {data.cols} columns")
        models = {p["col"]: make_model(data.column_spec(p["col"]), data.rows) for p in preds}
        for op in self.spec["ops"]:
            for pname, cols, k in self.project(data):
                for rates in itertools.product(*[p["rates"] for p in preds]):
                    terms = [(p["col"], r) for p, r in zip(preds, rates)]
                    logical = f"{self.name}/{op}/{pname}/" + "&".join(
                        f"c{c}:{r:g}" for c, r in terms)
                    sel = 1.0
                    for c, r in terms:
                        sel *= models[c].pass_rate(models[c].lo, self._threshold(models[c], r) - 1)
                    seen = set()
                    for order in (orders if len(terms) > 1 else ["-"]):
                        ordered = self._order(terms, order)
                        where = ",".join(f"c{c}<{self._threshold(models[c], r)}"
                                         for c, r in ordered)
                        if where in seen:  # e.g. equal rates: both orders are the same query
                            continue
                        seen.add(where)
                        tid = logical if order == "-" else f"{logical}/{order}"
                        yield Template(tid, logical, self.name, op, pname, cols, k, where,
                                       len(terms), ";".join(f"{r:g}" for _, r in ordered),
                                       sel, order)

    @staticmethod
    def _threshold(model, rate):
        t = model.threshold(rate)
        return max(t, model.lo + 1) if rate > 0 else t

    @staticmethod
    def _order(terms, order):
        if order in ("-", "as_written"):
            return list(terms)
        if order == "selective_first":
            return sorted(terms, key=lambda t: t[1])
        if order == "selective_last":
            return sorted(terms, key=lambda t: -t[1])
        raise ValueError(f"unknown predicate order '{order}'")


@FAMILIES.register("lookup")
class LookupFamily(Family):
    """Point lookups: spec.counts (ids per query) x spec.id_dists (engine row-id dists)."""

    def expand(self, data, orders):
        for pname, cols, k in self.project(data):
            for n in self.spec["counts"]:
                for dist in self.spec["id_dists"]:
                    tid = f"{self.name}/lookup/{pname}/n={n}/{dist}"
                    yield Template(tid, tid, self.name, "lookup", pname, cols, k,
                                   target_selectivity=n / data.rows, lookups=int(n),
                                   id_dist=dist)


# --------------------------------------------------------------------------- loading

def load_template_file(path):
    with open(path) as f:
        return yaml.safe_load(f)


def expand_templates(tfile, data, orders, families=None):
    """All templates of the file (or of the named families) for data set `data`."""
    projections = tfile["projections"]
    out = []
    for spec in tfile["families"]:
        if families and spec["name"] not in families:
            continue
        cls = FAMILIES.get(spec["type"])
        out.extend(cls(spec["name"], spec, projections).expand(data, orders))
    ids = [t.id for t in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate template ids; family names must be unique")
    for t in out:
        if "," in t.id:
            raise ValueError(f"template id '{t.id}' contains a comma")
    return out
