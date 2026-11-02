#!/usr/bin/env python3
"""Plot scan_bench CSV for a single query: median latency vs k for each layout.

Usage: plot.py RESULTS.csv OUT_STEM [--logy]

Writes OUT_STEM.png (latency). If the CSV has perf counter columns, also writes one
OUT_STEM_<metric>.png per metric (e.g. _ipc, _l1d_misses_per_row). With --logy every
file name gets a _log suffix.
"""
import csv
import statistics
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter, LogLocator  # noqa: E402

from perf_metrics import counter_columns, metrics  # noqa: E402

# Fixed colour per layout so a layout looks the same in every graph.
LAYOUT_COLORS = {"NSM": "#2a78d6", "DSM": "#eb6834", "PAX": "#1baf7a"}
TEXT = "#0b0b0b"
MUTED = "#898781"
GRID = "#e1e0d9"
SURFACE = "#fcfcfb"


def load(path):
    """Returns ({series name: {(query, layout, k): [values]}}, meta).
    Series are "latency" (ms) plus one per perf metric if the CSV has counters."""
    series = defaultdict(lambda: defaultdict(list))
    meta = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        counters = counter_columns(reader.fieldnames or [])
        for row in reader:
            key = (row["query"], row["layout"], int(row["k"]))
            series["latency"][key].append(int(row["latency_ns"]) / 1e6)
            for name, value in metrics(row, counters).items():
                series[name][key].append(value)
            meta["rows"], meta["cols"] = int(row["rows"]), int(row["cols"])
            if int(row["page_size"]):
                meta["page_size"] = int(row["page_size"])
    return series, meta


def plot_query(query, lat, meta, out_path, logy, metric="latency"):
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    layouts = [l for l in LAYOUT_COLORS if any(key[:2] == (query, l) for key in lat)]
    all_ks = set()
    for layout in layouts:
        ks = sorted(k for (q, l, k) in lat if q == query and l == layout)
        all_ks.update(ks)
        runs = [lat[(query, layout, k)] for k in ks]
        med = [statistics.median(r) for r in runs]
        lo = [min(r) for r in runs]
        hi = [max(r) for r in runs]
        color = LAYOUT_COLORS[layout]
        ax.fill_between(ks, lo, hi, color=color, alpha=0.12, linewidth=0)
        ax.plot(ks, med, color=color, linewidth=2, marker="o", markersize=6,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=layout,
                solid_capstyle="round", solid_joinstyle="round")

    ks = sorted(all_ks)
    ax.set_xscale("log", base=2)
    ax.set_xticks(ks, [str(k) for k in ks])
    ax.minorticks_off()
    if logy:
        # Log y: equal vertical distance = equal ratio; slope 1 = cost proportional to k.
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.yaxis.set_minor_locator(LogLocator(base=10, subs="auto"))
        ax.yaxis.set_minor_formatter("")
    else:
        ax.set_ylim(bottom=0)
    ax.set_xlabel(f"k (columns read, of N = {meta['cols']})", color=TEXT)
    scale = ", log scale" if logy else ""
    what = "latency (ms" if metric == "latency" else f"{metric} (median"
    if metric == "latency":
        what += ", median"
    ax.set_ylabel(f"{what}{scale}; band = min–max)", color=TEXT)

    subtitle = f"{meta['rows']:,} rows × {meta['cols']} int64 columns"
    if "page_size" in meta:
        subtitle += f", PAX page {meta['page_size'] // 1024} KB"
    ax.set_title(f"{query}: {metric} vs k\n", loc="left", color=TEXT, fontsize=12,
                 fontweight="bold")
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, color=MUTED, fontsize=9)

    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelcolor=TEXT)
    ax.legend(frameon=False, loc="upper left", labelcolor=TEXT)

    fig.tight_layout()
    fig.savefig(out_path, facecolor=SURFACE)
    plt.close(fig)


def main():
    args = [a for a in sys.argv[1:] if a != "--logy"]
    logy = "--logy" in sys.argv[1:]
    if len(args) != 2:
        sys.exit(__doc__)
    csv_path, stem = args

    series, meta = load(csv_path)
    queries = sorted({q for (q, _, _) in series["latency"]})
    if len(queries) != 1:
        sys.exit(f"plot.py: expected exactly one query in {csv_path}, found {queries}")
    suffix = "_log" if logy else ""
    for metric, values in series.items():
        name = "" if metric == "latency" else "_" + metric.replace("/", "_per_")
        out_path = f"{stem}{name}{suffix}.png"
        plot_query(queries[0], values, meta, out_path, logy, metric)
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
