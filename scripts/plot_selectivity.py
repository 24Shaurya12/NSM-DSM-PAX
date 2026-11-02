#!/usr/bin/env python3
"""Plots and summarises the selectivity sweep produced by scripts/run_selectivity.sh.

Usage: scripts/plot_selectivity.py [results/selectivity]

Reads <dir>/raw.csv and writes:
  <dir>/summary.csv   medians per (layout, size, k, predicate), incl. the unfiltered baseline
  <dir>/winners.md    fastest layout per (size, selectivity, k): the crossover table
  <dir>/plots/*.png   latency, relative-to-DSM, and (with counters) branch-miss, LLC-miss and
                      DRAM-traffic figures, each as small multiples: one row per table size,
                      one column per k
"""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_working_set import (COLORS, INK, INK_2, LAYOUTS, direct_labels, fmt_bytes, load,  # noqa: E402
                              plot_series, read_caches)


def summarise(df):
    metrics = [c for c in df.columns if c.endswith("_per_row") or c in ("useful_gbps", "ipc")]
    keys = ["layout", "data_bytes", "rows", "k", "predicate"]
    g = df.groupby(keys)
    out = g[["latency_ns", "selectivity"] + metrics].median().add_suffix("_median")
    q = g[["ns_per_row"]]
    out = out.join(q.quantile(0.25).add_suffix("_q25")).join(q.quantile(0.75).add_suffix("_q75"))
    out = out.reset_index()
    out["sel_pct"] = out.selectivity_median * 100
    return out


def sweep(summary, metric, ylabel, title, path, band=True, ref=None, layouts=LAYOUTS, logy=False):
    filtered = summary[summary.predicate != "none"]
    sizes = sorted(filtered.data_bytes.unique())
    ks = sorted(filtered.k.unique())
    fig, axes = plt.subplots(len(sizes), len(ks), figsize=(4.2 * len(ks), 3.3 * len(sizes)),
                             sharex=True, squeeze=False)
    pending_labels = []
    for r, size in enumerate(sizes):
        for c, k in enumerate(ks):
            ax = axes[r][c]
            if logy:
                ax.set_yscale("log")
            ends = []
            for layout in layouts:
                d = filtered[(filtered.layout == layout) & (filtered.data_bytes == size) &
                             (filtered.k == k)]
                if d.empty or d[metric].isna().all():
                    continue
                lo, hi = metric.replace("_median", "_q25"), metric.replace("_median", "_q75")
                b = (lo, hi) if band and lo in d and hi in d else None
                plot_series(ax, d, "sel_pct", metric, layout, b)
                last = d.sort_values("sel_pct").iloc[-1]
                ends.append((layout, last.sel_pct, last[metric]))
            if ref is not None:
                ref(ax, size, k)
            ax.set_xlim(-3, 108)
            ax.set_xticks([0, 25, 50, 75, 100])
            ax.set_ylim(bottom=0 if not logy else None)
            ax.set_title(f"{fmt_bytes(size)} table · k = {k}", loc="left")
            if r == len(sizes) - 1:
                ax.set_xlabel("selectivity (% of rows qualifying)")
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


def has(summary, metric):
    return metric in summary and summary[metric].notna().any()


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results/selectivity")
    df = load(out_dir / "raw.csv")
    plots = out_dir / "plots"
    plots.mkdir(parents=True, exist_ok=True)

    summary = summarise(df)
    summary.to_csv(out_dir / "summary.csv", index=False)
    print(f"  wrote {out_dir / 'summary.csv'}")

    # Hairline per layout at the latency of the same query without a WHERE clause.
    base = summary[summary.predicate == "none"].set_index(["layout", "data_bytes", "k"])

    def scan_ref(ax, size, k):
        for layout in LAYOUTS:
            key = (layout, size, k)
            if key in base.index:
                ax.axhline(base.loc[key].ns_per_row_median, color=COLORS[layout], linewidth=0.8,
                           alpha=0.5, zorder=1)

    sweep(summary, "ns_per_row_median", "latency per row (ns)",
          "Filtered-scan latency vs selectivity  (thin lines: same query with no WHERE clause)",
          plots / "sel_latency.png", ref=scan_ref)

    dsm = summary[summary.layout == "DSM"].set_index(["data_bytes", "k", "predicate"])
    rel = summary.join(dsm.ns_per_row_median.rename("dsm_ns"), on=["data_bytes", "k", "predicate"])
    rel["vs_dsm_median"] = rel.ns_per_row_median / rel.dsm_ns

    def dsm_ref(ax, _size, _k):
        ax.axhline(1.0, color=COLORS["DSM"], linewidth=1.2, zorder=2)
        ax.annotate("DSM = 1", xy=(0, 1.0), xycoords=("axes fraction", "data"),
                    xytext=(4, 3), textcoords="offset points", fontsize=8, color=INK_2)

    sweep(rel, "vs_dsm_median", "latency ÷ DSM latency (log)",
          "Latency relative to DSM (above 1 = slower than DSM)",
          plots / "sel_relative_to_dsm.png", band=False, ref=dsm_ref, layouts=["NSM", "PAX"],
          logy=True)

    # DRAM traffic of a cache-resident table is near zero and dominated by system-wide noise, so
    # that figure only shows tables larger than the last-level cache.
    llc = max((size for _, size in read_caches(out_dir / "machine.txt")), default=0)
    for metric, ylabel, title, name in (
        ("branch_misses_per_row_median", "branch misses per row",
         "Branch mispredictions per row", "sel_branch_misses.png"),
        ("llc_misses_per_row_median", "LLC misses per row",
         "Last-level-cache misses per row", "sel_llc_misses.png"),
        ("dram_read_bytes_per_row_median", "DRAM bytes read per row",
         "Memory traffic per row (IMC counters, system-wide)", "sel_dram_bytes.png"),
    ):
        data = summary[summary.data_bytes > llc] if metric.startswith("dram") else summary
        if has(data, metric):
            sweep(data, metric, ylabel, title, plots / name, band=False)
        else:
            print(f"  skipped {name} (no counter data)")

    winners(summary, out_dir / "winners.md")


def winners(summary, path):
    lines = ["# Fastest layout per selectivity", "",
             "Each cell: winner (runner-up latency ÷ winner latency). Medians of the timed "
             "samples. `scan` is the same query with no WHERE clause.", ""]
    for size in sorted(summary.data_bytes.unique()):
        m = summary[summary.data_bytes == size]
        ks = sorted(m.k.unique())
        lines += [f"## {fmt_bytes(size)} table", "",
                  "| selectivity | " + " | ".join(f"k={k}" for k in ks) + " |",
                  "|---|" + "---|" * len(ks)]
        preds = (m[m.predicate != "none"].groupby("predicate").sel_pct.first().sort_values()
                 .index.tolist()) + ["none"]
        for pred in preds:
            cells = []
            for k in ks:
                d = m[(m.predicate == pred) & (m.k == k)].sort_values("ns_per_row_median")
                if len(d) < 2:
                    cells.append("-")
                    continue
                best, second = d.iloc[0], d.iloc[1]
                cells.append(f"{best.layout} ({second.ns_per_row_median / best.ns_per_row_median:.2f}×)")
            label = "scan" if pred == "none" else f"{m[m.predicate == pred].sel_pct.iloc[0]:.1f}%"
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("")
    path.write_text("\n".join(lines))
    print(f"  wrote {path}")


if __name__ == "__main__":
    main()
