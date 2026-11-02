#!/usr/bin/env python3
"""Plots and summarises the working-set-size sweep produced by scripts/run_working_set.sh.

Usage: scripts/plot_working_set.py [results/working_set]

Reads <dir>/raw.csv (one row per timed sample) and <dir>/machine.txt (cache sizes), and writes:
  <dir>/summary.csv      median and quartiles of every metric per (layout, size, k, mode)
  <dir>/winners.md       fastest layout per (size, k, mode): the crossover table
  <dir>/plots/*.png      the figures described in FIGURES below
Counter figures are skipped when the sweep ran without hardware counters.
"""

import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter

# Fixed layout -> colour mapping (first three slots of the validated categorical palette), so a
# layout keeps its colour in every figure regardless of which series are present.
COLORS = {"NSM": "#2a78d6", "DSM": "#eb6834", "PAX": "#1baf7a"}
MARKERS = {"NSM": "o", "DSM": "s", "PAX": "^"}
LAYOUTS = ["NSM", "DSM", "PAX"]
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SURFACE = "#ffffff"

PANEL_KS = [1, 4, 16]                       # projectivity panels in the size-sweep figures
PROJECTIVITY_SIZES = ["256K", "8M", "256M"]  # in L2, in L3, in DRAM (for 16 x int64 rows)

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK_2,
    "axes.titlecolor": INK,
    "axes.titlesize": 11,
    "axes.labelsize": 10,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "grid.linestyle": "-",
    "xtick.color": INK_2,
    "ytick.color": INK_2,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "font.size": 10,
    "legend.frameon": False,
    "savefig.dpi": 200,
})


def fmt_bytes(b):
    for unit, shift in (("G", 30), ("M", 20), ("K", 10)):
        if b >= 1 << shift:
            v = b / (1 << shift)
            return f"{v:g}{unit}"
    return f"{b:g}B"


def parse_size(s):
    m = re.fullmatch(r"(\d+)([KMG]?)", s.upper())
    return int(m.group(1)) << {"": 0, "K": 10, "M": 20, "G": 30}[m.group(2)]


def read_caches(machine_txt):
    """[(label, bytes)] for data/unified caches, from the 'cache:' lines of machine.txt."""
    caches = []
    if machine_txt.exists():
        for line in machine_txt.read_text().splitlines():
            m = re.match(r"cache: (L\d) (Data|Unified) (\d+)([KMG]?)", line)
            if m:
                caches.append((m.group(1), parse_size(m.group(3) + m.group(4))))
    return caches


def load(raw_csv):
    df = pd.read_csv(raw_csv)
    df["ns_per_row"] = df.latency_ns / df.rows
    df["useful_gbps"] = df.rows * df.k * 8 / df.latency_ns  # bytes per ns == GB/s
    for c in ("llc_misses", "llc_refs", "l1d_misses", "branch_misses", "dram_read_bytes",
              "dram_write_bytes"):
        if c in df and df[c].notna().any():
            df[c + "_per_row"] = df[c] / df.rows
    if "instructions" in df and df.instructions.notna().any():
        df["ipc"] = df.instructions / df.cycles
    return df


def summarise(df):
    metrics = [c for c in df.columns if c.endswith("_per_row") or c in ("useful_gbps", "ipc")]
    keys = ["layout", "data_bytes", "rows", "k", "mode"]
    g = df.groupby(keys)
    out = g[["latency_ns"] + metrics].median().add_suffix("_median")
    q = g[["latency_ns", "ns_per_row", "useful_gbps"]]
    out = out.join(q.quantile(0.25).add_suffix("_q25")).join(q.quantile(0.75).add_suffix("_q75"))
    return out.reset_index()


# ---------------------------------------------------------------- figure helpers

def setup_size_axis(ax, sizes):
    ax.set_xscale("log", base=2)
    major = [s for s in sizes if round(np.log2(s)) % 2 == 0]
    ax.xaxis.set_major_locator(FixedLocator(major))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_bytes(v)))
    ax.xaxis.set_minor_locator(FixedLocator(sizes))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlim(min(sizes) / 1.3, max(sizes) * 1.3)


def draw_caches(ax, caches):
    """Hairline marker at each cache capacity, labelled along the top edge."""
    for label, size in caches:
        ax.axvline(size, color=INK_2, linewidth=0.8, alpha=0.6, zorder=1)
        ax.annotate(label, xy=(size, 1), xycoords=("data", "axes fraction"),
                    xytext=(3, -3), textcoords="offset points", ha="left", va="top",
                    fontsize=8, color=INK_2)


def plot_series(ax, d, x, y, layout, band=None):
    d = d.sort_values(x)
    ax.plot(d[x], d[y], color=COLORS[layout], linewidth=2, marker=MARKERS[layout],
            markersize=5.5, markeredgecolor=SURFACE, markeredgewidth=1.2,
            solid_joinstyle="round", solid_capstyle="round", label=layout, zorder=3)
    if band is not None:
        lo, hi = band
        ax.fill_between(d[x], d[lo], d[hi], color=COLORS[layout], alpha=0.12, linewidth=0,
                        zorder=2)


def direct_labels(ax, ends):
    """Label each series just right of its last point, nudged apart so labels never overlap.

    ends: [(layout, x_last, y_last)]"""
    if not ends:
        return
    ax.figure.canvas.draw()  # labels are placed in axes coordinates, so limits must be final
    to_axes = ax.transAxes.inverted()
    pts = []
    for layout, x, y in ends:
        _, ya = to_axes.transform(ax.transData.transform((x, y)))
        pts.append([ya, layout, x])
    pts.sort()
    gap = 0.07
    for i in range(1, len(pts)):
        pts[i][0] = max(pts[i][0], pts[i - 1][0] + gap)
    for ya, layout, x in pts:
        ax.annotate(layout, xy=(x, ya), xycoords=("data", "axes fraction"),
                    xytext=(8, 0), textcoords="offset points",
                    ha="left", va="center", fontsize=9, color=INK, annotation_clip=False)


def k_title(k, cols):
    return f"k = {k} of {cols} columns ({100 * k / cols:.0f}%)"


def size_sweep(summary, metric, ylabel, title, path, caches, modes, cols, logy=False,
               band=True, ref=None, layouts=LAYOUTS):
    """Small multiples: one row per mode, one column per k; x = working-set size."""
    ks = [k for k in PANEL_KS if k in set(summary.k)]
    modes = [m for m in modes if m in set(summary["mode"])]
    sizes = sorted(summary.data_bytes.unique())
    fig, axes = plt.subplots(len(modes), len(ks), figsize=(4.2 * len(ks), 3.3 * len(modes)),
                             sharex=True, sharey=True, squeeze=False)
    pending_labels = []
    for r, mode in enumerate(modes):
        for c, k in enumerate(ks):
            ax = axes[r][c]
            setup_size_axis(ax, sizes)
            if logy:
                ax.set_yscale("log")
            ends = []
            for layout in layouts:
                d = summary[(summary.layout == layout) & (summary.k == k) &
                            (summary["mode"] == mode)]
                if d.empty or d[metric].isna().all():
                    continue
                b = None
                lo, hi = metric.replace("_median", "_q25"), metric.replace("_median", "_q75")
                if band and lo in d and hi in d:
                    b = (lo, hi)
                plot_series(ax, d, "data_bytes", metric, layout, b)
                last = d.sort_values("data_bytes").iloc[-1]
                ends.append((layout, last.data_bytes, last[metric]))
            if ref is not None:
                ref(ax, k)
            draw_caches(ax, caches)
            ax.set_title(k_title(k, cols) + (f" · {mode} cache" if len(modes) > 1 else ""),
                         loc="left")
            if r == len(modes) - 1:
                ax.set_xlabel("working-set size (table bytes)")
            if c == 0:
                ax.set_ylabel(ylabel)
            if c == len(ks) - 1:
                pending_labels.append((ax, ends))
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), bbox_to_anchor=(0.99, 1.0))
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 0.97, 0.95))
    for ax, ends in pending_labels:
        direct_labels(ax, ends)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


# ---------------------------------------------------------------- main

def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results/working_set")
    df = load(out_dir / "raw.csv")
    if "predicate" in df:  # the working-set figures are about the unfiltered scan
        df = df[df.predicate == "none"]
    caches = read_caches(out_dir / "machine.txt")
    cols = int(df.cols.iloc[0])
    plots = out_dir / "plots"
    plots.mkdir(parents=True, exist_ok=True)

    summary = summarise(df)
    summary.to_csv(out_dir / "summary.csv", index=False)
    print(f"  wrote {out_dir / 'summary.csv'}")
    modes = ["warm", "cold"]

    size_sweep(summary, "useful_gbps_median", "useful GB/s  (k·8 B per row / latency)",
               "Scan throughput vs working-set size", plots / "ws_throughput.png",
               caches, modes, cols)
    size_sweep(summary, "ns_per_row_median", "latency per row (ns, log)",
               "Scan latency per row vs working-set size", plots / "ws_latency_per_row.png",
               caches, modes, cols, logy=True)

    # Latency relative to DSM: values > 1 mean slower than DSM.
    dsm = summary[summary.layout == "DSM"].set_index(["data_bytes", "k", "mode"]).ns_per_row_median
    rel = summary.join(dsm.rename("dsm_ns"), on=["data_bytes", "k", "mode"])
    rel["vs_dsm_median"] = rel.ns_per_row_median / rel.dsm_ns

    def dsm_ref(ax, _k):
        ax.axhline(1.0, color=COLORS["DSM"], linewidth=1.2, zorder=2)
        ax.annotate("DSM = 1", xy=(0, 1.0), xycoords=("axes fraction", "data"),
                    xytext=(4, 3), textcoords="offset points", fontsize=8, color=INK_2)

    size_sweep(rel, "vs_dsm_median", "latency ÷ DSM latency (log)",
               "Latency relative to DSM (above 1 = slower than DSM)",
               plots / "ws_relative_to_dsm.png", caches, modes, cols, logy=True, band=False,
               ref=dsm_ref, layouts=["NSM", "PAX"])

    # Cold-cache penalty: cold latency / warm latency.
    if {"warm", "cold"} <= set(summary["mode"]):
        w = summary[summary["mode"] == "warm"].set_index(["layout", "data_bytes", "k"])
        c = summary[summary["mode"] == "cold"].set_index(["layout", "data_bytes", "k"])
        pen = (c.ns_per_row_median / w.ns_per_row_median).rename("cold_penalty_median")
        pen = pen.reset_index()
        pen["mode"] = "warm"

        def one_ref(ax, _k):
            ax.axhline(1.0, color=INK_2, linewidth=0.8)

        size_sweep(pen, "cold_penalty_median", "cold latency ÷ warm latency",
                   "Cold-cache penalty: how much each layout gains from a warm cache",
                   plots / "ws_cold_penalty.png", caches, ["warm"], cols, band=False,
                   ref=one_ref)

    if "llc_misses_per_row_median" in summary and summary.llc_misses_per_row_median.notna().any():
        size_sweep(summary, "llc_misses_per_row_median", "LLC misses per row",
                   "Last-level-cache misses per row", plots / "ws_llc_misses.png",
                   caches, modes, cols, band=False)
    else:
        print("  skipped ws_llc_misses.png (no llc_misses data)")

    if "dram_read_bytes_per_row_median" in summary and \
            summary.dram_read_bytes_per_row_median.notna().any():
        row_bytes = cols * 8

        def bytes_ref(ax, k):
            refs = [(k * 8, f"useful = {k * 8} B"), (row_bytes, f"full row = {row_bytes} B")]
            if k * 8 == row_bytes:
                refs = [(row_bytes, f"useful = full row = {row_bytes} B")]
            for y, label in refs:
                ax.axhline(y, color=INK_2, linewidth=0.8, alpha=0.7)
                ax.annotate(label, xy=(0, y), xycoords=("axes fraction", "data"),
                            xytext=(4, 3), textcoords="offset points", fontsize=8, color=INK_2)

        size_sweep(summary, "dram_read_bytes_per_row_median", "DRAM bytes read per row",
                   "Memory traffic per row (IMC counters, system-wide)",
                   plots / "ws_dram_bytes.png", caches, modes, cols, band=False, ref=bytes_ref)
    else:
        print("  skipped ws_dram_bytes.png (no dram_read_bytes data)")

    projectivity(summary, plots / "projectivity_by_size.png", cols)
    winners(summary, out_dir / "winners.md")


def projectivity(summary, path, cols):
    """Latency per row vs k at a few fixed working-set sizes (warm cache)."""
    want = [parse_size(s) for s in PROJECTIVITY_SIZES]
    have = sorted(summary.data_bytes.unique())
    sizes = [min(have, key=lambda h: abs(np.log2(h) - np.log2(w))) for w in want]
    warm = summary[summary["mode"] == "warm"]
    ks = sorted(warm.k.unique())
    fig, axes = plt.subplots(1, len(sizes), figsize=(4.2 * len(sizes), 3.4), sharey=True,
                             squeeze=False)
    where = {0: "fits in L2", 1: "fits in L3", 2: "DRAM-resident"}
    for i, size in enumerate(sizes):
        ax = axes[0][i]
        ax.set_xscale("log", base=2)
        ax.xaxis.set_major_locator(FixedLocator(ks))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_xlim(min(ks) / 1.3, max(ks) * 1.3)
        ends = []
        for layout in LAYOUTS:
            d = warm[(warm.layout == layout) & (warm.data_bytes == size)]
            if d.empty:
                continue
            plot_series(ax, d, "k", "ns_per_row_median", layout,
                        ("ns_per_row_q25", "ns_per_row_q75"))
            last = d.sort_values("k").iloc[-1]
            ends.append((layout, last.k, last.ns_per_row_median))
        ax.set_title(f"{fmt_bytes(size)} table · {where.get(i, '')}", loc="left")
        ax.set_xlabel(f"columns summed, k (of {cols})")
        if i == 0:
            ax.set_ylabel("latency per row (ns)")
        if i == len(sizes) - 1:
            last_ends = (ax, ends)
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), bbox_to_anchor=(0.99, 1.0))
    fig.suptitle("Projectivity crossover at different working-set sizes (warm cache)",
                 x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 0.97, 0.93))
    direct_labels(*last_ends)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def winners(summary, path):
    """Markdown tables: fastest layout per (size, k), with its margin over the runner-up."""
    lines = ["# Fastest layout per working-set size and k",
             "",
             "Each cell: winner (runner-up latency ÷ winner latency). Medians of the timed "
             "samples.", ""]
    for mode in ("warm", "cold"):
        m = summary[summary["mode"] == mode]
        if m.empty:
            continue
        ks = sorted(m.k.unique())
        lines += [f"## {mode} cache", "",
                  "| size | " + " | ".join(f"k={k}" for k in ks) + " |",
                  "|---|" + "---|" * len(ks)]
        for size in sorted(m.data_bytes.unique()):
            cells = []
            for k in ks:
                d = m[(m.data_bytes == size) & (m.k == k)].sort_values("ns_per_row_median")
                if len(d) < 2:
                    cells.append("-")
                    continue
                best, second = d.iloc[0], d.iloc[1]
                cells.append(f"{best.layout} ({second.ns_per_row_median / best.ns_per_row_median:.2f}×)")
            lines.append(f"| {fmt_bytes(size)} | " + " | ".join(cells) + " |")
        lines.append("")
    path.write_text("\n".join(lines))
    print(f"  wrote {path}")


if __name__ == "__main__":
    main()
