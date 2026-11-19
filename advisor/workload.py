"""Workloads (workload.yaml) and data profiles (data.yaml): the advisor's inputs besides hw.json.

workload.yaml:
    name: dashboard_mix
    queries:
      - {name: q1, weight: 0.6, op: sum, cols: [c0, c1, c2, c3], where: "c0 < 5"}
      - {weight: 0.2, op: materialize, cols: all, where: "c0 < 1 AND c12 < 50"}
      - {weight: 0.2, op: lookup, cols: all, lookups: 10000, dist: uniform}
      - {weight: 0.1, op: sum, cols: "0-3", where: "c0 < 5", rates: [0.04]}   # known pass rate

  ops: sum | materialize | lookup.  cols: all | [c0, c5] | "0-3" | "0,4,8".
  where: conjunction of  cN < v, cN <= v, cN > v, cN >= v, cN = v, cN = a..b, cN BETWEEN a AND b,
  joined by AND (or ','). Weights are relative frequencies.

data.yaml:
    rows: 8_000_000            # or  size: 4G
    cols: 64
    default: {dist: uniform, min: 0, max: 99}
    columns:                   # overrides; a mapping or a list with name:
      c0: {dist: uniform, min: 0, max: 999}
      c1: {dist: clustered, run: 1024}
      c2: {dist: uniform, hist: {edges: [0, 10, 100], counts: [900, 100]}}   # optional histogram
"""
import re
from dataclasses import dataclass, field

import yaml

from .engine import parse_size
from .models.base import DataInfo

INF = float("inf")


@dataclass
class Predicate:
    """Closed range lo <= value <= hi (as the engine's predicates)."""
    col: int
    lo: float
    hi: float
    text: str = ""


@dataclass
class WorkloadQuery:
    name: str
    weight: float
    op: str
    cols: list
    preds: list = field(default_factory=list)   # [Predicate] as written
    lookups: int = 0
    id_dist: str = "uniform"
    rates: list = None                          # optional known pass rates per predicate

    def describe(self):
        if self.op == "lookup":
            return f"lookup {self.lookups} x {_cols_text(self.cols)} ({self.id_dist})"
        w = " AND ".join(p.text for p in self.preds) or "-"
        return f"{self.op} {_cols_text(self.cols)} WHERE {w}"


def _cols_text(cols):
    if len(cols) > 6:
        return f"{len(cols)} cols"
    return ",".join(f"c{c}" for c in cols)


@dataclass
class Workload:
    name: str
    queries: list

    @property
    def total_weight(self):
        return sum(q.weight for q in self.queries)


# --------------------------------------------------------------------------- parsing

def parse_cols(spec, ncols):
    if spec is None or spec == "all":
        return list(range(ncols))
    items = spec if isinstance(spec, list) else str(spec).split(",")
    out = []
    for it in items:
        it = str(it).strip().lstrip("c")
        if "-" in it:
            a, b = it.split("-")
            out.extend(range(int(a.lstrip("c")), int(b.lstrip("c")) + 1))
        else:
            out.append(int(it))
    for c in out:
        if not 0 <= c < ncols:
            raise ValueError(f"column c{c} outside c0..c{ncols - 1}")
    return out


_TERM = re.compile(r"^\s*c(\d+)\s*(<=|>=|<|>|=)\s*(-?\d+)(?:\s*\.\.\s*(-?\d+))?\s*$")
_BETWEEN = re.compile(r"^\s*c(\d+)\s+between\s+(-?\d+)\s+and\s+(-?\d+)\s*$", re.I)


def parse_where(text, ncols):
    if text is None or str(text).strip().lower() in ("", "none"):
        return []
    s = re.sub(r"(c\d+\s+between\s+-?\d+)\s+and\s+", r"\1 & ", str(text), flags=re.I)
    terms = [t for t in re.split(r"\s+and\s+|,", s, flags=re.I) if t.strip()]
    preds = []
    for t in terms:
        t = t.replace(" & ", " and ")
        m = _BETWEEN.match(t)
        if m:
            c, lo, hi = int(m[1]), int(m[2]), int(m[3])
        else:
            m = _TERM.match(t)
            if not m:
                raise ValueError(f"cannot parse predicate '{t.strip()}'")
            c, op, v = int(m[1]), m[2], int(m[3])
            lo, hi = {"<": (-INF, v - 1), "<=": (-INF, v), ">": (v + 1, INF), ">=": (v, INF),
                      "=": (v, int(m[4]) if m[4] else v)}[op]
        if not 0 <= c < ncols:
            raise ValueError(f"predicate column c{c} outside c0..c{ncols - 1}")
        preds.append(Predicate(c, lo, hi, re.sub(r"\s+", " ", t.strip())))
    return preds


def query_from_dict(d, ncols, idx=0):
    op = d.get("op", "sum")
    if op not in ("sum", "materialize", "lookup"):
        raise ValueError(f"unknown op '{op}'")
    q = WorkloadQuery(str(d.get("name", f"q{idx + 1}")), float(d.get("weight", 1.0)), op,
                      parse_cols(d.get("cols", "all"), ncols),
                      parse_where(d.get("where"), ncols), int(d.get("lookups", 0)),
                      str(d.get("dist", "uniform")), d.get("rates"))
    if op == "lookup" and (q.preds or q.lookups < 1):
        raise ValueError(f"{q.name}: a lookup needs lookups >= 1 and no where")
    if q.rates is not None and len(q.rates) != len(q.preds):
        raise ValueError(f"{q.name}: one rate per predicate expected")
    return q


def load_workload(path, ncols):
    d = yaml.safe_load(open(path))
    return Workload(d.get("name", str(path)),
                    [query_from_dict(q, ncols, i) for i, q in enumerate(d["queries"])])


def parse_engine_query(text, ncols):
    """The engine's --query syntax: op=sum;cols=0-3;where=c0<5,c12<50 (or op=lookup;n=..)."""
    kv = dict(p.split("=", 1) for p in text.split(";") if p)
    d = {"op": kv.get("op", "sum"), "cols": kv.get("cols", "all"), "where": kv.get("where"),
         "lookups": kv.get("n", 0), "dist": kv.get("dist", "uniform"),
         "name": kv.get("label", "q1")}
    return query_from_dict(d, ncols)


def _dist_spec(d):
    name = d.get("dist", "uniform")
    parts = [name]
    if "min" in d or "lo" in d:
        parts.append(f"lo={int(d.get('min', d.get('lo')))}")
    if "max" in d or "hi" in d:
        parts.append(f"hi={int(d.get('max', d.get('hi')))}")
    for k, v in d.items():
        if k not in ("dist", "min", "max", "lo", "hi", "name", "width", "hist", "sorted"):
            parts.append(f"{k}={v}")
    if d.get("sorted") and name == "uniform":
        parts[0] = "sorted"
    return ":".join(parts)


def load_data_profile(path):
    """data.yaml -> (DataInfo, {col: histogram dict})."""
    d = yaml.safe_load(open(path))
    cols = int(d["cols"]) if "cols" in d else len(d["columns"])
    rows = int(d["rows"]) if "rows" in d else parse_size(d["size"]) // (cols * 8)
    dist = {"default": _dist_spec(d.get("default", {}))}
    hists = {}
    columns = d.get("columns") or {}
    items = columns.items() if isinstance(columns, dict) else (
        (c.get("name", f"c{i}"), c) for i, c in enumerate(columns))
    for name, spec in items:
        c = int(str(name).lstrip("c"))
        if int(spec.get("width", 8)) != 8:
            raise ValueError("only 8-byte columns are supported by the engine")
        dist[f"c{c}"] = _dist_spec(spec)
        if "hist" in spec:
            hists[c] = spec["hist"]
    if d.get("correlations"):
        print("note: correlations are not modelled yet; predicates are assumed independent")
    return DataInfo(rows, cols, dist), hists
