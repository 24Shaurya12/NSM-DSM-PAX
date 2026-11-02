#!/usr/bin/env python3
"""Plots and summarises the two-predicate experiment produced by scripts/run_conjunction.sh.

Usage: scripts/plot_conjunction.py [results/conjunction]

Each query is  WHERE c0 < S1 AND <other>  (selective first) or  WHERE <other> AND c0 < S1
(selective last). Writes:
  <dir>/summary.csv   medians per (layout, size, k, query)
  <dir>/winners.md    fastest layout per (order, S1, k)
  <dir>/plots/conj_latency.png          ns per row vs S1; one row per predicate order, one column per k
  <dir>/plots/conj_relative_to_dsm.png  NSM and PAX latency divided by DSM latency
  <dir>/plots/conj_order_penalty.png    selective-last latency divided by selective-first latency
  <dir>/plots/conj_counters.png         comparisons, branch misses and DRAM bytes per row (one k)
"""

import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_working_set import (COLORS, INK, INK_2, LAYOUTS, direct_labels, fmt_bytes, load,  # noqa: E402
                              plot_series)

ORDERS = ["selective first", "selective last"]
COUNTER_K = 4  # k used for the counter figure


def parse_queries(df):
    """Adds s1 (percent), order and other (the unselective predicate) from the predicate label."""
    def split(label):
        terms = [t.strip() for t in label.split("&")]
        sel = [t for t in terms if re.fullmatch(r"c0<\d+", t)]
        if len(terms) != 2 or len(sel) != 1:
            return pd.Series({"s1": np.nan, "order": None, "other": None})
        other = terms[1] if terms[0] == sel[0] else terms[0]
        return pd.Series({"s1": int(sel[0][3:]),
                          "order": ORDERS[0] if terms[0] == sel[0] else ORDERS[1],
                          "other": other})
    labels = df.predicate.drop_duplicates()
    parsed = labels.apply(split)
    parsed.index = labels.values
    df = df.join(parsed, on="predicate")
    return df[df.order.notna()]


def summarise(df):
    df = df.copy()
    df["comparisons_per_row"] = np.where(df.order == ORDERS[0], 1 + df.s1 / 100, 1.5)
    metrics = [c for c in df.columns if c.endswith("_per_row") or c in ("useful_gbps", "ipc")]
    keys = ["layout", "data_bytes", "rows", "k", "predicate", "order", "s1", "other"]
    g = df.groupby(keys)
    out = g[["latency_ns", "selectivity"] + metrics].median().add_suffix("_median")
    q = g[["ns_per_row"]]
    out = out.join(q.quantile(0.25).add_suffix("_q25")).join(q.quantile(0.75).add_suffix("_q75"))
    return out.reset_index()


def setup_s1_axis(ax, s1_values):
    ax.set_xticks(range(len(s1_values)))
    ax.set_xticklabels([f"{s:g}%" for s in s1_values])
    ax.set_xlim(-0.4, len(s1_values) - 0.6)


def grid_figure(summary, metric, ylabel, title, path, layouts=LAYOUTS, band=True, ref=None,
                logy=True):
    """Small multiples: one row per predicate order, one column per k; x = S1."""
    s1_values = sorted(summary.s1.unique())
    pos = {s: i for i, s in enumerate(s1_values)}
    ks = sorted(summary.k.unique())
    other = summary.other.iloc[0]
    fig, axes = plt.subplots(len(ORDERS), len(ks), figsize=(4.2 * len(ks), 3.3 * len(ORDERS)),
                             sharex=True, sharey=True, squeeze=False)
    pending = []
    for r, order in enumerate(ORDERS):
        for c, k in enumerate(ks):
            ax = axes[r][c]
            if logy:
                ax.set_yscale("log")
            ends = []
            for layout in layouts:
                d = summary[(summary.layout == layout) & (summary.order == order) &
                            (summary.k == k)].copy()
                if d.empty or d[metric].isna().all():
                    continue
                d["xpos"] = d.s1.map(pos)
                lo, hi = metric.replace("_median", "_q25"), metric.replace("_median", "_q75")
                plot_series(ax, d, "xpos", metric, layout,
                            (lo, hi) if band and lo in d and hi in d else None)
                last = d.sort_values("xpos").iloc[-1]
                ends.append((layout, last.xpos, last[metric]))
            if ref is not None:
                ref(ax)
            setup_s1_axis(ax, s1_values)
            where = f"c0<S1 AND {other}" if order == ORDERS[0] else f"{other} AND c0<S1"
            ax.set_title(f"WHERE {where} · k = {k}", loc="left", fontsize=10)
            if r == len(ORDERS) - 1:
                ax.set_xlabel("S1 = selectivity of c0 < S1")
            if c == 0:
                ax.set_ylabel(ylabel)
            if c == len(ks) - 1:
                pending.append((ax, ends))
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), bbox_to_anchor=(0.99, 1.0))
    size = fmt_bytes(summary.data_bytes.iloc[0])
    fig.suptitle(f"{title}  ({size} table, warm)", x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 0.97, 0.95))
    for ax, ends in pending:
        direct_labels(ax, ends)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def order_penalty(summary, path):
    """Selective-last latency divided by selective-first latency, per layout and k."""
    s1_values = sorted(summary.s1.unique())
    pos = {s: i for i, s in enumerate(s1_values)}
    piv = summary.pivot_table(index=["layout", "k", "s1"], columns="order",
                              values="ns_per_row_median").reset_index()
    piv["penalty_median"] = piv[ORDERS[1]] / piv[ORDERS[0]]
    piv["xpos"] = piv.s1.map(pos)
    ks = sorted(piv.k.unique())
    fig, axes = plt.subplots(1, len(ks), figsize=(4.2 * len(ks), 3.4), sharey=True, squeeze=False)
    for c, k in enumerate(ks):
        ax = axes[0][c]
        ends = []
        for layout in LAYOUTS:
            d = piv[(piv.layout == layout) & (piv.k == k)]
            if d.empty:
                continue
            plot_series(ax, d, "xpos", "penalty_median", layout)
            last = d.sort_values("xpos").iloc[-1]
            ends.append((layout, last.xpos, last.penalty_median))
        ax.axhline(1.0, color=INK_2, linewidth=0.8)
        setup_s1_axis(ax, s1_values)
        ax.set_title(f"k = {k}", loc="left")
        ax.set_xlabel("S1 = selectivity of c0 < S1")
        if c == 0:
            ax.set_ylabel("selective-last ÷ selective-first")
        if c == len(ks) - 1:
            last_ends = (ax, ends)
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), bbox_to_anchor=(0.99, 1.0))
    fig.suptitle("Cost of putting the unselective predicate first (above 1 = slower)",
                 x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 0.97, 0.92))
    direct_labels(*last_ends)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def counters(summary, path, k):
    """Columns: comparisons, branch misses, DRAM bytes per row; rows: predicate order."""
    panels = [("comparisons_per_row_median", "predicate comparisons per row"),
              ("branch_misses_per_row_median", "branch misses per row"),
              ("dram_read_bytes_per_row_median", "DRAM bytes read per row")]
    panels = [(m, l) for m, l in panels if m in summary and summary[m].notna().any()]
    s1_values = sorted(summary.s1.unique())
    pos = {s: i for i, s in enumerate(s1_values)}
    d_k = summary[summary.k == k]
    fig, axes = plt.subplots(len(ORDERS), len(panels), figsize=(4.2 * len(panels), 3.3 * len(ORDERS)),
                             sharex=True, sharey="col", squeeze=False)
    pending = []
    for r, order in enumerate(ORDERS):
        for c, (metric, label) in enumerate(panels):
            ax = axes[r][c]
            ends = []
            if metric.startswith("comparisons"):
                # Identical for every layout (NSM short-circuits, DSM/PAX refine a selection
                # vector), so draw it once in neutral ink rather than in one layout's colour.
                d = d_k[(d_k.layout == "DSM") & (d_k.order == order)].sort_values("s1")
                ax.plot(d.s1.map(pos), d[metric], color=INK_2, linewidth=2, marker="o",
                        markersize=5.5, markeredgecolor="#ffffff", markeredgewidth=1.2)
            else:
                for layout in LAYOUTS:
                    d = d_k[(d_k.layout == layout) & (d_k.order == order)].copy()
                    if d.empty:
                        continue
                    d["xpos"] = d.s1.map(pos)
                    plot_series(ax, d, "xpos", metric, layout)
                    last = d.sort_values("xpos").iloc[-1]
                    ends.append((layout, last.xpos, last[metric]))
            setup_s1_axis(ax, s1_values)
            ax.set_title(f"{order} · {label}", loc="left", fontsize=10)
            if r == len(ORDERS) - 1:
                ax.set_xlabel("S1 = selectivity of c0 < S1")
            pending.append((ax, ends))
    handles, labels = axes[0][-1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), bbox_to_anchor=(0.99, 1.0))
    fig.suptitle(f"Why the order matters (k = {k}; comparison counts are the same for all layouts)",
                 x=0.01, ha="left", fontsize=12, color=INK)
    # Only now are the shared column limits final; fixing the bottom earlier would freeze the top.
    for ax in axes.flat:
        ax.set_ylim(bottom=0)
    fig.tight_layout(rect=(0, 0, 0.97, 0.95))
    for ax, ends in pending:
        if ends:
            direct_labels(ax, ends)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def winners(summary, path):
    lines = ["# Fastest layout per query", "",
             "Each cell: winner (runner-up latency ÷ winner latency). Medians of the timed "
             "samples.", ""]
    ks = sorted(summary.k.unique())
    for order in ORDERS:
        m = summary[summary.order == order]
        other = m.other.iloc[0]
        where = f"c0<S1 AND {other}" if order == ORDERS[0] else f"{other} AND c0<S1"
        lines += [f"## WHERE {where}", "",
                  "| S1 | overall selectivity | " + " | ".join(f"k={k}" for k in ks) + " |",
                  "|---|---|" + "---|" * len(ks)]
        for s1 in sorted(m.s1.unique()):
            cells = []
            for k in ks:
                d = m[(m.s1 == s1) & (m.k == k)].sort_values("ns_per_row_median")
                best, second = d.iloc[0], d.iloc[1]
                cells.append(f"{best.layout} ({second.ns_per_row_median / best.ns_per_row_median:.2f}×)")
            sel = m[m.s1 == s1].selectivity_median.iloc[0] * 100
            lines.append(f"| {s1:g}% | {sel:.2f}% | " + " | ".join(cells) + " |")
        lines.append("")
    path.write_text("\n".join(lines))
    print(f"  wrote {path}")


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results/conjunction")
    df = load(out_dir / "raw.csv")
    if "op" in df:
        df = df[df.op == "sum"]
    df = parse_queries(df)
    plots = out_dir / "plots"
    plots.mkdir(parents=True, exist_ok=True)

    summary = summarise(df)
    summary.to_csv(out_dir / "summary.csv", index=False)
    print(f"  wrote {out_dir / 'summary.csv'}")

    for size, s in summary.groupby("data_bytes"):
        suffix = "" if summary.data_bytes.nunique() == 1 else f"_{fmt_bytes(size)}"
        grid_figure(s, "ns_per_row_median", "latency per row (ns, log)",
                    "Two predicates: a selective one on c0 and a 50% one on c12",
                    plots / f"conj_latency{suffix}.png")

        dsm = s[s.layout == "DSM"].set_index(["k", "predicate"]).ns_per_row_median
        rel = s.join(dsm.rename("dsm_ns"), on=["k", "predicate"])
        rel["vs_dsm_median"] = rel.ns_per_row_median / rel.dsm_ns

        def dsm_ref(ax):
            ax.axhline(1.0, color=COLORS["DSM"], linewidth=1.2, zorder=2)
            ax.annotate("DSM = 1", xy=(0, 1.0), xycoords=("axes fraction", "data"),
                        xytext=(4, 3), textcoords="offset points", fontsize=8, color=INK_2)

        grid_figure(rel, "vs_dsm_median", "latency ÷ DSM latency (log)",
                    "How much slower than DSM (above 1 = slower)",
                    plots / f"conj_relative_to_dsm{suffix}.png", layouts=["NSM", "PAX"],
                    band=False, ref=dsm_ref)
        order_penalty(s, plots / f"conj_order_penalty{suffix}.png")
        counters(s, plots / f"conj_counters{suffix}.png",
                 COUNTER_K if COUNTER_K in set(s.k) else sorted(s.k)[0])
        winners(s, out_dir / f"winners{suffix}.md")


if __name__ == "__main__":
    main()
