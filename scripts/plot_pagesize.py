#!/usr/bin/env python3
"""Plots and summarises the PAX page-size sweep produced by scripts/run_pagesize.sh.

Usage: scripts/plot_pagesize.py [results/pagesize]

PAX is a line over page size; NSM and DSM have no pages and appear as horizontal reference
lines. Writes:
  <dir>/summary.csv                     medians per (layout, page size, op, predicate, k)
  <dir>/best_page_size.md               best PAX page size per query, against NSM and DSM
  <dir>/plots/pagesize_sum.png          SUM queries: one row per predicate, one column per k
  <dir>/plots/pagesize_materialize.png  tuple reconstruction, same grid
  <dir>/plots/pagesize_counters.png     latency, DRAM bytes, L1D and dTLB misses for 3 queries
"""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_working_set import (COLORS, INK, direct_labels, fmt_bytes, load,  # noqa: E402
                              plot_series)

PREDICATE_ORDER = ["none", "c0<1", "c0<10", "c0<50", "c0<1 & c12<50"]
METRICS = ["ns_per_row", "dram_read_bytes_per_row", "l1d_misses_per_row", "dtlb_misses_per_row",
           "llc_misses_per_row"]


def summarise(df):
    keys = ["layout", "page_size", "cols", "op", "predicate", "k"]
    metrics = [m for m in METRICS if m in df]
    g = df.groupby(keys)
    out = g[["latency_ns", "selectivity"] + metrics].median().add_suffix("_median")
    q = g[["ns_per_row"]]
    out = out.join(q.quantile(0.25).add_suffix("_q25")).join(q.quantile(0.75).add_suffix("_q75"))
    return out.reset_index()


def setup_page_axis(ax, sizes):
    ax.set_xscale("log", base=2)
    major = [s for s in sizes if round(np.log2(s)) % 2 == 0]
    ax.xaxis.set_major_locator(FixedLocator(major))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_bytes(v)))
    ax.xaxis.set_minor_locator(FixedLocator(sizes))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlim(min(sizes) / 1.4, max(sizes) * 1.4)


def draw_panel(ax, s, metric, band=True):
    """PAX over page size plus NSM/DSM reference lines, all labelled at the right edge.

    s: the summary rows of one query (all layouts)."""
    pax = s[s.layout == "PAX"].sort_values("page_size")
    sizes = sorted(pax.page_size.unique())
    setup_page_axis(ax, sizes)
    ends = []
    for layout in ("NSM", "DSM"):
        ref = s[s.layout == layout]
        if not ref.empty and ref[metric].notna().any():
            y = ref[metric].iloc[0]
            ax.axhline(y, color=COLORS[layout], linewidth=1.6, zorder=2, label=layout)
            ends.append((layout, sizes[-1], y))
    if not pax.empty and pax[metric].notna().any():
        lo, hi = metric.replace("_median", "_q25"), metric.replace("_median", "_q75")
        plot_series(ax, pax, "page_size", metric, "PAX",
                    (lo, hi) if band and lo in pax and hi in pax else None)
        ends.append(("PAX", sizes[-1], pax[metric].iloc[-1]))
    ax.set_ylim(bottom=0)
    return ends


def query_label(op, predicate, k, cols):
    cols_txt = f"c0..c{k - 1}" if k > 1 else "c0"
    where = "" if predicate == "none" else f" WHERE {predicate.replace(' & ', ' AND ')}"
    if op == "sum":
        return f"SUM({cols_txt}){where}"
    return f"SELECT {cols_txt}{where}"


def grid(summary, op, path):
    s_op = summary[summary.op == op]
    preds = [p for p in PREDICATE_ORDER if p in set(s_op.predicate)]
    ks = sorted(s_op.k.unique())
    cols = int(s_op.cols.iloc[0])
    fig, axes = plt.subplots(len(preds), len(ks), figsize=(4.3 * len(ks), 2.9 * len(preds)),
                             sharex=True, squeeze=False)
    pending = []
    for r, pred in enumerate(preds):
        for c, k in enumerate(ks):
            ax = axes[r][c]
            s = s_op[(s_op.predicate == pred) & (s_op.k == k)]
            ends = draw_panel(ax, s, "ns_per_row_median")
            ax.set_title(query_label(op, pred, k, cols), loc="left", fontsize=9.5)
            if c == 0:
                ax.set_ylabel("ns per row")
            if r == len(preds) - 1:
                ax.set_xlabel(f"PAX page size (minipage = page ÷ {cols})")
            pending.append((ax, ends))
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), bbox_to_anchor=(0.99, 1.0))
    what = "SUM queries" if op == "sum" else "tuple reconstruction (SELECT c0..c{k-1})"
    fig.suptitle(f"PAX page size vs latency: {what}  (lines: NSM and DSM, which have no pages)",
                 x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 0.96, 0.975))
    for ax, ends in pending:
        direct_labels(ax, ends)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def counters(summary, path):
    queries = [("sum", "none", 1), ("sum", "c0<10", 4), ("materialize", "c0<1", 16)]
    panels = [("ns_per_row_median", "ns per row"),
              ("dram_read_bytes_per_row_median", "DRAM bytes read per row"),
              ("l1d_misses_per_row_median", "L1D misses per row"),
              ("dtlb_misses_per_row_median", "dTLB misses per row")]
    panels = [(m, l) for m, l in panels if m in summary and summary[m].notna().any()]
    queries = [q for q in queries
               if not summary[(summary.op == q[0]) & (summary.predicate == q[1]) &
                              (summary.k == q[2])].empty]
    cols = int(summary.cols.iloc[0])
    fig, axes = plt.subplots(len(queries), len(panels),
                             figsize=(4.0 * len(panels), 3.0 * len(queries)), sharex=True,
                             squeeze=False)
    pending = []
    for r, (op, pred, k) in enumerate(queries):
        s = summary[(summary.op == op) & (summary.predicate == pred) & (summary.k == k)]
        for c, (metric, label) in enumerate(panels):
            ax = axes[r][c]
            ends = draw_panel(ax, s, metric, band=False)
            ax.set_title(label if r == 0 else "", loc="left", fontsize=10)
            if c == 0:
                ax.set_ylabel(query_label(op, pred, k, cols), fontsize=9)
            if r == len(queries) - 1:
                ax.set_xlabel("PAX page size")
            pending.append((ax, ends))
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), bbox_to_anchor=(0.99, 1.0))
    fig.suptitle("Why page size matters: memory traffic, cache and TLB misses per row",
                 x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 0.96, 0.95))
    for ax, ends in pending:
        direct_labels(ax, ends)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def best_table(summary, path):
    cols = int(summary.cols.iloc[0])
    lines = ["# Best PAX page size per query", "",
             "ns per row (medians). `PAX best` is the fastest page size; the last column "
             "compares it with the faster of NSM and DSM (above 1 = PAX wins).", "",
             "| query | NSM | DSM | PAX 16K | PAX best | best page | PAX best vs other |",
             "|---|---|---|---|---|---|---|"]
    for op in ("sum", "materialize"):
        for pred in PREDICATE_ORDER:
            for k in sorted(summary.k.unique()):
                s = summary[(summary.op == op) & (summary.predicate == pred) & (summary.k == k)]
                if s.empty:
                    continue
                nsm = s[s.layout == "NSM"].ns_per_row_median.iloc[0]
                dsm = s[s.layout == "DSM"].ns_per_row_median.iloc[0]
                pax = s[s.layout == "PAX"].set_index("page_size").ns_per_row_median
                best = pax.idxmin()
                p16 = pax.get(16384, np.nan)
                lines.append(f"| `{query_label(op, pred, k, cols)}` | {nsm:.2f} | {dsm:.2f} | "
                             f"{p16:.2f} | {pax.min():.2f} | {fmt_bytes(best)} | "
                             f"{min(nsm, dsm) / pax.min():.2f}× |")
    path.write_text("\n".join(lines) + "\n")
    print(f"  wrote {path}")


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results/pagesize")
    df = load(out_dir / "raw.csv")
    plots = out_dir / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    summary = summarise(df)
    summary.to_csv(out_dir / "summary.csv", index=False)
    print(f"  wrote {out_dir / 'summary.csv'}")
    for op in ("sum", "materialize"):
        if op in set(summary.op):
            grid(summary, op, plots / f"pagesize_{op}.png")
    counters(summary, plots / "pagesize_counters.png")
    best_table(summary, out_dir / "best_page_size.md")


if __name__ == "__main__":
    main()
