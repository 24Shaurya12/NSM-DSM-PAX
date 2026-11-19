#!/usr/bin/env python3
"""Draws the report's figures and tables from results/ (written by experiments.py).

Usage: figures.py [--results DIR] [--out DIR] [--only fig1,fig2] [--png]

Writes report/figures/<name>.pdf (and .png with --png) and report/generated/<name>.tex
(LaTeX tables and numbers quoted in the text). A figure whose data is missing is skipped
with a note, so this can be re-run as experiments finish.
"""
import argparse
import csv
import math
import os
import statistics
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.ticker import (FixedLocator, FuncFormatter, LogLocator, NullFormatter,  # noqa: E402
                               NullLocator)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Style ------------------------------------------------------------------------------
# Layouts keep the colours of scripts/plot.py (categorical slots 1-3). Strategies use a
# second validated set (slots 7, 5, 6, 4, 8 of the reference palette, in this order; adjacent
# pairs pass the CVD and normal-vision checks). Every series also has its own line style and
# marker, so identity never rests on colour alone.
LAYOUT_COLOR = {"NSM": "#2a78d6", "DSM": "#eb6834", "PAX": "#1baf7a"}
STRATEGY_COLOR = {"early": "#6250d6", "early-pipe": "#e87ba4", "late": "#008300",
                  "late-branch": "#eda100", "auto": "#e34948"}
STRATEGY_STYLE = {"early": "--", "early-pipe": ":", "late": "-", "late-branch": "-.", "auto": "-"}
STRATEGY_MARKER = {"early": "s", "early-pipe": "v", "late": "o", "late-branch": "D", "auto": "*"}
LAYOUT_MARKER = {"NSM": "s", "DSM": "o", "PAX": "^"}
ENCODING_COLOR = {"plain": "#6f6e69", "bitpack": "#eda100", "for": "#e87ba4", "dict": "#008300", "dictb": "#14a3a3",
                  "rle": "#6250d6", "delta": "#e34948"}
ENCODING_MARKER = {"plain": "o", "bitpack": "s", "for": "^", "dict": "D", "dictb": "d", "rle": "v", "delta": "P"}
TEXT = "#0b0b0b"
TEXT2 = "#52514e"
GRID = "#e1e0d9"
SURFACE = "#ffffff"
CACHE_LINE_COLOR = "#898781"

L1, L2, L3 = 32 << 10, 512 << 10, 16 << 20

plt.rcParams.update({
    "font.size": 8, "axes.titlesize": 8.5, "axes.labelsize": 8, "legend.fontsize": 7,
    "xtick.labelsize": 7, "ytick.labelsize": 7, "axes.edgecolor": GRID,
    "axes.labelcolor": TEXT, "xtick.color": TEXT2, "ytick.color": TEXT2,
    "text.color": TEXT, "font.family": "DejaVu Sans", "pdf.fonttype": 42,
    "savefig.facecolor": SURFACE, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "lines.linewidth": 1.6, "lines.markersize": 4.5,
})

BASE = ["query", "layout", "strategy", "dist", "rows", "cols", "k", "where", "sel", "page_size",
        "batch", "knobs", "footprint_bytes", "iteration", "latency_ns", "rows_out"]


# --- Data -------------------------------------------------------------------------------
class Results:
    def __init__(self, root):
        self.root = root

    def path(self, exp, name):
        return os.path.join(self.root, exp, name + ".csv")

    def have(self, exp, name):
        return os.path.exists(self.path(exp, name))

    def rows(self, exp, name):
        with open(self.path(exp, name)) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            r["_family"] = family(r["layout"])
        return rows


def family(layout):
    """DSM-dict-vector -> DSM; NSM -> NSM."""
    return layout.split("-")[0]


def encoding_of(layout):
    parts = layout.split("-")
    return parts[1] if len(parts) == 3 else None


def select(rows, **kw):
    out = []
    for r in rows:
        ok = True
        for k, v in kw.items():
            if isinstance(v, (list, tuple, set)):
                ok = r[k] in {str(x) for x in v}
            else:
                ok = r[k] == str(v)
            if not ok:
                break
        if ok:
            out.append(r)
    return out


def med_ms(rows):
    return statistics.median(int(r["latency_ns"]) for r in rows) / 1e6


def quartiles_ms(rows):
    v = sorted(int(r["latency_ns"]) / 1e6 for r in rows)
    if len(v) < 4:
        return v[0], v[-1]
    q = statistics.quantiles(v, n=4)
    return q[0], q[2]


def per_row(rows, counter):
    return statistics.median(int(r[counter]) / int(r["rows"]) for r in rows)


def series(rows, xkey, ykey="latency", xconv=float, **kw):
    """[(x, median y, lo, hi)] over the rows matching kw, sorted by x."""
    groups = defaultdict(list)
    for r in select(rows, **kw):
        groups[xconv(r[xkey])].append(r)
    out = []
    for x in sorted(groups):
        rs = groups[x]
        if ykey == "latency":
            lo, hi = quartiles_ms(rs)
            out.append((x, med_ms(rs), lo, hi))
        else:
            out.append((x, per_row(rs, ykey), None, None))
    return out


# --- Axes helpers -------------------------------------------------------------------------
def style_axes(ax, grid_y=True):
    ax.grid(axis="y" if grid_y else "both", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=2.5, width=0.6)


def fmt_bytes(b):
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if b >= size:
            v = b / size
            return f"{v:g} {unit}" if v == int(v) else f"{v:.1f} {unit}"
    return f"{int(b)} B"


def log2_axis(ax, ticks, labels=None):
    ax.set_xscale("log", base=2)
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.xaxis.set_major_formatter(FuncFormatter(
        lambda v, _: labels[ticks.index(v)] if labels and v in ticks else f"{v:g}"))
    ax.xaxis.set_minor_locator(NullLocator())


def sel_axis(ax, sels):
    ax.set_xscale("log")
    ticks = [s for s in (0.0001, 0.001, 0.01, 0.1, 1) if min(sels) <= s <= max(sels) * 1.01]
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v * 100:g}%"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlabel("selectivity (fraction of rows that qualify)")


def log_y(ax):
    ax.set_yscale("log")
    lo, hi = ax.get_ylim()
    # A narrow range (under 4x) gets finer labelled ticks, so at least two are visible.
    subs = (1, 2, 5) if hi / max(lo, 1e-12) > 4 else (1, 1.5, 2, 3, 5, 7)
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=subs))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.yaxis.set_minor_locator(LogLocator(base=10, subs="auto"))
    ax.yaxis.set_minor_formatter(NullFormatter())


def cache_marks(ax, positions, labels=("L1D", "L2", "L3"), y=0.97):
    for x, lab in zip(positions, labels):
        ax.axvline(x, color=CACHE_LINE_COLOR, linewidth=0.7, linestyle=(0, (2, 2)), zorder=0)
        ax.text(x, y, " " + lab, transform=ax.get_xaxis_transform(), fontsize=6.5,
                color=TEXT2, va="top", ha="left")


def plot_series(ax, pts, color, style="-", marker="o", label=None, band=False, lw=1.6, z=3):
    if not pts:
        return
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if band and pts[0][2] is not None:
        ax.fill_between(xs, [p[2] for p in pts], [p[3] for p in pts], color=color, alpha=0.12,
                        linewidth=0)
    ax.plot(xs, ys, color=color, linestyle=style, marker=marker, label=label, linewidth=lw,
            markeredgecolor=SURFACE, markeredgewidth=0.6, zorder=z)


def bottom_legend(fig, ax, ncol=None, y=-0.06, handles=None, labels=None):
    """One legend for the whole figure, centred below the axes."""
    if handles is None:
        handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, loc="upper center", ncol=ncol or len(handles),
               fontsize=6.8, bbox_to_anchor=(0.5, y if y < 0 else y), handlelength=2.6)


def save(fig, out, name, png):
    os.makedirs(out, exist_ok=True)
    fig.savefig(os.path.join(out, name + ".pdf"), bbox_inches="tight")
    if png:
        fig.savefig(os.path.join(out, name + ".png"), dpi=170, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {name}")


def write_tex(gen, name, text):
    os.makedirs(gen, exist_ok=True)
    with open(os.path.join(gen, name + ".tex"), "w") as f:
        f.write(text)
    print(f"  wrote generated/{name}.tex")


# --- Figures ------------------------------------------------------------------------------
FIGURES = []


def figure(fn):
    FIGURES.append(fn)
    return fn


@figure
def fig_machine(R, out, gen, png):
    if not R.have("machine", "membench"):
        return "no results/machine/membench.csv"
    with open(R.path("machine", "membench")) as f:
        rows = list(csv.DictReader(f))
    data = defaultdict(list)
    for r in rows:
        data[r["test"]].append((int(r["bytes"]), float(r["value"])))
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.6, 2.3))
    lat = data["latency"]
    a.plot([x for x, _ in lat], [y for _, y in lat], color=LAYOUT_COLOR["NSM"], marker="o",
           markersize=2.5, linewidth=1.4)
    a.set_xscale("log", base=2)
    log_y(a)
    a.set_ylabel("load-to-use latency (ns)")
    a.set_title("Dependent-load latency", loc="left", fontweight="bold")
    for name, color, style in (("read", LAYOUT_COLOR["NSM"], "-"),
                               ("write", LAYOUT_COLOR["DSM"], "--"),
                               ("write_nt", LAYOUT_COLOR["PAX"], ":")):
        pts = data[name]
        lab = {"read": "read", "write": "write", "write_nt": "write, non-temporal"}[name]
        b.plot([x for x, _ in pts], [y for _, y in pts], color=color, linestyle=style,
               linewidth=1.5, label=lab)
    b.set_xscale("log", base=2)
    b.set_ylim(bottom=0)
    b.set_ylabel("bandwidth, one core (GB/s)")
    b.set_title("Sequential bandwidth", loc="left", fontweight="bold")
    b.legend(frameon=False, loc="lower left")
    ticks = [4 << 10, 64 << 10, 1 << 20, 16 << 20, 256 << 20]
    for ax in (a, b):
        log2_axis(ax, ticks, [fmt_bytes(t) for t in ticks])
        ax.set_xlabel("working-set size")
        style_axes(ax)
        cache_marks(ax, [L1, L2, L3])
    fig.tight_layout(w_pad=2)
    save(fig, out, "machine", png)

    # Numbers for the text: medians over each level's plateau.
    def plateau(test, lo, hi):
        v = [y for x, y in data[test] if lo <= x <= hi]
        return statistics.median(v) if v else float("nan")
    levels = [("L1D", 4 << 10, 24 << 10), ("L2", 64 << 10, 256 << 10),
              ("L3", 2 << 20, 8 << 20), ("DRAM", 128 << 20, 1 << 30)]
    lines = ["\\begin{tabular}{lrrrr}", "\\toprule",
             "Level & Latency (ns) & Read (GB/s) & Write (GB/s) & NT write (GB/s) \\\\",
             "\\midrule"]
    for name, lo, hi in levels:
        lines.append(f"{name} & {plateau('latency', lo, hi):.1f} & {plateau('read', lo, hi):.0f} & "
                     f"{plateau('write', lo, hi):.0f} & {plateau('write_nt', lo, hi):.0f} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "machine_table", "\n".join(lines) + "\n")


@figure
def fig_projectivity(R, out, gen, png):
    if not (R.have("projectivity", "sum") and R.have("projectivity", "projection")):
        return "projectivity results missing"
    fig, axes = plt.subplots(1, 2, figsize=(6.6, 2.5))
    for ax, name, title in ((axes[0], "sum", "SUM of k columns (no tuples built)"),
                            (axes[1], "projection", "Projection of k columns (tuples built)")):
        rows = R.rows("projectivity", name)
        ks = sorted({int(r["k"]) for r in rows})
        for lay in ("NSM", "DSM", "PAX"):
            for st in ("early", "late"):
                pts = series(rows, "k", xconv=int, layout=lay, strategy=st)
                plot_series(ax, pts, LAYOUT_COLOR[lay], STRATEGY_STYLE[st], LAYOUT_MARKER[lay],
                            f"{lay} {st}")
        log2_axis(ax, ks)
        ax.set_xlabel(f"k = columns read (of N = {rows[0]['cols']})")
        ax.set_ylabel("latency (ms, median)")
        ax.set_ylim(bottom=0)
        ax.set_title(title, loc="left", fontweight="bold")
        style_axes(ax)
    axes[0].legend(frameon=False, ncol=1, loc="upper left", fontsize=6.5)
    r0 = rows[0]
    fig.text(0.0, -0.02, f"{int(r0['rows']):,} rows x {r0['cols']} int64 columns "
             f"({int(r0['rows']) * int(r0['cols']) * 8 >> 20} MiB per layout); dashed = early "
             "(tuple at a time), solid = late (column/vector at a time).", fontsize=6.5,
             color=TEXT2)
    fig.tight_layout(w_pad=2)
    save(fig, out, "projectivity", png)


def strategies_in(rows):
    order = list(STRATEGY_COLOR)
    return sorted({r["strategy"] for r in rows}, key=order.index)


SEL_TITLES = {"projection_k2": "SELECT c0, c1 WHERE c0 < x",
              "projection_k4": "SELECT c0..c3 WHERE c0 < x",
              "projection_k16": "SELECT * (16 columns) WHERE c0 < x",
              "sum_k4": "SELECT SUM(c0+..+c3), COUNT(*) WHERE c0 < x"}
SEL_SHORT = {"projection_k2": "Proj.\\ $k{=}2$", "projection_k4": "Proj.\\ $k{=}4$",
             "projection_k16": "Proj.\\ $k{=}16$", "sum_k4": "SUM $k{=}4$"}


def draw_selectivity(R, exp, prefix, outname, gen, png, out, tables=True):
    names = [n for n in ("projection_k2", "projection_k4", "projection_k16", "sum_k4")
             if R.have(exp, prefix + n)]
    if not names:
        return f"{exp} results missing"
    fig = plt.figure(figsize=(6.6, 2.05 * len(names)), layout="constrained")
    subs = fig.subfigures(len(names), 1, squeeze=False)[:, 0]
    first = None
    all_rows = {n: R.rows(exp, prefix + n) for n in names}
    for sub, name in zip(subs, names):
        rows = all_rows[name]
        sels = sorted({float(r["sel"]) for r in rows})
        size = int(rows[0]["rows"]) * int(rows[0]["cols"]) * 8
        sub.suptitle(f"{SEL_TITLES[name]}   ({int(rows[0]['rows']):,} rows x {rows[0]['cols']} "
                     f"columns, {fmt_bytes(size)})", x=0.01, ha="left", fontsize=8.5,
                     fontweight="bold")
        axes = sub.subplots(1, 3)
        for j, lay in enumerate(("NSM", "DSM", "PAX")):
            ax = axes[j]
            for st in strategies_in(rows):
                pts = series(rows, "sel", layout=lay, strategy=st)
                plot_series(ax, pts, STRATEGY_COLOR[st], STRATEGY_STYLE[st], STRATEGY_MARKER[st],
                            st, lw=1.3)
            sel_axis(ax, sels)
            ax.set_xlabel("selectivity" if name == names[-1] else "")
            log_y(ax)
            if j == 0:
                ax.set_ylabel("latency (ms)")
            ax.set_title(lay, loc="left", fontsize=8)
            style_axes(ax)
        first = first or axes[0]
    first.legend(frameon=False, loc="upper left", fontsize=6.3)
    save(fig, out, outname, png)
    if not tables:
        return None

    # Winners table: fastest layout/strategy per (query, selectivity).
    lines = ["\\begin{tabular}{r" + "l" * len(names) + "}", "\\toprule",
             "Selectivity & " + " & ".join(SEL_SHORT[n] for n in names) + " \\\\",
             "\\midrule"]
    sels = sorted({r["sel"] for r in all_rows[names[0]]}, key=float)
    for sv in sels:
        cells = []
        for n in names:
            best = None
            for lay in ("NSM", "DSM", "PAX"):
                for st in strategies_in(all_rows[n]):
                    rs = select(all_rows[n], layout=lay, strategy=st, sel=sv)
                    if rs:
                        m = med_ms(rs)
                        if best is None or m < best[0]:
                            best = (m, lay, st)
            cells.append(f"{best[1]} {best[2]} ({fmt_ms(best[0])})" if best else "-")
        lines.append(f"{pct(float(sv))} & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, outname + "_winners", "\n".join(lines) + "\n")

    # Crossover table: per query and layout, the selectivity where late (branch-free selection
    # vector) stops beating early (branch-free stitch-then-filter), log-interpolated.
    lines = ["\\begin{tabular}{lrrr}", "\\toprule", "Query & NSM & DSM & PAX \\\\",
             "\\midrule"]
    for n in names:
        cells = [fmt_crossover(crossover(all_rows[n], lay)) for lay in ("NSM", "DSM", "PAX")]
        lines.append(SEL_SHORT[n] + " & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, outname + "_crossovers", "\n".join(lines) + "\n")
    return None


@figure
def fig_selectivity(R, out, gen, png):
    return draw_selectivity(R, "selectivity", "", "selectivity", gen, png, out)


@figure
def fig_selectivity_cache(R, out, gen, png):
    return draw_selectivity(R, "selectivity_cache", "cache_", "selectivity_cache", gen, png, out)


def crossover(rows, lay, late=("late",), early=("early",), **kw):
    """Selectivity where min(late variants) crosses min(early variants), log-interpolated.
    Returns (value, kind): kind '<' (late never wins), '>' (late always wins), '=' (crossing)."""
    sels = sorted({float(r["sel"]) for r in rows if float(r["sel"]) > 0})
    def best(names, s):
        ms = [med_ms(rs) for st in names
              if (rs := [r for r in rows if r["layout"] == lay and r["strategy"] == st
                         and float(r["sel"]) == s
                         and all(r.get(k) == str(v) for k, v in kw.items())])]
        return min(ms) if ms else None
    ratio = []
    for s in sels:
        l, e = best(late, s), best(early, s)
        if l is not None and e is not None:
            ratio.append((s, math.log(l / e)))
    if not ratio:
        return None
    # The last crossing from "late ahead" to "early ahead", so that one noisy point at a low
    # selectivity does not decide it.
    if all(r > 0 for _, r in ratio):
        return (ratio[0][0], "<")
    if ratio[-1][1] <= 0:
        return (ratio[-1][0], ">")
    for (s0, r0), (s1, r1) in reversed(list(zip(ratio, ratio[1:]))):
        if r0 <= 0 < r1:
            t = r0 / (r0 - r1)
            return (math.exp(math.log(s0) + t * (math.log(s1) - math.log(s0))), "=")
    return (ratio[0][0], "<")


def fmt_ms(v):
    """Latency in ms with three significant figures (61.4, 7.27, 0.0213)."""
    return f"{v:.1f}" if v >= 10 else f"{v:.3g}"


def pct(v):
    """0.0832 -> '8.3\\%'; 1 -> '100\\%'."""
    p = v * 100
    return (f"{p:.0f}" if p >= 10 else f"{p:.2g}") + "\\%"


def fmt_crossover(c):
    if c is None:
        return "--"
    v, kind = c
    txt = pct(v)
    return {"<": "$<$ " + txt, ">": "$>$ " + txt, "=": txt}[kind]


@figure
def fig_selectivity_lines(R, out, gen, png):
    """Measured L1D misses per row vs the cache-line model, DSM, k = 16."""
    if not R.have("selectivity", "projection_k16"):
        return "selectivity results missing"
    rows = R.rows("selectivity", "projection_k16")
    if "l1d_misses" not in rows[0]:
        return "no perf counters"
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.6, 2.4))
    sels = sorted({float(r["sel"]) for r in rows})
    for lay, ax in (("DSM", a), ("PAX", b)):
        for st in strategies_in(rows):
            pts = series(rows, "sel", "l1d_misses", layout=lay, strategy=st)
            plot_series(ax, pts, STRATEGY_COLOR[st], STRATEGY_STYLE[st], STRATEGY_MARKER[st], st,
                        lw=1.3)
        # Model: input cache lines per row a strategy has to bring in. Early reads all 16
        # columns (16 x 8 B = 2 lines/row). Late reads c0 fully (1/8 line/row) and each other
        # column's line only if one of its 8 values is selected: 15/8 * (1 - (1 - s)^8).
        xs = [10 ** (e / 20) for e in range(-80, 1)]
        ax.plot(xs, [2.0] * len(xs), color=TEXT2, linewidth=0.9, linestyle=(0, (1, 1)),
                label="model: input lines, early")
        ax.plot(xs, [1 / 8 + 15 / 8 * (1 - (1 - s) ** 8) for s in xs], color=TEXT2,
                linewidth=0.9, linestyle=(0, (4, 1, 1, 1)), label="model: input lines, late")
        sel_axis(ax, sels)
        ax.set_ylabel("L1D misses per row")
        log_y(ax)
        ax.set_ylim(0.08, 30)
        ax.set_title(lay, loc="left", fontweight="bold")
        style_axes(ax)
    handles, labels = a.get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, loc="lower center", ncol=3, fontsize=6.5,
               bbox_to_anchor=(0.5, -0.13))
    fig.suptitle("SELECT * WHERE c0 < x (4M rows x 16 columns): L1D misses per row vs the "
                 "cache-line model", x=0.01, ha="left", fontweight="bold", fontsize=8.5)
    fig.tight_layout(w_pad=2)
    save(fig, out, "selectivity_lines", png)


@figure
def fig_width(R, out, gen, png):
    names = [n for n in (f"width_n{n}" for n in (2, 4, 8, 16, 32, 64, 128, 256))
             if R.have("width", n)]
    if len(names) < 2:
        return "width results missing"
    rows = []
    for n in names:
        rows += R.rows("width", n)
    panels = [("sum", "1", None, "SUM(c0), all rows qualify"),
              ("projection", "N", "0.01", "SELECT *, 1% qualify"),
              ("projection", "N", "1", "SELECT *, all rows qualify")]
    fig, axes = plt.subplots(1, 3, figsize=(6.6, 2.3))
    ns = sorted({int(r["cols"]) for r in rows})
    for ax, (q, k, sel, title) in zip(axes, panels):
        for lay in ("NSM", "DSM", "PAX"):
            pts = []
            for n in ns:
                kk = str(n) if k == "N" else k
                cand = select(rows, query=q, layout=lay, cols=n, k=kk)
                if sel is not None:
                    cand = [r for r in cand if abs(float(r["sel"]) - float(sel)) < 0.002]
                else:
                    cand = [r for r in cand if r["where"] == "-" or float(r["sel"]) > 0.999]
                    cand = [r for r in cand if r["where"] == "-"] or cand
                best = None
                for st in {r["strategy"] for r in cand}:
                    m = med_ms([r for r in cand if r["strategy"] == st])
                    best = m if best is None or m < best else best
                if best is not None:
                    pts.append((n, best, None, None))
            plot_series(ax, pts, LAYOUT_COLOR[lay], "-", LAYOUT_MARKER[lay], lay)
        log2_axis(ax, ns)
        ax.set_xlabel("N (columns per row)")
        ax.set_title(title, loc="left", fontweight="bold")
        log_y(ax)
        style_axes(ax)
    axes[0].set_ylabel("latency (ms, best strategy)")
    fig.suptitle("Table width at a fixed 512 MB per layout (R = 64M / N rows; WHERE c0 < x)", x=0.01,
                 ha="left", fontweight="bold", fontsize=8.5)
    fig.tight_layout(w_pad=1.2)
    bottom_legend(fig, axes[0], y=0.0)
    save(fig, out, "width", png)


@figure
def fig_workingset(R, out, gen, png):
    names = sorted([f[:-4] for f in os.listdir(os.path.join(R.root, "workingset"))
                    if f.endswith(".csv")] if os.path.isdir(os.path.join(R.root, "workingset"))
                   else [], key=lambda s: int(s.split("_")[1][:-1]))
    if len(names) < 3:
        return "working-set results missing"
    rows = []
    for n in names:
        rows += R.rows("workingset", n)
    panels = [("sum", "1", "SUM(c0)"), ("sum", "16", "SUM of all 16 columns"),
              ("projection", "16", "SELECT * (tuples rebuilt)")]
    fig, axes = plt.subplots(1, 3, figsize=(6.6, 2.35))
    for ax, (q, k, title) in zip(axes, panels):
        for lay in ("NSM", "DSM", "PAX"):
            groups = defaultdict(list)
            for r in select(rows, query=q, k=k, layout=lay):
                groups[(int(r["footprint_bytes"]) if lay != "PAX" else
                        int(r["rows"]) * int(r["cols"]) * 8, r["strategy"])].append(r)
            best = {}
            for (size, st), rs in groups.items():
                ns_row = statistics.median(int(r["latency_ns"]) for r in rs) / int(rs[0]["rows"])
                best[size] = min(best.get(size, 1e30), ns_row)
            pts = [(s, best[s], None, None) for s in sorted(best)]
            plot_series(ax, pts, LAYOUT_COLOR[lay], "-", LAYOUT_MARKER[lay], lay)
        ticks = [16 << 10, 512 << 10, 16 << 20, 512 << 20]
        log2_axis(ax, ticks, [fmt_bytes(t) for t in ticks])
        ax.set_xlabel("table size (each layout)")
        ax.set_title(title, loc="left", fontweight="bold")
        log_y(ax)
        style_axes(ax)
        cache_marks(ax, [L1, L2, L3])
    axes[0].set_ylabel("ns per row (best strategy)")
    fig.tight_layout(w_pad=1.2)
    bottom_legend(fig, axes[0], y=0.0)
    save(fig, out, "workingset", png)


@figure
def fig_pax(R, out, gen, png):
    d = os.path.join(R.root, "pax")
    if not os.path.isdir(d):
        return "pax results missing"
    rows = []
    for f in os.listdir(d):
        if f.endswith(".csv"):
            rows += R.rows("pax", f[:-4])
    if not rows:
        return "pax results missing"
    pad_of = lambda r: ("pad64" if "pad64" in r["knobs"] else  # noqa: E731
                        "odd" if "oddstride" in r["knobs"] else "pad0")
    # DSM reference: the width experiment's N = 16 run has the same table and predicates.
    ref = R.rows("width", "width_n16") if R.have("width", "width_n16") else []
    near = lambda r, sel: abs(float(r["sel"]) - sel) < 0.002 * max(sel, 0.01) * 100  # noqa: E731
    panels = [("sum", "1", 1.0, "SUM(c0): one minipage per page"),
              ("sum", "16", 1.0, "SUM of all 16 attributes"),
              ("projection", "16", 0.01, "SELECT *, 1% qualify"),
              ("projection", "16", 1.0, "SELECT *, all rows qualify")]
    fig, axes = plt.subplots(2, 2, figsize=(6.6, 4.3))
    pages = sorted({int(r["page_size"]) for r in rows})
    ticks = [t for t in (1024, 4096, 16384, 65536, 262144, 1048576, 4194304)
             if pages[0] <= t <= pages[-1]]
    for ax, (q, k, sel, title) in zip(axes.flat, panels):
        for st in ("early", "late"):
            for pad, mk, style, lw in (("pad0", STRATEGY_MARKER[st], STRATEGY_STYLE[st], 1.4),
                                       ("pad64", "x", ":", 0.9),
                                       ("odd", "*", (0, (3, 1, 1, 1)), 1.4)):
                cand = [r for r in select(rows, query=q, k=k, strategy=st)
                        if pad_of(r) == pad and near(r, sel)]
                plot_series(ax, series(cand, "page_size", xconv=int), STRATEGY_COLOR[st], style,
                            mk, None, lw=lw)
            cand = [r for r in select(ref, query=q, k=k, layout="DSM", strategy=st) if near(r, sel)]
            if cand:
                ax.axhline(med_ms(cand), color=LAYOUT_COLOR["DSM"], linewidth=0.9,
                           linestyle=STRATEGY_STYLE[st], zorder=1)
        log2_axis(ax, ticks, [fmt_bytes(t).replace(" ", "") for t in ticks])
        ax.set_xlabel("PAX page size" if ax in axes[1] else "")
        ax.set_title(title, loc="left", fontweight="bold")
        log_y(ax)
        style_axes(ax)
    for ax in axes[:, 0]:
        ax.set_ylabel("latency (ms)")
    handles = [Line2D([], [], color=STRATEGY_COLOR[s], linestyle=STRATEGY_STYLE[s],
                      marker=STRATEGY_MARKER[s], label=f"PAX {s}") for s in ("early", "late")]
    handles += [Line2D([], [], color=TEXT2, linestyle=":", marker="x", linewidth=0.9,
                       label="+64 B pad per minipage"),
                Line2D([], [], color=TEXT2, linestyle=(0, (3, 1, 1, 1)), marker="*", linewidth=1.4,
                       label="--pax-pad auto (odd-line stride if aliased)")]
    handles += [Line2D([], [], color=LAYOUT_COLOR["DSM"], linewidth=0.9, linestyle=STRATEGY_STYLE[s],
                       label=f"DSM {s}") for s in ("early", "late")]
    fig.tight_layout(h_pad=1.2, w_pad=1.5)
    bottom_legend(fig, None, ncol=3, y=0.0, handles=handles, labels=[h.get_label() for h in handles])
    save(fig, out, "pax_pagesize", png)


@figure
def fig_encodings(R, out, gen, png):
    dists = [d for d in ("uniform", "wide", "sorted", "runs", "zipf") if R.have("encodings", f"enc_{d}")]
    if not dists:
        return "encodings results missing"
    encs = list(ENCODING_COLOR)
    data = {}
    for d in dists:
        for simd in ("", "_simd"):
            if not R.have("encodings", f"enc_{d}{simd}"):
                continue
            rows = R.rows("encodings", f"enc_{d}{simd}")
            for q in ("sum", "projection"):
                # Baseline: uncompressed DSM, its better strategy, in the same process.
                base = min(med_ms(select(rows, query=q, layout="DSM", strategy=st))
                           for st in ("early", "late") if select(rows, query=q, layout="DSM", strategy=st))
                for e in encs:
                    for mode in ("vector", "value"):
                        ms = [med_ms(rs) for st in ("early", "late")
                              if (rs := select(rows, query=q, layout=f"DSM-{e}-{mode}", strategy=st))]
                        if ms:
                            data[(d, q, e, mode, simd)] = base / min(ms)
            for e in encs:
                rs = select(rows, layout=f"DSM-{e}-vector")
                if rs:
                    data[(d, "ratio", e)] = int(select(rows, layout="DSM")[0]["footprint_bytes"]) / \
                        int(rs[0]["footprint_bytes"])
    fig, axes = plt.subplots(2, 2, figsize=(6.6, 4.3))
    width = 0.8 / len(encs)
    panels = [(("ratio",), "Compression ratio"),
              (("sum", "value", ""), "SUM vs plain DSM: value mode"),
              (("sum", "vector", ""), "SUM vs DSM: vectors, scalar"),
              (("sum", "vector", "_simd"), "SUM vs DSM: vectors, AVX2")]
    for ax, (spec, title) in zip(axes.flat, panels):
        for i, e in enumerate(encs):
            xs, ys = [], []
            for j, d in enumerate(dists):
                key = (d, "ratio", e) if spec[0] == "ratio" else (d, spec[0], e, spec[1], spec[2])
                if key in data:
                    xs.append(j + (i - (len(encs) - 1) / 2) * width)
                    ys.append(data[key])
            ax.bar(xs, ys, width=width * 0.9, color=ENCODING_COLOR[e], label=e, bottom=0)
        ax.set_yscale("log")
        ax.axhline(1, color=TEXT, linewidth=0.7)
        ax.set_xticks(range(len(dists)), dists, rotation=0)
        ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.set_title(title, loc="left", fontweight="bold")
        style_axes(ax)
    axes.flat[1].set_ylim(0.03, 2)  # value mode: always slower
    for ax in axes.flat[2:]:
        ax.set_ylim(0.03, 200)  # rle SUM on runs reaches ~90x
    fig.tight_layout(h_pad=1.2, w_pad=1.2)
    bottom_legend(fig, axes.flat[0], ncol=6, y=0.0)
    save(fig, out, "encodings", png)

    lines = ["\\begin{tabular}{l" + "r" * len(encs) + "}", "\\toprule",
             "Distribution & " + " & ".join(encs) + " \\\\", "\\midrule"]
    for d in dists:
        lines.append(d + " & " + " & ".join(
            f"{data[(d, 'ratio', e)]:.1f}" if (d, "ratio", e) in data else "-" for e in encs) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "encodings_ratio", "\n".join(lines) + "\n")

    # Speedups over uncompressed DSM, vector mode: scalar / AVX2, for both queries.
    lines = ["\\begin{tabular}{ll" + "r" * len(encs) + "}", "\\toprule",
             "Distribution & Query & " + " & ".join(encs) + " \\\\", "\\midrule"]
    for d in dists:
        for q, qn in (("sum", "SUM"), ("projection", "SELECT *")):
            cells = []
            for e in encs:
                a, b = data.get((d, q, e, "vector", "")), data.get((d, q, e, "vector", "_simd"))
                cells.append("--" if a is None else (f"{a:.2f} / {b:.2f}" if b is not None else f"{a:.2f}"))
            lines.append(f"{d} & {qn} & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "encodings_speedup", "\n".join(lines) + "\n")


@figure
def fig_enc_selectivity(R, out, gen, png):
    dists = [d for d in ("uniform", "sorted", "runs") if R.have("enc_selectivity", f"encsel_{d}")]
    if not dists:
        return "enc_selectivity results missing"
    fig, axes = plt.subplots(2, len(dists), figsize=(6.6, 4.2), squeeze=False)
    table = {}
    for j, d in enumerate(dists):
        for simd in ("", "_simd"):
            if not R.have("enc_selectivity", f"encsel_{d}{simd}"):
                continue
            rows = R.rows("enc_selectivity", f"encsel_{d}{simd}")
            for q in ("sum", "projection"):
                for lay in sorted({r["layout"] for r in rows}):
                    c = crossover([r for r in rows if r["query"] == q], lay)
                    table[(d, q, encoding_of(lay) or "DSM", simd)] = c
        name = f"encsel_{d}_simd" if R.have("enc_selectivity", f"encsel_{d}_simd") else f"encsel_{d}"
        rows = R.rows("enc_selectivity", name)
        sels = sorted({float(r["sel"]) for r in rows if r["where"] != "-"})
        for i, q in enumerate(("sum", "projection")):
            ax = axes[i][j]
            encs = sorted({encoding_of(r["layout"]) for r in rows if encoding_of(r["layout"])},
                          key=list(ENCODING_COLOR).index)
            for e in encs:
                pts_e = series(rows, "sel", query=q, layout=f"DSM-{e}-vector", strategy="early")
                pts_l = series(rows, "sel", query=q, layout=f"DSM-{e}-vector", strategy="late")
                le = {x: y for x, y, _, _ in pts_e}
                pts = [(x, le[x] / y, None, None) for x, y, _, _ in pts_l if x in le]
                plot_series(ax, pts, ENCODING_COLOR[e], "-", ENCODING_MARKER[e], e, lw=1.3)
            ax.axhline(1, color=TEXT, linewidth=0.7)
            sel_axis(ax, sels)
            log_y(ax)
            ax.set_xlabel("selectivity" if i == 1 else "")
            qtxt = "SUM(c0..c3)" if q == "sum" else "SELECT c0..c3"
            ax.set_title(f"{d}: {qtxt}", loc="left", fontweight="bold")
            if j == 0:
                ax.set_ylabel("late speedup over early")
            style_axes(ax)
    fig.tight_layout(h_pad=1.0, w_pad=1.0)
    bottom_legend(fig, axes[0][0], y=0.0)
    save(fig, out, "enc_selectivity", png)

    encs = ["plain", "bitpack", "for", "dict", "dictb", "rle"]
    lines = ["\\begin{tabular}{ll" + "r" * len(encs) + "}", "\\toprule",
             "Data & Query & " + " & ".join(encs) + " \\\\", "\\midrule"]
    for d in dists:
        for q, qn in (("sum", "SUM"), ("projection", "SELECT")):
            cells = []
            for e in encs:
                a, b = table.get((d, q, e, "")), table.get((d, q, e, "_simd"))
                cells.append(fmt_crossover(a) + (" / " + fmt_crossover(b) if b else ""))
            lines.append(f"{d} & {qn} & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "enc_crossovers", "\n".join(lines) + "\n")


@figure
def fig_enc_workingset(R, out, gen, png):
    d = os.path.join(R.root, "enc_workingset")
    if not os.path.isdir(d):
        return "enc_workingset results missing"
    rows = {"": [], "_simd": []}
    for f in sorted(os.listdir(d)):
        if f.endswith(".csv"):
            rows["_simd" if f.endswith("_simd.csv") else ""] += R.rows("enc_workingset", f[:-4])
    if not rows[""]:
        return "enc_workingset results missing"
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.6, 2.5))
    size_of = lambda r: int(r["rows"]) * int(r["cols"]) * 8  # noqa: E731
    sizes = sorted({size_of(r) for r in rows[""]})
    def per_row(rs):
        """ns per row of the best strategy among rs."""
        best = min(statistics.median(int(r["latency_ns"]) for r in rs if r["strategy"] == st)
                   for st in {r["strategy"] for r in rs})
        return best / int(rs[0]["rows"])
    base = {s: per_row(rs) for s in sizes
            if (rs := [r for r in select(rows[""], layout="DSM") if size_of(r) == s])}
    plot_series(a, [(s, base[s], None, None) for s in sizes if s in base], LAYOUT_COLOR["DSM"],
                "-", "o", "DSM (uncompressed)")
    # Best of early (decode, then sum) and late (fused sum on the packed data) per point.
    for e in ("bitpack", "for", "dict", "dictb"):
        for simd, style, lw in (("", ":", 1.0), ("_simd", "-", 1.5)):
            pts, sp = [], []
            for s in sizes:
                rs = [r for r in select(rows[simd], layout=f"DSM-{e}-vector") if size_of(r) == s]
                if rs and s in base:
                    v = per_row(rs)
                    pts.append((s, v, None, None))
                    sp.append((s, base[s] / v, None, None))
            lab = e + (" (AVX2)" if simd else " (scalar)")
            if simd:
                plot_series(a, pts, ENCODING_COLOR[e], style, ENCODING_MARKER[e], lab, lw=lw)
            plot_series(b, sp, ENCODING_COLOR[e], style, ENCODING_MARKER[e], lab, lw=lw)
    b.axhline(1, color=TEXT, linewidth=0.7)
    ticks = [64 << 10, 1 << 20, 16 << 20, 256 << 20]
    for ax in (a, b):
        log2_axis(ax, ticks, [fmt_bytes(t) for t in ticks])
        ax.set_xlabel("uncompressed table size (8 columns)")
        style_axes(ax)
        cache_marks(ax, [L1, L2, L3])
    log_y(a)
    lo, hi = a.get_ylim()
    a.set_ylim(lo, hi * 1.3)  # room for the dict line at the top
    a.set_ylabel("ns per row, SUM of 8 columns")
    b.set_ylabel("speedup over uncompressed DSM")
    a.set_title("Scan cost (AVX2 decode)", loc="left", fontweight="bold")
    b.set_title("Speedup from compression", loc="left", fontweight="bold")
    fig.tight_layout(w_pad=2)
    h, l = b.get_legend_handles_labels()
    h0, l0 = a.get_legend_handles_labels()
    bottom_legend(fig, None, ncol=4, y=0.0, handles=h0[:1] + h, labels=l0[:1] + l)
    save(fig, out, "enc_workingset", png)


@figure
def fig_enc_dict(R, out, gen, png):
    """Dictionary codes bit-packed (dict) vs byte-aligned (dictb): SUM WHERE c0 < x, Zipf."""
    if not R.have("enc_dict", "encdict_zipf_simd"):
        return "enc_dict results missing"
    runs = {simd: R.rows("enc_dict", "encdict_zipf" + simd) for simd in ("", "_simd")
            if R.have("enc_dict", "encdict_zipf" + simd)}
    def best(rows, lay, k, sel):
        ms = [med_ms(rs) for st in ("early", "late")
              if (rs := [r for r in rows if r["layout"] == lay and r["strategy"] == st
                         and r["k"] == str(k) and float(r["sel"]) == sel])]
        return min(ms) if ms else None
    lays = [("DSM", "plain DSM", LAYOUT_COLOR["DSM"], "o"),
            ("DSM-dict-vector", "dict (bit-packed codes)", ENCODING_COLOR["dict"], "D"),
            ("DSM-dictb-vector", "dictb (byte-aligned codes)", ENCODING_COLOR["dictb"], "d")]
    rows = runs["_simd"]
    sels = sorted({float(r["sel"]) for r in rows if r["where"] != "-"})
    fig, axes = plt.subplots(1, 2, figsize=(6.6, 2.4))
    for ax, k in zip(axes, (1, 4)):
        for lay, lab, color, marker in lays:
            pts = [(x, y, None, None) for x in sels if (y := best(rows, lay, k, x)) is not None]
            plot_series(ax, pts, color, "-", marker, lab)
        sel_axis(ax, sels)
        log_y(ax)
        ax.set_ylabel("ms (best of early / late)" if k == 1 else "")
        q = "SUM(c0)" if k == 1 else "SUM(c0..c3)"
        ax.set_title(f"{q} WHERE c0 < x (AVX2)", loc="left", fontweight="bold")
        style_axes(ax)
    fig.tight_layout(w_pad=2)
    bottom_legend(fig, axes[0], ncol=3, y=0.0)
    save(fig, out, "enc_dict", png)

    lines = ["\\begin{tabular}{lrrrrrrrr}", "\\toprule",
             " & \\multicolumn{4}{c}{SUM(c0)} & \\multicolumn{4}{c}{SUM(c0..c3)} \\\\",
             "\\cmidrule(lr){2-5}\\cmidrule(lr){6-9}",
             "Selected & plain & dict & dictb & dictb/plain & plain & dict & dictb & dictb/plain \\\\",
             "\\midrule"]
    for x in sels:
        cells = []
        for k in (1, 4):
            p_, d_, b_ = (best(rows, lay, k, x) for lay, _, _, _ in lays)
            cells += [f"{p_:.2f}", f"{d_:.2f}", f"{b_:.2f}", f"{p_ / b_:.1f}$\\times$"]
        lines.append(pct(x) + " & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "enc_dict", "\n".join(lines) + "\n")


@figure
def fig_batch(R, out, gen, png):
    if not R.have("batch", "batch_proj"):
        return "batch results missing"
    rows = R.rows("batch", "batch_proj")
    fig, axes = plt.subplots(1, 3, figsize=(6.6, 2.5))
    ticks = [64, 256, 1024, 4096, 16384, 65536]
    for ax, sel in zip(axes[:2], ("0.1", "1")):
        for lay in ("DSM", "PAX"):
            for k, st in (("4", ":"), ("16", "-")):
                cand = [r for r in select(rows, layout=lay, k=k) if abs(float(r["sel"]) - float(sel)) < 0.002]
                pts = series(cand, "batch", xconv=int)
                plot_series(ax, pts, LAYOUT_COLOR[lay], st, LAYOUT_MARKER[lay], f"{lay}, k={k}", lw=1.3)
        log2_axis(ax, ticks, ["64", "256", "1K", "4K", "16K", "64K"])
        ax.set_xlabel("vector size (rows)")
        ax.set_title(f"Late projection, {float(sel) * 100:g}%", loc="left", fontweight="bold")
        ax.set_ylim(bottom=0)
        style_axes(ax)
    axes[0].set_ylabel("latency (ms)")
    h1, l1 = axes[0].get_legend_handles_labels()
    if R.have("batch", "batch_enc"):
        er = R.rows("batch", "batch_enc")
        ax = axes[2]
        for e in ("bitpack", "dict"):
            for q, st in (("sum", "-"), ("projection", ":")):
                pts = series(er, "batch", xconv=int, layout=f"DSM-{e}-vector", query=q)
                plot_series(ax, pts, ENCODING_COLOR[e], st, ENCODING_MARKER[e], f"{e}, {q}", lw=1.3)
        log2_axis(ax, ticks, ["64", "256", "1K", "4K", "16K", "64K"])
        ax.set_xlabel("vector size (rows)")
        ax.set_title("Decode 8 columns (N=8)", loc="left", fontweight="bold")
        ax.set_ylim(bottom=0)
        # 8 decoded vectors of 8 B values fill L1D / L2 at these sizes:
        cache_marks(ax, [L1 / 64, L2 / 64], ("L1D", "L2"))
        style_axes(ax)
        h2, l2 = ax.get_legend_handles_labels()
        h1, l1 = h1 + h2, l1 + l2
    fig.tight_layout(w_pad=1.2)
    bottom_legend(fig, None, ncol=4, y=0.0, handles=h1, labels=l1)
    save(fig, out, "batch", png)


@figure
def fig_dsm_knobs(R, out, gen, png):
    names = [("dsm_off0_4k", "column starts aligned, 4 KB pages", "#6f6e69", "--"),
             ("dsm_off0_huge", "aligned, 2 MB pages", LAYOUT_COLOR["DSM"], "--"),
             ("dsm_offauto_4k", "staggered starts, 4 KB pages", "#6f6e69", "-"),
             ("dsm_offauto_huge", "staggered, 2 MB pages", LAYOUT_COLOR["DSM"], "-")]
    names = [n for n in names if R.have("dsm_knobs", n[0])]
    if not names:
        return "dsm_knobs results missing"
    fig, axes = plt.subplots(1, 3, figsize=(6.6, 2.4))
    panels = [("projection", "early", "latency", "Projection: latency"),
              ("projection", "early", "l1d_misses", "Projection: L1D misses"),
              ("sum", "early", "latency", "SUM: latency")]
    for ax, (q, st, metric, title) in zip(axes, panels):
        for name, label, color, style in names:
            rows = R.rows("dsm_knobs", name)
            if metric != "latency" and metric not in rows[0]:
                continue
            pts = series(rows, "k", metric, xconv=int, query=q, strategy=st)
            plot_series(ax, pts, color, style, "o" if "4k" in name else "s", label, lw=1.3)
        ks = sorted({int(r["k"]) for r in R.rows("dsm_knobs", names[0][0])})
        ax.set_xticks(ks)
        ax.set_xlabel("k = columns read in lockstep")
        ax.set_ylabel("latency (ms)" if metric == "latency" else "L1D misses per row")
        ax.set_ylim(bottom=0)
        ax.axvline(8, color=CACHE_LINE_COLOR, linewidth=0.7, linestyle=(0, (2, 2)), zorder=0)
        ax.text(8, 0.97, " 8 ways", transform=ax.get_xaxis_transform(), fontsize=6.5,
                color=TEXT2, va="top")
        ax.set_title(title, loc="left", fontweight="bold")
        style_axes(ax)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, loc="lower center", ncol=4, fontsize=6.5,
               bbox_to_anchor=(0.5, -0.1))
    fig.suptitle("DSM, early (row at a time) over k columns: column start offsets x page size",
                 x=0.01, ha="left", fontweight="bold", fontsize=8.5)
    fig.tight_layout(w_pad=1.2)
    save(fig, out, "dsm_knobs", png)


@figure
def fig_aliasing(R, out, gen, png):
    """DSM SELECT * at 1% vs the stagger between column starts; one dot per process."""
    steps = [st for st in ("0", "64", "512", "1024", "1088", "2048", "3072", "4096", "8192",
                           "16384", "32768") if R.have("aliasing", f"alias_{st}_r0")]
    if not steps:
        return "aliasing results missing"
    per = {}  # (step, strategy) -> [(ms, l1d misses per row)] one per process
    for st in steps:
        for rep in range(10):
            if not R.have("aliasing", f"alias_{st}_r{rep}"):
                break
            rows = R.rows("aliasing", f"alias_{st}_r{rep}")
            for strat in ("early", "late"):
                rs = select(rows, strategy=strat)
                if rs:
                    per.setdefault((st, strat), []).append((med_ms(rs), per_row(rs, "l1d_misses")))
    fig, axes = plt.subplots(1, 2, figsize=(6.6, 2.4))
    labels = [("0 (default)" if st == "0" else "1088 (auto)" if st == "1088" else
               f"{int(st) // 1024} K" if int(st) % 1024 == 0 else st) for st in steps]
    for ax, idx, title in ((axes[0], 0, "latency (ms)"), (axes[1], 1, "L1D misses per row")):
        for strat, dx in (("early", -0.12), ("late", 0.12)):
            for i, st in enumerate(steps):
                vals = [v[idx] for v in per.get((st, strat), [])]
                ax.scatter([i + dx] * len(vals), vals, s=14, color=STRATEGY_COLOR[strat],
                           marker=STRATEGY_MARKER[strat], linewidths=0, alpha=0.8, zorder=3,
                           label=strat if i == 0 else None)
        ax.set_xticks(range(len(steps)), labels, rotation=35, ha="right", fontsize=6.8)
        ax.set_xlabel("stagger between consecutive column starts (bytes)")
        ax.set_title(title, loc="left", fontweight="bold")
        ax.set_ylim(bottom=0)
        style_axes(ax)
    nproc = max(len(v) for v in per.values())
    fig.suptitle(f"SELECT * WHERE c0 < x (1%) on DSM: one dot per process ({nproc} per stagger)", x=0.01,
                 ha="left", fontweight="bold", fontsize=8.5)
    fig.tight_layout()
    bottom_legend(fig, axes[0], ncol=2, y=0.0)
    save(fig, out, "aliasing", png)

    lines = ["\\begin{tabular}{lrrrr}", "\\toprule",
             "Stagger (B) & \\multicolumn{2}{c}{early (ms)} & \\multicolumn{2}{c}{late (ms)} \\\\",
             " & min & max & min & max \\\\", "\\midrule"]
    for st, lab in zip(steps, labels):
        e = [v[0] for v in per.get((st, "early"), [])]
        l = [v[0] for v in per.get((st, "late"), [])]
        lines.append(f"{lab} & {min(e):.1f} & {max(e):.1f} & {min(l):.1f} & {max(l):.1f} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "aliasing", "\n".join(lines) + "\n")


KNOB_COLOR = {"direct": "#6f6e69", "stage": "#eda100", "nt": "#008300"}
KNOB_MARKER = {"direct": "o", "stage": "s", "nt": "^"}
KNOB_LABEL = {"direct": "direct writes", "stage": "staged + memcpy", "nt": "staged + NT stores"}


@figure
def fig_output_knobs(R, out, gen, png):
    knobs = [k for k in ("direct", "stage", "nt") if R.have("output_knobs", f"out_{k}")]
    if len(knobs) < 2:
        return "output_knobs results missing"
    data = {k: R.rows("output_knobs", f"out_{k}") for k in knobs}
    fig, axes = plt.subplots(1, 3, figsize=(6.6, 2.5))
    for ax, lay in zip(axes, ("NSM", "DSM", "PAX")):
        for k in knobs:
            for st in ("early", "late"):
                pts = series(data[k], "sel", query="projection", k="16", layout=lay, strategy=st)
                plot_series(ax, pts, KNOB_COLOR[k], STRATEGY_STYLE[st], KNOB_MARKER[k],
                            f"{st}, {KNOB_LABEL[k]}", lw=1.3)
        sels = sorted({float(r["sel"]) for r in data[knobs[0]]})
        sel_axis(ax, sels)
        ax.set_xlabel("selectivity")
        log_y(ax)
        ax.set_title(lay, loc="left", fontweight="bold")
        style_axes(ax)
    axes[0].set_ylabel("latency (ms)")
    fig.suptitle("SELECT * WHERE c0 < x (k = 16): how the output tuples are written", x=0.01,
                 ha="left", fontweight="bold", fontsize=8.5)
    fig.tight_layout(w_pad=1.2)
    bottom_legend(fig, axes[0], ncol=3, y=0.0)
    save(fig, out, "output_knobs", png)

    # Table: latency at 100% selectivity per write mode, k = 4 and 16, and the NT speedup.
    lines = ["\\begin{tabular}{lll" + "r" * (len(knobs) + 1) + "}", "\\toprule",
             "$k$ & Layout & Strategy & " + " & ".join(KNOB_LABEL[k] for k in knobs) +
             " & NT speedup \\\\", "\\midrule"]
    for kk in ("4", "16"):
        for lay in ("NSM", "DSM", "PAX"):
            for st in ("early", "late"):
                lat = {}
                for k in knobs:
                    rs = [r for r in select(data[k], query="projection", k=kk, layout=lay, strategy=st)
                          if float(r["sel"]) > 0.999]
                    lat[k] = med_ms(rs) if rs else None
                cells = [f"{lat[k]:.1f}" if lat[k] else "--" for k in knobs]
                sp = f"{lat['direct'] / lat['nt']:.2f}$\\times$" if lat.get("direct") and lat.get("nt") else "--"
                lines.append(f"{kk} & {lay} & {st} & " + " & ".join(cells) + f" & {sp} \\\\")
        if kk == "4":
            lines.append("\\midrule")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "output_knobs", "\n".join(lines) + "\n")


@figure
def fig_simd(R, out, gen, png):
    tags = [t for t in ("dram", "l3", "l2") if R.have("simd", f"simd_{t}_off") and R.have("simd", f"simd_{t}_on")]
    if not tags:
        return "simd results missing"
    fig, axes = plt.subplots(2, len(tags), figsize=(6.6, 4.2), squeeze=False)
    titles = {"dram": "DRAM: 4M rows", "l3": "L3: 64K rows", "l2": "L2: 2K rows"}
    for j, t in enumerate(tags):
        off, on = R.rows("simd", f"simd_{t}_off"), R.rows("simd", f"simd_{t}_on")
        for i, q in enumerate(("sum", "projection")):
            ax = axes[i][j]
            def ns_row(rows, st):
                pts = series(rows, "sel", query=q, layout="DSM", strategy=st)
                n = int(rows[0]["rows"])
                return [(x, y * 1e6 / n, None, None) for x, y, *_ in pts if x > 0]
            for st in ("early", "early-pipe", "late-branch", "late"):
                plot_series(ax, ns_row(off, st), STRATEGY_COLOR[st], STRATEGY_STYLE[st],
                            STRATEGY_MARKER[st], st, lw=1.1)
            plot_series(ax, ns_row(on, "late"), STRATEGY_COLOR["auto"], "-", "*",
                        "late, AVX2 selection", lw=1.6)
            sel_axis(ax, sorted({float(r["sel"]) for r in off if float(r["sel"]) > 0}))
            ax.set_xlabel("selectivity" if i == 1 else "")
            log_y(ax)
            if i == 0:
                ax.set_title(titles[t], loc="left", fontweight="bold")
            if j == 0:
                ax.set_ylabel(("SUM(c0..c3)" if q == "sum" else "SELECT c0..c3") + "\nns per row")
            style_axes(ax)
    fig.suptitle("DSM, k = 4 of 16 attributes, WHERE c0 < x", x=0.01, ha="left",
                 fontweight="bold", fontsize=8.5)
    fig.tight_layout(h_pad=1.0, w_pad=1.0)
    bottom_legend(fig, axes[0][0], ncol=5, y=0.0)
    save(fig, out, "simd", png)

    # Crossovers of late (scalar / AVX2) against early, per level and layout.
    lines = ["\\begin{tabular}{llrr}", "\\toprule",
             "Data in & Query & late vs early & late (AVX2) vs early \\\\", "\\midrule"]
    for t in tags:
        off, on = R.rows("simd", f"simd_{t}_off"), R.rows("simd", f"simd_{t}_on")
        for q in ("sum", "projection"):
            a = crossover([r for r in off if r["query"] == q], "DSM")
            # AVX2 late against scalar early (early does not use the selection vector).
            mixed = [r for r in on if r["query"] == q and r["strategy"] == "late"] + \
                    [r for r in off if r["query"] == q and r["strategy"] == "early"]
            b = crossover(mixed, "DSM")
            lines.append(f"{t.upper()} & {'SUM' if q == 'sum' else 'SELECT'} & {fmt_crossover(a)} & "
                         f"{fmt_crossover(b)} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "simd_crossovers", "\n".join(lines) + "\n")


@figure
def fig_auto(R, out, gen, png):
    specs = [("projection", 4), ("projection", 16), ("sum", 4)]
    sets = [("uniform", "uniform, DRAM", lambda q, k, m: f"auto_{q}_k{k}_uniform{m}"),
            ("sorted", "sorted, DRAM", lambda q, k, m: f"auto_{q}_k{k}_sorted{m}"),
            ("cache", "uniform, L3", lambda q, k, m: f"auto_cache_{q}_k{k}{m}")]
    sets = [st for st in sets if all(R.have("auto", st[2](q, k, "")) for q, k in specs)]
    if not sets:
        return "auto results missing"
    have_measure = all(R.have("auto", st[2](q, k, "_measure")) for st in sets for q, k in specs)
    modes = [("", "--", "model threshold")] + ([("_measure", "-", "measured")] if have_measure else [])
    fig, axes = plt.subplots(len(sets), 3, figsize=(6.6, 1.9 * len(sets) + 0.5), squeeze=False)
    summary = defaultdict(list)  # (set, layout, column) -> ratios to the best fixed strategy

    def med(rows, lay, st, sv):
        return med_ms([r for r in rows if r["layout"] == lay and r["strategy"] == st
                       and float(r["sel"]) == sv])

    for i, (key, title, name) in enumerate(sets):
        for j, (q, k) in enumerate(specs):
            ax = axes[i][j]
            for suffix, style, _ in modes:
                rows = R.rows("auto", name(q, k, suffix))
                sels = sorted({float(r["sel"]) for r in rows if float(r["sel"]) > 0})
                for lay in ("NSM", "DSM", "PAX"):
                    pts = []
                    for sv in sels:
                        e, l, a = (med(rows, lay, st, sv) for st in ("early", "late", "auto"))
                        best = min(e, l)
                        pts.append((sv, a / best, None, None))
                        summary[(key, lay, "auto" + suffix)].append(a / best)
                        if suffix == "":
                            summary[(key, lay, "early")].append(e / best)
                            summary[(key, lay, "late")].append(l / best)
                    plot_series(ax, pts, LAYOUT_COLOR[lay], style, LAYOUT_MARKER[lay],
                                f"{lay}" if suffix == modes[-1][0] else None,
                                lw=1.4 if suffix else 1.0)
                if suffix == "":
                    sel_axis(ax, sels)
            ax.axhline(1, color=TEXT, linewidth=0.7)
            ax.set_xlabel("selectivity" if i == len(sets) - 1 else "")
            ax.set_ylim(0.6, 2.6)
            if j == 0:
                ax.set_ylabel(f"{title}\nauto / best fixed")
            if i == 0:
                qtxt = f"SELECT c0..c{k - 1}" if q == "projection" else f"SUM(c0..c{k - 1})"
                ax.set_title(qtxt, loc="left", fontweight="bold")
            style_axes(ax)
    fig.tight_layout(h_pad=0.8, w_pad=1.0)
    handles, labels = axes[0][0].get_legend_handles_labels()
    handles += [Line2D([], [], color=TEXT2, linestyle=st, linewidth=1.2, label=lab) for _, st, lab in modes]
    bottom_legend(fig, None, ncol=5, y=0.0, handles=handles, labels=[h.get_label() for h in handles])
    save(fig, out, "auto", png)

    # Geometric-mean slowdown against the better of early / late at each selectivity.
    gm = lambda v: math.exp(sum(math.log(x) for x in v) / len(v))  # noqa: E731
    cols = ["early", "late", "auto"] + (["auto_measure"] if have_measure else [])
    head = ["always early", "always late", "auto (model)"] + (["auto (measured)"] if have_measure else [])
    lines = ["\\begin{tabular}{ll" + "r" * len(cols) + "}", "\\toprule",
             "Data & Layout & " + " & ".join(head) + " \\\\", "\\midrule"]
    for n, (key, title, _) in enumerate(sets):
        for lay in ("NSM", "DSM", "PAX"):
            vals = [gm(summary[(key, lay, c)]) for c in cols]
            lines.append(f"{title if lay == 'NSM' else ''} & {lay} & " +
                         " & ".join(f"{v:.2f}" for v in vals) + " \\\\")
        if n < len(sets) - 1:
            lines.append("\\midrule")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "auto_regret", "\n".join(lines) + "\n")


@figure
def fig_predicates(R, out, gen, png):
    preds = [("eq1", "c0 = 42"), ("eq2", "c0 = 42 AND c1 = 7"), ("range1", "c2 BETWEEN 10 AND 29"),
             ("range2", "c2 BETWEEN 10 AND 29 AND c5 < 50"), ("eq_range", "c3 = 5 AND c6 BETWEEN 0 AND 49")]
    preds = [p for p in preds if R.have("predicates", f"pred_{p[0]}")]
    if not preds:
        return "predicates results missing"
    lines = ["\\begin{tabular}{lr" + "l" * 6 + "}", "\\toprule",
             "Predicate & sel. & \\multicolumn{3}{c}{SELECT * (16 attributes)} & "
             "\\multicolumn{3}{c}{SUM of 4 attributes} \\\\",
             " & & NSM & DSM & PAX & NSM & DSM & PAX \\\\", "\\midrule"]
    abbrev = {"early": "E", "early-pipe": "EP", "late": "L", "late-branch": "LB"}
    for name, text in preds:
        rows = R.rows("predicates", f"pred_{name}")
        sel = float(rows[0]["sel"]) if rows[0]["sel"] not in ("", "1") else None
        cnt = int(rows[0]["rows_out"]) / int(rows[0]["rows"])
        cells = []
        for q, k in (("projection", "16"), ("sum", "4")):
            for lay in ("NSM", "DSM", "PAX"):
                best = min(((med_ms(rs), st) for st in STRATEGY_COLOR
                            if (rs := select(rows, query=q, k=k, layout=lay, strategy=st))), default=None)
                cells.append(f"{best[0]:.1f} ({abbrev.get(best[1], best[1])})" if best else "--")
        lines.append(f"\\code{{{text}}} & {cnt * 100:.2g}\\% & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "predicates", "\n".join(lines) + "\n")
    return None


@figure
def fig_tuned(R, out, gen, png):
    queries = [("sum_k1", "SUM(c0)"), ("sum_k16", "SUM(c0..c15)"), ("proj_k16", "SELECT *"),
               ("proj_k4_s1", "SELECT c0..c3, 1%"), ("proj_k16_s10", "SELECT *, 10%"),
               ("proj_k16_s50", "SELECT *, 50%")]
    queries = [q for q in queries if R.have("tuned", f"untuned_{q[0]}") and R.have("tuned", f"tuned_{q[0]}")]
    if not queries:
        return "tuned results missing"
    fig, ax = plt.subplots(figsize=(6.6, 2.5))
    width = 0.13
    table = []
    for qi, (qn, label) in enumerate(queries):
        un, tu = R.rows("tuned", f"untuned_{qn}"), R.rows("tuned", f"tuned_{qn}")
        best_un = {}
        for li, lay in enumerate(("NSM", "DSM", "PAX")):
            u = min(((med_ms(rs), st) for st in STRATEGY_COLOR if (rs := select(un, layout=lay, strategy=st))))
            t = min(((med_ms(rs), st) for st in STRATEGY_COLOR if (rs := select(tu, layout=lay, strategy=st))))
            ta = med_ms(select(tu, layout=lay, strategy="auto")) if select(tu, layout=lay, strategy="auto") else None
            best_un[lay] = u
            x = qi + (li - 1) * 2 * width
            ax.bar(x - width / 2, u[0], width * 0.92, color=LAYOUT_COLOR[lay], alpha=0.35,
                   label=f"{lay} untuned" if qi == 0 else None)
            ax.bar(x + width / 2, t[0], width * 0.92, color=LAYOUT_COLOR[lay],
                   label=f"{lay} tuned" if qi == 0 else None)
            table.append((label, lay, u, t, ta))
    ax.set_xticks(range(len(queries)), [l for _, l in queries], fontsize=6.8)
    log_y(ax)
    ax.set_ylabel("latency (ms, best strategy)")
    ax.set_title("Untuned defaults vs machine-derived knobs (4M rows x 16 columns, from DRAM)",
                 loc="left", fontweight="bold")
    style_axes(ax)
    fig.tight_layout()
    bottom_legend(fig, ax, ncol=6, y=0.0)
    save(fig, out, "tuned", png)

    # Speedups against the best untuned fixed strategy: of the best tuned run (an oracle that
    # knows which strategy to pick), and of the tuned auto strategy (what an engine would run).
    lines = ["\\begin{tabular}{llrlrlrrr}", "\\toprule",
             "& & \\multicolumn{2}{c}{untuned} & \\multicolumn{3}{c}{tuned} & "
             "\\multicolumn{2}{c}{speedup} \\\\",
             "Query & Layout & ms & best strategy & ms & best strategy & auto & best & auto \\\\",
             "\\midrule"]
    for label, lay, u, t, ta in table:
        auto = (f"{ta:.1f}", f"{u[0] / ta:.2f}$\\times$") if ta is not None else ("--", "--")
        lines.append(f"{label.replace('%', '\\%')} & {lay} & {u[0]:.1f} & {u[1]} & {t[0]:.1f} & {t[1]} & "
                     f"{auto[0]} & {u[0] / t[0]:.2f}$\\times$ & {auto[1]} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    write_tex(gen, "tuned", "\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(ROOT, "results"))
    ap.add_argument("--out", default=os.path.join(ROOT, "report", "figures"))
    ap.add_argument("--gen", default=os.path.join(ROOT, "report", "generated"))
    ap.add_argument("--only", default="")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    R = Results(a.results)
    only = {s for s in a.only.split(",") if s}
    for fn in FIGURES:
        name = fn.__name__[4:]
        if only and name not in only:
            continue
        try:
            msg = fn(R, a.out, a.gen, a.png)
        except Exception as e:  # keep going: one broken figure should not stop the rest
            import traceback
            traceback.print_exc()
            msg = f"error: {e}"
        if msg:
            print(f"  skip {name}: {msg}")


if __name__ == "__main__":
    main()
