"""Advisor output (plan §6.4): recommendation, per-query execution options, predicted time,
runner-up and margin, worst option, where the time goes, and how robust the choice is.
"""
import json
from dataclasses import replace

from .selectivity import Estimator


def fmt_ns(ns):
    for unit, div in (("s", 1e9), ("ms", 1e6), ("µs", 1e3)):
        if ns >= div:
            return f"{ns / div:.3g} {unit}"
    return f"{ns:.3g} ns"


def exec_text(ex):
    if not ex:
        return "-"
    parts = []
    if "filter" in ex:
        parts.append("branch-free" if ex["filter"] == "branchless" else "branchy")
    if "dsm_batch" in ex:
        parts.append(f"batch {ex['dsm_batch']}")
    if "order" in ex:
        parts.append(ex["order"].replace("_", "-"))
    return ", ".join(parts)


class ScaledEstimator(Estimator):
    """Every pass rate multiplied by `factor` (clamped to [0, 1]): selectivity uncertainty."""

    def __init__(self, inner, factor):
        super().__init__(inner.data, inner.hists)
        self.inner, self.factor = inner, factor

    def rates(self, query):
        return [min(1.0, max(0.0, r * self.factor)) for r in self.inner.rates(query)]


def sensitivity(space, workload, model, ctx, search, best_key):
    """Ranges over which the recommendation stays the same (selectivity scale, memory speed)."""
    def winner(m, c):
        return search.run(space, workload, m, c).best.storage.key()

    out = {}
    held = [f for f in (0.25, 0.5, 0.8, 1.25, 2.0, 4.0)
            if winner(model, replace(ctx, estimator=ScaledEstimator(ctx.estimator, f))) == best_key]
    out["selectivity_factors_holding"] = held
    held = []
    k0 = model.params["k_mem"]
    for scale in (0.8, 1.25):  # memory 20% slower / faster ~ bandwidth -20% / +25%
        model.params["k_mem"] = k0 * scale
        if winner(model, ctx) == best_key:
            held.append(scale)
    model.params["k_mem"] = k0
    out["memory_time_scales_holding"] = held
    return out


def to_json(ranked, workload, hw, data, model_name, sens=None):
    def choice(c):
        b = c.cost
        return {"query": c.query.name, "weight": c.query.weight, "text": c.query.describe(),
                "exec": c.exec, "pass_rates": c.rates, "predicted_ns": b.total_ns,
                "mem_ns": b.mem_ns, "cpu_ns": b.cpu_ns, "dram_bytes": b.dram_bytes,
                "branch_misses": b.branch_misses}

    return {
        "workload": workload.name, "model": model_name,
        "machine": hw.hw.get("machine", {}).get("cpu_model", "?"),
        "table": {"rows": data.rows, "cols": data.cols, "bytes": data.rows * data.cols * 8},
        "objective": ranked.objective,
        "ranking": [{"storage": r.storage.key(), "cost_ns": r.cost_ns,
                     "x_best": r.cost_ns / ranked.best.cost_ns,
                     "queries": [choice(c) for c in r.choices]} for r in ranked.results],
        "sensitivity": sens,
    }


def to_text(ranked, workload, hw, data, model_name, sens=None):
    best, res = ranked.best, ranked.results
    second = res[1] if len(res) > 1 else best
    worst = res[-1]
    machine = hw.hw.get("machine", {}).get("cpu_model", "unknown machine")
    gb = data.rows * data.cols * 8 / 2 ** 30
    unit = "per query (weighted mean)" if ranked.objective == "mean" else "per workload unit"
    lines = [
        f"Machine:     {machine} (hw.json created {hw.hw.get('created', '?')}), 1 thread",
        f"Table:       {data.rows:,} rows x {data.cols} columns ({gb:.2f} GB); model {model_name}",
        f"Workload:    {workload.name} ({len(workload.queries)} queries)",
        "",
        f"Recommended storage: {best.storage.key()}",
        f"Predicted:           {fmt_ns(best.cost_ns)} {unit}",
    ]
    margin = second.cost_ns / best.cost_ns
    close = "  -> close call" if margin < 1.05 else ""
    lines.append(f"Runner-up:           {second.storage.key()} ({fmt_ns(second.cost_ns)}, "
                 f"{margin:.2f}x slower){close}")
    by_layout = {}
    for r in res:
        by_layout.setdefault(r.storage.layout, r)
    others = ", ".join(f"{lay}: {r.storage.key()} {r.cost_ns / best.cost_ns:.2f}x"
                       for lay, r in by_layout.items() if r is not best)
    lines.append(f"Best of each layout: {others}")
    lines.append(f"Worst option:        {worst.storage.key()} ({fmt_ns(worst.cost_ns)}, "
                 f"{worst.cost_ns / best.cost_ns:.1f}x)")
    lines += ["", "Per query on the recommended storage:"]
    tw = sum(c.query.weight * c.cost.total_ns for c in best.choices) or 1.0
    for c in best.choices:
        b = c.cost
        share = c.query.weight * b.total_ns / tw
        parts = b.mem_ns + b.cpu_ns or 1.0
        lines.append(
            f"  {c.query.name:6s} w={c.query.weight:<5g} {fmt_ns(b.total_ns):>10s}  "
            f"{100 * share:4.0f}% of time  [mem {100 * b.mem_ns / parts:.0f}% / cpu "
            f"{100 * b.cpu_ns / parts:.0f}%, {b.dram_bytes / 1e6:.3g} MB DRAM]  "
            f"exec: {exec_text(c.exec)}  -- {c.query.describe()}")
    if sens:
        s = sens["selectivity_factors_holding"]
        m = sens["memory_time_scales_holding"]
        lines += ["", "Sensitivity:"]
        lines.append(f"  selectivity x{{0.25, 0.5, 0.8, 1.25, 2, 4}}: choice holds for "
                     f"{', '.join(f'x{f:g}' for f in s) or 'none'}")
        lines.append(f"  memory time x0.8 / x1.25: choice holds for "
                     f"{', '.join(f'x{f:g}' for f in m) or 'neither'}")
    return "\n".join(lines)


def dump_json(obj):
    return json.dumps(obj, indent=2, default=float)
