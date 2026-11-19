"""Decision maps (plan §6.4 / V5): which storage the advisor picks over a grid of workload
parameters, for one data profile.

Grids:
  selectivity x projection   one query (sum or materialize) with `c0 < K` at each pass rate
  lookup x selectivity       mix of a filtered scan and point lookups, by lookup weight

Writes <out>/<name>.csv (winner, runner-up, margin per cell) and <name>.png.
"""
import csv
import math
from pathlib import Path

from .search.base import SearchContext
from .selectivity import FixedEstimator
from .workload import Predicate, Workload, WorkloadQuery

# Fixed categorical slots per layout (validated default palette, slots 1-3), so a layout keeps
# its colour across every map.
LAYOUT_COLORS = {"nsm": "#2a78d6", "dsm": "#eb6834", "pax": "#1baf7a"}
INK, INK_MUTED, SURFACE = "#1f1f1e", "#5f5e5a", "#ffffff"

DEFAULT_RATES = [0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 0.9, 1.0]


def _k_values(ncols):
    ks, k = [], 1
    while k < ncols:
        ks.append(k)
        k *= 2
    return ks + [ncols]


def _scan_query(op, k, rate, weight=1.0, name="scan"):
    return WorkloadQuery(name, weight, op, list(range(k)),
                         [Predicate(0, -math.inf, 0, f"c0 pass {rate:g}")], rates=[rate])


def selectivity_projection(space, model, search, data, op="sum", rates=None, ks=None):
    rates, ks = rates or DEFAULT_RATES, ks or _k_values(data.cols)
    ctx = SearchContext(data, FixedEstimator(data))
    cells = []
    for k in ks:
        for r in rates:
            w = Workload(f"{op} k={k} f={r}", [_scan_query(op, k, r)])
            cells.append(_cell(search.run(space, w, model, ctx), {"k": k, "selectivity": r}))
    return cells, "selectivity", "k", rates, ks


def lookup_selectivity(space, model, search, data, op="sum", rates=None, lookup_n=10000,
                       weights=(0.0, 0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0)):
    rates = rates or DEFAULT_RATES
    ctx = SearchContext(data, FixedEstimator(data))
    cells = []
    for lw in weights:
        for r in rates:
            qs = []
            if lw < 1:
                qs.append(_scan_query(op, data.cols, r, 1 - lw))
            if lw > 0:
                qs.append(WorkloadQuery("lookup", lw, "lookup", list(range(data.cols)),
                                        lookups=lookup_n))
            w = Workload(f"lookup weight {lw}, f={r}", qs)
            cells.append(_cell(search.run(space, w, model, ctx),
                               {"lookup_weight": lw, "selectivity": r}))
    return cells, "selectivity", "lookup_weight", rates, list(weights)


def _cell(ranked, coords):
    b = ranked.results[0]
    second = ranked.results[1] if len(ranked.results) > 1 else b
    return {**coords, "winner": b.storage.key(), "layout": b.storage.layout,
            "predicted_ns": b.cost_ns, "runner_up": second.storage.key(),
            "margin": second.cost_ns / b.cost_ns}


def write(cells, xkey, ykey, xs, ys, out, name, title):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / f"{name}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cells[0].keys()))
        w.writeheader()
        w.writerows(cells)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch, Rectangle
    except ImportError:
        return out / f"{name}.csv"
    fig, ax = plt.subplots(figsize=(1.0 * len(xs) + 2, 0.62 * len(ys) + 1.8))
    lookup = {(c[xkey], c[ykey]): c for c in cells}
    for i, y in enumerate(ys):
        for j, x in enumerate(xs):
            c = lookup[(x, y)]
            # translucent fill so the in-cell label stays readable; margin shown as text
            ax.add_patch(Rectangle((j, i), 1, 1, facecolor=LAYOUT_COLORS.get(c["layout"], "#999"),
                                   alpha=0.85 if c["margin"] >= 1.1 else 0.45,
                                   edgecolor=SURFACE, linewidth=2))
            label = c["winner"].replace("pax:page=", "pax ")
            ax.text(j + 0.5, i + 0.58, label, ha="center", va="center", fontsize=7, color=INK)
            ax.text(j + 0.5, i + 0.28, f"{c['margin']:.2f}x", ha="center", va="center",
                    fontsize=6, color=INK_MUTED)
    ax.set_xlim(0, len(xs))
    ax.set_ylim(0, len(ys))
    ax.set_xticks([j + 0.5 for j in range(len(xs))])
    ax.set_xticklabels([f"{x:g}" for x in xs], fontsize=8, color=INK_MUTED)
    ax.set_yticks([i + 0.5 for i in range(len(ys))])
    ax.set_yticklabels([f"{y:g}" for y in ys], fontsize=8, color=INK_MUTED)
    ax.set_xlabel(xkey.replace("_", " "), color=INK_MUTED)
    ax.set_ylabel(ykey.replace("_", " "), color=INK_MUTED)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(length=0)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    present = sorted({c["layout"] for c in cells})
    handles = [Patch(facecolor=LAYOUT_COLORS[lay], label=lay.upper()) for lay in present]
    handles.append(Patch(facecolor="#cccccc", alpha=0.45, label="faded: runner-up within 10%"))
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False,
              fontsize=8)
    fig.tight_layout()
    fig.savefig(out / f"{name}.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return out / f"{name}.png"
