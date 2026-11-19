"""Access-pattern descriptions: one function per (layout, op), mirroring src/{nsm,dsm,pax}.cpp.

describe(storage, exec, query, data) -> Description(pattern, cpu terms, branch misses, rounds)

Selections: predicate j is evaluated on the rows that passed predicates 1..j-1 (pass rate
F_{j-1}); projected columns are read for the rows that pass all (F). Each column is read by
exactly one component, with the largest of its access probabilities (a predicate column that is
also projected is still in cache when it is gathered).

CPU work is a list of terms (constant name, count, argument); models turn them into cycles with
the calibrated constants in hw.json["cpu"]. Adding a layout or operator = one function here (plus
its calibration kernels if it has new inner loops).
"""
import math
from dataclasses import dataclass, field

from .basic import RAcc, Reuse, Stream
from .combine import Conc, Rounds, Seq
from .regions import Component, column_runs

SINK_TUPLES = 1024  # src/tuple_sink.h kBatchTuples


@dataclass
class Description:
    pattern: object
    cpu: list = field(default_factory=list)      # (name, count, arg) in "base" work
    branch_misses: float = 0.0                   # branchy predicate mispredictions (v3)
    overhead: list = field(default_factory=list)  # (name, count, arg): per batch / page (v3)


# --------------------------------------------------------------------------- selections

def selection_gap(query, data, upto):
    """Mean run of skipped rows for the selection by the first `upto` predicates."""
    F = query.cum_rates()[upto]
    if upto == 0 or F >= 1.0:
        return 1.0
    if F <= 0.0:
        return math.inf
    runs = []
    for c, r in query.preds[:upto]:
        try:
            runs.append(data.model(c).mean_run(r))
        except NotImplementedError:
            runs.append(1.0 / (1.0 - r) if r < 1 else math.inf)
    independent = 1.0 / (1.0 - F)
    L = max(independent, min(runs)) if runs else independent
    return max(1.0, L * (1.0 - F) / F)


def branch_rate(data, col, rate):
    """Mispredictions per evaluation of a branchy `value < K` with pass rate `rate`."""
    m = data.model(col)
    if m.branch_predictable():
        return 0.0
    run = getattr(m, "run", 1)
    return 2.0 * rate * (1.0 - rate) / max(1, run)


def column_access(query, data):
    """{column: (access probability, gap)} for a filter/scan query."""
    F = query.cum_rates()
    acc = {}
    for j, (c, _) in enumerate(query.preds):
        if F[j] > acc.get(c, (-1, 0))[0]:
            acc[c] = (F[j], selection_gap(query, data, j))
    if query.op != "lookup":
        f_all = F[-1]
        g = selection_gap(query, data, len(query.preds))
        for c in query.cols:
            if f_all > acc.get(c, (-1, 0))[0]:
                acc[c] = (f_all, g)
    return acc


def group_components(acc, run_of, unit_is_block):
    """Columns with the same (f, gap) share one component; runs from run_of(columns)."""
    groups = {}
    for c, fg in acc.items():
        groups.setdefault(fg, []).append(c)
    return tuple(Component(run_of(sorted(cs)), f, g, unit_is_block)
                 for (f, g), cs in sorted(groups.items(), key=lambda x: -x[0][0]))


def predicate_cpu(query, data, rows, first_name, refine_name, exec_opts):
    """Predicate-evaluation terms and branch misses shared by all layouts."""
    F = query.cum_rates()
    cpu, br = [], 0.0
    for j, (c, r) in enumerate(query.preds):
        n_eval = rows * F[j]
        cpu.append((first_name if j == 0 else refine_name, n_eval, None))
        if exec_opts.filter == "branchy":
            br += n_eval * branch_rate(data, c, r)
    return cpu, br


def sink(k, n_out):
    if n_out <= 0:
        return []
    buf = min(SINK_TUPLES, max(1.0, n_out)) * k * 8
    return [Reuse("sink", buf, 2.0 * max(1.0, n_out / SINK_TUPLES))]


def sel_bytes(query, batch):
    """Selection-vector bytes live per batch: the positions that passed the first predicate
    (later passes read only those)."""
    f1 = query.cum_rates()[1] if query.preds else 1.0
    return max(8.0, 4.0 * batch * f1)


def sorted_lookup_rate(query, rows):
    return 1.0 - math.exp(-query.lookups / rows)


# --------------------------------------------------------------------------- NSM

def describe_nsm(storage, ex, q, data):
    rows, cols = data.rows, data.cols
    row = cols * 8
    k = q.k
    runs = lambda cs: column_runs(cs)  # noqa: E731
    if q.op == "lookup":
        n = q.lookups
        if q.id_dist == "sorted":
            f = sorted_lookup_rate(q, rows)
            comps = (Component(runs(q.cols), f, 1.0 / f, True),)
            pat = Conc([Stream("table", row, rows, comps, overfetch_ok=False)] + sink(k, n))
        else:
            pat = Conc([RAcc("table", n, tuple(c * 8 for c in q.cols), row, rows * row)] +
                       sink(k, n))
        return Description(pat, [("nsm_copy", n, k)])
    acc = column_access(q, data)
    comps = group_components(acc, runs, True)
    n_out = rows * q.selectivity
    live = (SINK_TUPLES / max(q.selectivity, 1e-9)) * row
    parts = [Stream("table", row, rows, comps, live_bytes=live)]
    if q.op == "materialize":
        parts += sink(k, n_out)
    if not q.preds:
        cpu = [("nsm_row", rows, None), ("nsm_value", rows * (k - 1), None)]
        if q.op == "materialize":
            cpu = [("nsm_copy", rows, k)]
        return Description(Conc(parts), cpu)
    cpu, br = predicate_cpu(q, data, rows, "nsm_pred", "nsm_value", ex)
    if ex.filter == "branchless":  # every row does the projection work
        n_work = rows
        br = 0.0
    else:
        n_work = n_out
    cpu.append(("nsm_copy", n_work, k) if q.op == "materialize" else
               ("nsm_value", n_work * k, None))
    return Description(Conc(parts), cpu, br)


# --------------------------------------------------------------------------- DSM

def describe_dsm(storage, ex, q, data):
    rows = data.rows
    k = q.k
    if q.op == "lookup":
        n = q.lookups
        if q.id_dist == "sorted":
            f = sorted_lookup_rate(q, rows)
            parts = [Stream(f"c{c}", 8, rows, (Component(((0, 8),), f, 1.0 / f),),
                            overfetch_ok=False) for c in q.cols]
        else:
            parts = [RAcc(f"c{c}", n, (0,), 8, rows * 8) for c in q.cols]
        return Description(Conc(parts + sink(k, n)),
                           [("dsm_gather_k1", n, None), ("dsm_gather_value", n * (k - 1), None)])
    acc = column_access(q, data)
    if not q.preds and q.op == "sum":
        parts = [Stream(f"c{c}", 8, rows, (Component(((0, 8),), 1.0),)) for c in q.cols]
        return Description(Seq(parts), [("dsm_scan", rows * k, None)])
    b = max(1, ex.dsm_batch)
    rounds = math.ceil(rows / b)
    streams = [Stream(f"c{c}", 8, rows, (Component(((0, 8),), f, g),), live_bytes=b * 8)
               for c, (f, g) in acc.items()]
    n_out = rows * q.selectivity
    reuse = [Reuse("sel", sel_bytes(q, b), rounds * len(acc))]
    if q.op == "materialize":
        reuse += sink(k, n_out)
    pat = Rounds(Conc(streams + reuse), rounds, len(acc))
    if q.preds:
        bl = ex.filter == "branchless"
        cpu, br = predicate_cpu(q, data, rows, "dsm_pred_branchless" if bl else "dsm_pred",
                                "dsm_refine_branchless" if bl else "dsm_refine", ex)
    else:  # materialize without a filter: every row is selected
        cpu, br = [("dsm_pred_branchless", rows, None)], 0.0
    if q.op == "materialize":
        cpu += [("dsm_gather_k1", n_out, None), ("dsm_gather_value", n_out * (k - 1), None)]
    else:
        cpu += [("dsm_select_sum", n_out, None), ("dsm_sum_value", n_out * (k - 1), None)]
    return Description(pat, cpu, br, [("o_batch", rounds, None)])


# --------------------------------------------------------------------------- PAX

def describe_pax(storage, ex, q, data):
    rows, cols, page = data.rows, data.cols, storage.page
    T = page // (cols * 8)
    pages = math.ceil(rows / T)
    k = q.k
    mp = T * 8
    runs = lambda cs: column_runs(cs, width=mp, stride=mp)  # noqa: E731
    if q.op == "lookup":
        n = q.lookups
        if q.id_dist == "sorted":
            f = sorted_lookup_rate(q, rows)
            comps = (Component(runs(q.cols), f, 1.0 / f),)
            parts = [Stream("pages", page, pages, comps, overfetch_ok=False)]
        else:
            parts = [RAcc("pages", n, tuple(c * mp for c in q.cols), 8, pages * page)]
        return Description(Conc(parts + sink(k, n)),
                           [("dsm_gather_k1", n, None), ("dsm_gather_value", n * (k - 1), None),
                            ("pax_lookup_extra", n, None)])
    acc = column_access(q, data)
    comps = group_components(acc, runs, False)
    stream = Stream("pages", page, pages, comps, live_bytes=page)
    if not q.preds and q.op == "sum":
        return Description(stream, [("pax_scan", rows * k, None)],
                           overhead=[("pax_scan_o_page", pages * k, None)])
    n_out = rows * q.selectivity
    parts = [stream, Reuse("sel", sel_bytes(q, T), pages * len(acc))]
    if q.op == "materialize":
        parts += sink(k, n_out)
    if q.preds:
        bl = ex.filter == "branchless"
        cpu, br = predicate_cpu(q, data, rows, "pax_pred_branchless" if bl else "pax_pred",
                                "dsm_refine_branchless" if bl else "dsm_refine", ex)
    else:
        cpu, br = [("pax_pred_branchless", rows, None)], 0.0
    if q.op == "materialize":
        cpu.append(("pax_gather_value", n_out * k, None))
    else:
        cpu += [("dsm_select_sum", n_out, None), ("dsm_sum_value", n_out * (k - 1), None)]
    return Description(Conc(parts), cpu, br, [("pax_pred_o_page", pages, None)])


DESCRIBERS = {"nsm": describe_nsm, "dsm": describe_dsm, "pax": describe_pax}


def describe(storage, exec_opts, query, data):
    try:
        fn = DESCRIBERS[storage.layout]
    except KeyError:
        raise ValueError(f"no access-pattern description for layout '{storage.layout}'") from None
    return fn(storage, exec_opts, query, data)
