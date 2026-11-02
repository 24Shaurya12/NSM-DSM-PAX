#!/usr/bin/env python3
"""Plots and summarises the PAX-wins experiment produced by scripts/run_pax_wins.sh.

Usage: scripts/plot_pax_wins.py [results/pax_wins]

Query: SELECT * FROM T WHERE c0 < S on a wide table. Writes:
  <dir>/summary.csv                   medians per (layout, page size, predicate)
  <dir>/winners.md                    NSM, DSM and every PAX page size per selectivity
  <dir>/plots/pax_wins_latency.png    left: ns per row vs selectivity (PAX at its best page
                                      size); right: ns per row vs PAX page size at ~1%
  <dir>/plots/pax_wins_counters.png   DRAM bytes, L1D and dTLB misses per row vs selectivity
"""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_pagesize import draw_panel  # noqa: E402
from plot_working_set import (INK, LAYOUTS, direct_labels, fmt_bytes, load,  # noqa: E402
                              plot_series)

FOCUS_PREDICATE = "c0<1"  # the right-hand panel: latency vs page size for this query


def summarise(df):
    keys = ["layout", "page_size", "cols", "predicate"]
    metrics = [m for m in ("ns_per_row", "dram_read_bytes_per_row", "l1d_misses_per_row",
                           "dtlb_misses_per_row", "llc_misses_per_row") if m in df]
    g = df.groupby(keys)
    out = g[["latency_ns", "selectivity"] + metrics].median().add_suffix("_median")
    q = g[["ns_per_row"]]
    out = out.join(q.quantile(0.25).add_suffix("_q25")).join(q.quantile(0.75).add_suffix("_q75"))
    out = out.reset_index()
    out["sel_pct"] = out.selectivity_median * 100
    return out


def best_pax_page(summary):
    """PAX page size with the lowest latency at the focus predicate."""
    pax = summary[(summary.layout == "PAX") & (summary.predicate == FOCUS_PREDICATE)]
    return int(pax.loc[pax.ns_per_row_median.idxmin()].page_size)


def series(summary, page):
    """NSM, DSM and PAX at the given page size, ordered by selectivity."""
    s = summary[(summary.layout != "PAX") | (summary.page_size == page)].copy()
    order = sorted(s.sel_pct.round(2).unique())
    s["xpos"] = s.sel_pct.round(2).map({v: i for i, v in enumerate(order)})
    return s, order


def setup_sel_axis(ax, order):
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([f"{v:g}%" for v in np.round(order, 1)])
    ax.set_xlim(-0.4, len(order) - 0.6)


def latency_figure(summary, path, page):
    cols = int(summary.cols.iloc[0])
    s, order = series(summary, page)
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 3.8), squeeze=False)

    ax = axes[0][0]
    ends = []
    for layout in LAYOUTS:
        d = s[s.layout == layout]
        plot_series(ax, d, "xpos", "ns_per_row_median", layout, ("ns_per_row_q25", "ns_per_row_q75"))
        last = d.sort_values("xpos").iloc[-1]
        ends.append((layout, last.xpos, last.ns_per_row_median))
    setup_sel_axis(ax, order)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("selectivity (% of rows qualifying)")
    ax.set_ylabel("ns per row")
    ax.set_title(f"vs selectivity  (PAX with {fmt_bytes(page)} pages)", loc="left")
    left_ends = (ax, ends)

    ax = axes[0][1]
    focus = summary[summary.predicate == FOCUS_PREDICATE]
    right_ends = (ax, draw_panel(ax, focus, "ns_per_row_median"))
    sel = focus.sel_pct.iloc[0]
    ax.set_xlabel(f"PAX page size (minipage = page ÷ {cols})")
    ax.set_title(f"vs PAX page size  (WHERE {FOCUS_PREDICATE}, {sel:.1f}% of rows)", loc="left")

    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), bbox_to_anchor=(0.99, 1.0))
    size = fmt_bytes(summary.rows.iloc[0] * cols * 8) if "rows" in summary else ""
    fig.suptitle(f"SELECT * FROM T WHERE c0 < S   ({cols} int64 columns, {size} table)",
                 x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 0.96, 0.92))
    direct_labels(*left_ends)
    direct_labels(*right_ends)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def counters_figure(summary, path, page):
    panels = [("dram_read_bytes_per_row_median", "DRAM bytes read per row"),
              ("l1d_misses_per_row_median", "L1D misses per row"),
              ("dtlb_misses_per_row_median", "dTLB misses per row")]
    panels = [(m, l) for m, l in panels if m in summary and summary[m].notna().any()]
    s, order = series(summary, page)
    fig, axes = plt.subplots(1, len(panels), figsize=(4.2 * len(panels), 3.6), squeeze=False)
    pending = []
    for c, (metric, label) in enumerate(panels):
        ax = axes[0][c]
        ends = []
        for layout in LAYOUTS:
            d = s[s.layout == layout]
            plot_series(ax, d, "xpos", metric, layout)
            last = d.sort_values("xpos").iloc[-1]
            ends.append((layout, last.xpos, last[metric]))
        setup_sel_axis(ax, order)
        ax.set_ylim(bottom=0)
        ax.set_title(label, loc="left")
        ax.set_xlabel("selectivity")
        pending.append((ax, ends))
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), bbox_to_anchor=(0.99, 1.0))
    fig.suptitle(f"Where the time goes (PAX with {fmt_bytes(page)} pages)", x=0.01, ha="left",
                 fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 0.96, 0.9))
    for ax, ends in pending:
        direct_labels(ax, ends)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def winners(summary, path):
    pages = sorted(summary[summary.layout == "PAX"].page_size.unique())
    lines = ["# SELECT * FROM T WHERE c0 < S: ns per row", "",
             "Medians of the timed samples. Bold: fastest in the row. Last column: fastest PAX "
             "page size against the faster of NSM and DSM (above 1 = PAX wins).", "",
             "| selectivity | NSM | DSM | " + " | ".join(f"PAX {fmt_bytes(p)}" for p in pages)
             + " | PAX best vs other |",
             "|---|---|---|" + "---|" * len(pages) + "---|"]
    for pred in summary.sort_values("sel_pct").predicate.unique():
        s = summary[summary.predicate == pred]
        vals = {"NSM": s[s.layout == "NSM"].ns_per_row_median.iloc[0],
                "DSM": s[s.layout == "DSM"].ns_per_row_median.iloc[0]}
        for p in pages:
            vals[p] = s[(s.layout == "PAX") & (s.page_size == p)].ns_per_row_median.iloc[0]
        best = min(vals.values())
        cell = lambda v: f"**{v:.2f}**" if v == best else f"{v:.2f}"  # noqa: E731
        pax_best = min(vals[p] for p in pages)
        lines.append(f"| {s.sel_pct.iloc[0]:.1f}% (`{pred}`) | {cell(vals['NSM'])} | "
                     f"{cell(vals['DSM'])} | " + " | ".join(cell(vals[p]) for p in pages) +
                     f" | {min(vals['NSM'], vals['DSM']) / pax_best:.2f}× |")
    path.write_text("\n".join(lines) + "\n")
    print(f"  wrote {path}")


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results/pax_wins")
    df = load(out_dir / "raw.csv")
    plots = out_dir / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    summary = summarise(df)
    summary["rows"] = int(df.rows.iloc[0])
    summary.to_csv(out_dir / "summary.csv", index=False)
    print(f"  wrote {out_dir / 'summary.csv'}")
    page = best_pax_page(summary)
    latency_figure(summary, plots / "pax_wins_latency.png", page)
    counters_figure(summary, plots / "pax_wins_counters.png", page)
    winners(summary, out_dir / "winners.md")


if __name__ == "__main__":
    main()
