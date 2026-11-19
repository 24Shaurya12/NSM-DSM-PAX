#!/usr/bin/env python3
"""Summarise encoded-DSM runs across data distributions into one figure.

Usage: compare_encodings.py OUT_STEM RESULTS.csv [RESULTS.csv ...]

Each RESULTS.csv is one `scan_bench --encoding ... --layout dsm --dist D` run of a single
query (one per distribution). For every distribution and encoding, at k = N (all
columns read), it compares against uncompressed DSM:
  compression ratio  DSM footprint / encoded footprint
  speedup            DSM median latency / encoded median latency, per scan mode
Writes OUT_STEM.png (three panels: compression, vector-mode speedup, value-mode speedup;
bars grow up or down from 1x on a log scale), OUT_STEM_size.png (encoded size in MB per
distribution and encoding, against uncompressed DSM) and OUT_STEM.csv with the numbers.
"""
import csv
import statistics
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

from plot import ENCODING_COLORS, GRID, MUTED, SURFACE, TEXT  # noqa: E402

DIST_ORDER = ["uniform", "wide", "sorted", "runs", "zipf"]
MODES = ["vector", "value"]


def load(path):
    """Returns (dist, query, rows, cols, {layout: (median_ns, footprint_bytes)}) at k = N."""
    lat = defaultdict(list)
    footprint = {}
    with open(path) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"compare_encodings.py: {path} is empty")
    cols = int(rows[0]["cols"])
    for r in rows:
        if int(r["k"]) != cols or r.get("where", "-") != "-":
            continue
        # One series per (layout, strategy); each layout is then represented by its
        # fastest strategy (vector mode: late = column at a time; value mode: early only).
        lat[(r["layout"], r.get("strategy", "-"))].append(int(r["latency_ns"]))
        footprint[r["layout"]] = int(r["footprint_bytes"])
    if not any(l == "DSM" for l, _ in lat):
        sys.exit(f"compare_encodings.py: {path} has no uncompressed DSM run at k = {cols}; "
                 "run scan_bench with --layout dsm")
    stats = {}
    for (l, _), v in lat.items():
        m = statistics.median(v)
        if l not in stats or m < stats[l][0]:
            stats[l] = (m, footprint[l])
    return rows[0]["dist"], rows[0]["query"], int(rows[0]["rows"]), cols, stats


def compute(paths):
    """Returns (query, rows, cols, dists, table) where table[(dist, enc)] is a dict of
    ratio and speedup_<mode>."""
    table = {}
    dists, meta = [], set()
    for path in paths:
        dist, query, rows, cols, stats = load(path)
        meta.add((query, rows, cols))
        dists.append(dist)
        base_ns, base_bytes = stats["DSM"]
        for enc in ENCODING_COLORS:
            entry = {}
            for mode in MODES:
                layout = f"DSM-{enc}-{mode}"
                if layout in stats:
                    ns, nbytes = stats[layout]
                    entry["ratio"] = base_bytes / nbytes
                    entry["mb"] = nbytes / 2**20
                    entry[f"speedup_{mode}"] = base_ns / ns
            if entry:
                table[(dist, enc)] = entry
    if len(meta) != 1:
        sys.exit(f"compare_encodings.py: inputs mix queries or table sizes: {sorted(meta)}")
    dists.sort(key=lambda d: DIST_ORDER.index(d) if d in DIST_ORDER else len(DIST_ORDER))
    query, rows, cols = meta.pop()
    return query, rows, cols, dists, table


def log_ticks(lo, hi):
    """1-2-5 ticks covering [lo, hi], always including 1."""
    ticks = []
    for exp in range(-4, 5):
        for m in (1, 2, 5):
            ticks.append(m * 10.0 ** exp)
    return [t for t in ticks if lo <= t <= hi]


def fmt_x(v, _):
    return f"{v:g}×"


def bar_panel(ax, dists, encs, values, title, ylim):
    """Grouped bars per distribution, each growing from 1x to its value."""
    n = len(encs)
    slot = 0.8 / n
    width = slot * 0.82  # leftover is the surface gap between neighbouring bars
    for i, enc in enumerate(encs):
        xs, heights, bottoms = [], [], []
        for d_i, dist in enumerate(dists):
            v = values.get((dist, enc))
            if v is None:
                continue
            xs.append(d_i - 0.4 + slot * (i + 0.5))
            bottoms.append(min(1.0, v))
            heights.append(abs(v - 1.0) if v != 1.0 else 0.0)
        ax.bar(xs, heights, bottom=bottoms, width=width, color=ENCODING_COLORS[enc],
               label=enc, linewidth=0)
    ax.axhline(1.0, color=TEXT, linewidth=1)
    ax.set_yscale("log")
    ax.set_ylim(*ylim)
    ax.yaxis.set_major_locator(FixedLocator(log_ticks(*ylim)))
    ax.yaxis.set_major_formatter(FuncFormatter(fmt_x))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_xticks(range(len(dists)), dists)
    ax.set_xlim(-0.5, len(dists) - 0.5)
    ax.set_title(title, loc="left", color=TEXT, fontsize=10, fontweight="bold")
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelcolor=TEXT)


def size_chart(path, query, rows, cols, dists, encs, table):
    """Grouped bars of encoded size (MB) from 0, with uncompressed DSM as a reference line.
    The smallest encoding in each distribution gets a value label. plain is left out: it
    is uncompressed by definition, so it would only repeat the reference line."""
    base_mb = rows * cols * 8 / 2**20
    encs = [e for e in encs if e != "plain"]
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    n = len(encs)
    slot = 0.8 / n
    width = slot * 0.82
    for i, enc in enumerate(encs):
        xs, mbs = [], []
        for d_i, dist in enumerate(dists):
            e = table.get((dist, enc))
            if e and "mb" in e:
                xs.append(d_i - 0.4 + slot * (i + 0.5))
                mbs.append(e["mb"])
        ax.bar(xs, mbs, width=width, color=ENCODING_COLORS[enc], label=enc, linewidth=0)
    for d_i, dist in enumerate(dists):
        sizes = [(table[(dist, e)]["mb"], i) for i, e in enumerate(encs) if (dist, e) in table]
        mb, i = min(sizes)
        if mb > base_mb * 0.99:  # nothing beats uncompressed: say so instead of a tie value
            ax.annotate("no gain", (d_i, max(m for m, _ in sizes)), xytext=(0, 3),
                        textcoords="offset points", ha="center", va="bottom", color=MUTED,
                        fontsize=8)
        else:
            ax.annotate(f"{mb:.1f}", (d_i - 0.4 + slot * (i + 0.5), mb), xytext=(0, 3),
                        textcoords="offset points", ha="center", va="bottom", color=TEXT,
                        fontsize=8)
    ax.axhline(base_mb, color=TEXT, linewidth=1, linestyle=(0, (4, 3)))
    ax.text(len(dists) - 0.5, base_mb, f" uncompressed DSM\n {base_mb:.0f} MB", va="center",
            ha="left", color=TEXT, fontsize=8)
    tallest = max(e["mb"] for (_, enc), e in table.items() if enc in encs and "mb" in e)
    ax.set_ylim(0, max(tallest, base_mb) * 1.1)  # headroom for the value labels
    ax.set_ylabel("encoded size (MB)", color=TEXT)
    ax.set_xticks(range(len(dists)), dists)
    ax.set_xlim(-0.5, len(dists) - 0.5)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelcolor=TEXT)

    height = fig.get_figheight()
    y = lambda inches: 1 - inches / height
    fig.text(0.01, y(0.15), "Encoded size per data distribution", ha="left",
             va="top", color=TEXT, fontsize=12, fontweight="bold")
    fig.text(0.01, y(0.45), f"{rows:,} rows × {cols} int64 columns; all {cols} columns, "
             "labels mark the smallest encoding per distribution", ha="left", va="top",
             color=MUTED, fontsize=9)
    fig.legend(*ax.get_legend_handles_labels(), loc="upper left", ncol=n, frameon=False,
               labelcolor=TEXT, fontsize=9, bbox_to_anchor=(0.0, y(0.62)))
    fig.tight_layout(rect=(0, 0, 0.9, y(1.0)))
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    print(f"wrote {path}")


def padded_limits(vals):
    lo, hi = min(vals + [1.0]), max(vals + [1.0])
    return lo / 1.5, hi * 1.5


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    stem, paths = sys.argv[1], sys.argv[2:]
    query, rows, cols, dists, table = compute(paths)
    encs = [e for e in ENCODING_COLORS if any((d, e) in table for d in dists)]

    # Table view: the exact numbers behind the bars.
    with open(f"{stem}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dist", "encoding", "size_MB", "compression_ratio", "speedup_vector",
                    "speedup_value"])
        for dist in dists:
            for enc in encs:
                e = table.get((dist, enc), {})
                w.writerow([dist, enc] + [f"{e[c]:.3f}" if c in e else ""
                                          for c in ("mb", "ratio", "speedup_vector",
                                                    "speedup_value")])
    print(f"wrote {stem}.csv")

    ratio = {key: e["ratio"] for key, e in table.items()}
    speed = {m: {key: e[f"speedup_{m}"] for key, e in table.items() if f"speedup_{m}" in e}
             for m in MODES}
    modes = [m for m in MODES if speed[m]]
    speed_lim = padded_limits([v for m in modes for v in speed[m].values()])

    fig, axes = plt.subplots(1 + len(modes), 1, figsize=(9, 3.0 * (1 + len(modes)) + 0.6),
                             dpi=150)
    fig.patch.set_facecolor(SURFACE)
    bar_panel(axes[0], dists, encs, ratio,
              "Compression ratio vs uncompressed DSM (higher = smaller)",
              padded_limits(list(ratio.values())))
    for ax, m in zip(axes[1:], modes):
        bar_panel(ax, dists, encs, speed[m],
                  f"Speedup vs uncompressed DSM, {m} mode (above 1× = faster)", speed_lim)

    # Header in inches from the top, so it stays put whatever the figure height.
    height = fig.get_figheight()
    y = lambda inches: 1 - inches / height
    fig.text(0.01, y(0.15), f"{query}: what each encoding buys, per data distribution",
             ha="left", va="top", color=TEXT, fontsize=12, fontweight="bold")
    fig.text(0.01, y(0.45), f"{rows:,} rows × {cols} int64 columns, k = N = {cols} (full scan); "
             "medians of 10 runs, single thread", ha="left", va="top", color=MUTED, fontsize=9)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper left", ncol=len(encs), frameon=False,
               labelcolor=TEXT, fontsize=9, bbox_to_anchor=(0.0, y(0.62)))
    fig.tight_layout(rect=(0, 0, 1, y(1.0)))
    fig.savefig(f"{stem}.png", facecolor=SURFACE)
    plt.close(fig)
    print(f"wrote {stem}.png")

    size_chart(f"{stem}_size.png", query, rows, cols, dists, encs, table)


if __name__ == "__main__":
    main()
