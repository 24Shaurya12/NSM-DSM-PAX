"""Search interface: Search.run(space, workload, model, ctx) -> RankedResults.

Storage parameters are chosen once for the table; execution options (DSM batch size, filter
mode, predicate order) per query: for each storage configuration every query gets its best
options, and storage configurations are ranked by the weighted cost of the workload.
Add a strategy with @register_search("name") on a Search subclass.
"""
import itertools
from dataclasses import dataclass, field

from ..candidates import exec_grid, predicate_orders, storage_configs
from ..engine import parse_size
from ..models.base import ExecInfo, QueryInfo, StorageInfo
from ..registry import Registry

SEARCHES = Registry("search strategy")
register_search = SEARCHES.register


@dataclass
class SearchContext:
    data: object                  # models.base.DataInfo
    estimator: object             # selectivity.Estimator
    objective: str = "mean"       # mean: expected time per query; total: sum of weighted times


@dataclass
class QueryChoice:
    query: object                 # workload.WorkloadQuery
    exec: dict                    # {"dsm_batch":, "filter":, "order":} (only those that apply)
    cost: object                  # models.base.CostBreakdown
    rates: list                   # pass rates in evaluation order


@dataclass
class StorageResult:
    storage: object               # engine.StorageConfig
    cost_ns: float                # workload objective
    choices: list = field(default_factory=list)


@dataclass
class RankedResults:
    results: list                 # StorageResult, best first
    objective: str

    @property
    def best(self):
        return self.results[0]


def storage_info(s):
    page = s.param("page")
    return StorageInfo(s.layout, parse_size(page) if page else 0)


def orders_for(query, rates, names):
    """[(order name, [(col, rate)] in evaluation order)] for the requested order options."""
    terms = [(p.col, r) for p, r in zip(query.preds, rates)]
    if len(terms) < 2:
        return [("-", terms)]
    out, seen = [], set()
    for name in names:
        if name == "as_written":
            cands = [("as_written", terms)]
        elif name == "selective_first":
            cands = [(name, sorted(terms, key=lambda t: t[1]))]
        elif name == "selective_last":
            cands = [(name, sorted(terms, key=lambda t: -t[1]))]
        elif name == "all" and len(terms) <= 4:
            cands = [("perm" + "".join(str(i) for i in idx), [terms[i] for i in idx])
                     for idx in itertools.permutations(range(len(terms)))]
        else:
            raise ValueError(f"unknown predicate order '{name}'")
        for n, t in cands:
            key = tuple(t)
            if key not in seen:
                seen.add(key)
                out.append((n, t))
    return out


def best_choice(model, storage, query, ctx, space):
    """The query's best execution options on this storage configuration."""
    st = storage_info(storage)
    rates = ctx.estimator.rates(query)
    grid = exec_grid(space)
    batches = grid.dsm_batch if (st.layout == "dsm" and query.op != "lookup" and
                                 (query.preds or query.op == "materialize")) else [1024]
    filters = grid.filter if query.preds else ["branchy"]
    best = None
    for order, terms in orders_for(query, rates, predicate_orders(space)):
        qi = QueryInfo(query.op, query.cols, terms, query.lookups, query.id_dist)
        for b in batches:
            for f in filters:
                cost = model.predict(st, ExecInfo(parse_size(b), f), qi, ctx.data)
                if best is None or cost.total_ns < best.cost.total_ns:
                    ex = {}
                    if len(batches) > 1:
                        ex["dsm_batch"] = parse_size(b)
                    if query.preds:
                        ex["filter"] = f
                    if order != "-":
                        ex["order"] = order
                    best = QueryChoice(query, ex, cost, [r for _, r in terms])
    return best


class Search:
    def run(self, space, workload, model, ctx):
        raise NotImplementedError

    @staticmethod
    def candidates(space, ctx):
        return storage_configs(space, _DataShim(ctx.data))

    @staticmethod
    def score(choices, ctx):
        tw = sum(c.query.weight for c in choices)
        s = sum(c.query.weight * c.cost.total_ns for c in choices)
        return s / tw if ctx.objective == "mean" else s


class _DataShim:
    """candidates.VALIDITY expects engine.DataSpec-like objects (row_bytes)."""

    def __init__(self, d):
        self.row_bytes = d.cols * 8


def make_search(name):
    return SEARCHES.get(name)()
