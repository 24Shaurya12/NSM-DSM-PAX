#!/usr/bin/env python3
"""Figures and LaTeX tables for the layout-advisor section of the report (report/sections/advisor.tex).

Reads the validation outputs of scripts/run_report.sh and the ground-truth store, and writes
  report/figures/advisor_accuracy.pdf   predicted vs measured (held-out points) + regret CDF
  report/generated/advisor_hw.tex       calibrated machine constants
  report/generated/advisor_ladder.tex   model ladder: accuracy and decision quality
  report/generated/advisor_decisions.tex advisor vs fixed rules on 1000 random workloads

  python3 scripts/report_advisor.py [--val results/report/validation] [--hw configs/hw/laptop_v2.json]
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from advisor.evaluate import predict  # noqa: E402
from advisor.models.base import HardwareModel  # noqa: E402
from advisor.validate import load_points, make_split  # noqa: E402

# Reference categorical palette (fixed order), text inks and surface.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
LAYOUT_STYLE = {"nsm": (SERIES[0], "o", "NSM"), "dsm": (SERIES[1], "s", "DSM"),
                "pax": (SERIES[2], "^", "PAX")}
CHOOSERS = [  # (key in decisions.csv, label, colour, line style, width)
    ("advisor", "advisor (v3)", SERIES[0], "-", 2.0),
    ("always-dsm", "always DSM", SERIES[1], "--", 1.4),
    ("dsm-unless-lookups>25%", "DSM unless\nlookups > 25%", SERIES[2], "-.", 1.4),
    ("always-pax:page=16K", "always PAX 16 KB", SERIES[3], ":", 1.6),
    ("always-nsm", "always NSM", SERIES[4], (0, (5, 1, 1, 1)), 1.4),
]


def style_axes(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(INK2)
    ax.tick_params(colors=INK2, labelsize=7)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def figure(points, pred, valid, decisions, out):
    plt.rcParams.update({"font.size": 8, "axes.labelcolor": INK, "text.color": INK})
    fig, (a, b) = plt.subplots(1, 2, figsize=(7.0, 2.45), gridspec_kw={"width_ratios": [1, 1.1]})

    # (a) predicted vs measured, held-out points, by layout
    meas = points.latency_ns / 1e6
    pr = pred / 1e6
    lay = points.storage.str.split(":").str[0]
    for key in ("pax", "dsm", "nsm"):  # largest group first so the smaller ones stay visible
        c, m, label = LAYOUT_STYLE[key]
        sel = valid & (lay == key)
        a.scatter(meas[sel], pr[sel], s=4, marker=m, color=c, alpha=0.35, linewidths=0,
                  label=f"{label} ({sel.sum():,})", rasterized=True)
    lo, hi = 0.03, 150
    xs = np.array([lo, hi])
    a.plot(xs, xs, color=INK, linewidth=0.8)
    for f in (0.75, 1.25):
        a.plot(xs, xs * f, color=INK2, linewidth=0.6, linestyle="--")
    a.set_xscale("log"); a.set_yscale("log")
    a.set_xlim(lo, hi); a.set_ylim(lo, hi)
    a.set_xlabel("measured latency (ms)"); a.set_ylabel("predicted latency (ms)")
    err = (pred[valid] / points.latency_ns[valid] - 1).abs()
    a.set_title(f"(a) v3 on {valid.sum():,} held-out points: median |err| "
                f"{100 * err.median():.0f}%", fontsize=8, color=INK)
    a.text(0.97, 0.04, "dashed: ±25%", transform=a.transAxes, ha="right", fontsize=7, color=INK2)
    style_axes(a)
    leg = a.legend(loc="upper left", fontsize=7, frameon=False, markerscale=2.5, handletextpad=0.2)
    for h in leg.legend_handles:
        h.set_alpha(1)

    # (b) regret CDF over the 1000 workloads
    for key, label, c, ls, lw in CHOOSERS:
        r = np.sort(decisions.loc[decisions.chooser == key, "regret"].to_numpy())
        if not len(r):
            continue
        y = np.arange(1, len(r) + 1) / len(r)
        b.step(r, y, where="post", color=c, linestyle=ls, linewidth=lw, label=label)
    b.set_xscale("log")
    b.set_xlim(1.0, 14)
    b.set_xticks([1, 1.5, 2, 3, 5, 10])
    b.set_xticklabels(["1", "1.5", "2", "3", "5", "10"])
    b.set_ylim(0, 1.01)
    b.set_xlabel("regret = measured time of the choice ÷ best (log)")
    b.set_ylabel("fraction of workloads")
    b.set_title("(b) 1000 random workloads", fontsize=8, color=INK)
    style_axes(b)
    b.legend(loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=6.5, frameon=False,
             handlelength=2.8)

    fig.tight_layout(w_pad=1.2)
    fig.savefig(out, dpi=300, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def pct(x):
    return f"{100 * x:.0f}\\%"


def hw_table(hw, out):
    lv = {x["name"]: x for x in hw["levels"]}
    rows = []
    for n in ("L1", "L2", "L3", "DRAM"):
        x = lv[n]
        size = x.get("bytes")
        s = "--" if not size else (f"{size // 1024} KB" if size < 1 << 20 else
                                    f"{size / (1 << 20):.3g} MB")
        rows.append(f"{n} & {s} & {x['seq_gb_s']:.1f} & {x['lat_ns']:.1f} \\\\")
    d, t, c = hw.get("dram", {}), hw.get("tlb", {}), hw.get("cpu", {})
    out.write_text(
        "\\begin{tabular}{lrrr}\n\\toprule\nLevel & Size & Read (GB/s) & Latency (ns) \\\\\n"
        "\\midrule\n" + "\n".join(rows) + "\n\\midrule\n"
        f"\\multicolumn{{4}}{{l}}{{DRAM bytes per line miss $B_{{\\mathrm{{eff}}}}$ = "
        f"{d.get('B_eff', 0):.0f}; MLP = {d.get('mlp', 0):.1f}}} \\\\\n"
        f"\\multicolumn{{4}}{{l}}{{TLB {t.get('l1_entries', 0):.0f}/{t.get('l2_entries', 0):.0f}"
        f" entries, walk {t.get('walk_ns', 0):.0f} ns; branch miss "
        f"{c.get('c_branch_miss', 0):.0f} cycles}} \\\\\n"
        "\\bottomrule\n\\end{tabular}\n")


def ladder_table(val, out):
    lad = pd.read_csv(val / "main" / "ladder.csv").set_index("model")
    names = {"v0": "v0 bytes $\\div$ bandwidth", "v1": "v1 Manegold et al.",
             "v2": "v2 + modern memory", "v3": "v3 + CPU effects"}
    rows = []
    for m in ("v0", "v1", "v2", "v3"):
        r = lad.loc[m]
        rows.append(f"{names[m]} & {pct(r.median_abs_err)} & {pct(r.p90_abs_err)} & "
                    f"{pct(r.top1_accuracy)} & {r.median_regret:.2f} & {r.max_regret:.2f} \\\\")
    out.write_text(
        "\\begin{tabular}{lrrrrr}\n\\toprule\n & \\multicolumn{2}{c}{$|$error$|$} & "
        "\\multicolumn{3}{c}{decisions} \\\\\n\\cmidrule(lr){2-3}\\cmidrule(lr){4-6}\n"
        "Model & med. & p90 & top-1 & med. & max \\\\\n\\midrule\n" + "\n".join(rows) +
        "\n\\bottomrule\n\\end{tabular}\n")


def decisions_table(val, out):
    d = pd.read_csv(val / "main" / "decision_summary.csv").set_index("chooser")
    rows = []
    for key, label, *_ in CHOOSERS:
        r = d.loc[key]
        tex = label.replace("\n", " ").replace("%", "\\%").replace(">", "$>$")
        rows.append(f"{tex} & {pct(r.top1_accuracy)} & {r.median_regret:.2f} & "
                    f"{r.p90_regret:.2f} & {r.max_regret:.2f} \\\\")
    out.write_text(
        "\\begin{tabular}{lrrrr}\n\\toprule\n & & \\multicolumn{3}{c}{regret} \\\\\n"
        "\\cmidrule(lr){3-5}\nChooser & top-1 & med. & p90 & max \\\\\n\\midrule\n" +
        "\n".join(rows) + "\n\\bottomrule\n\\end{tabular}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--val", default="results/report/validation")
    ap.add_argument("--store", default="results/ground_truth/laptop_v1")
    ap.add_argument("--hw", default="configs/hw/laptop_v2.json")
    ap.add_argument("--report", default="report")
    args = ap.parse_args()
    val, rep = REPO / args.val, REPO / args.report
    (rep / "figures").mkdir(parents=True, exist_ok=True)
    (rep / "generated").mkdir(parents=True, exist_ok=True)

    hw_dict = json.loads((REPO / args.hw).read_text())
    params = json.loads((val / "main" / "params_v3.json").read_text())["params"]
    points, data = load_points(REPO / args.store)
    pred, *_ = predict(points, data, HardwareModel(hw_dict), "v3", None, params, do_fit=False)
    valid = ~make_split(points, "random:0.3")
    decisions = pd.read_csv(val / "main" / "decisions.csv")

    figure(points, pred, valid, decisions, rep / "figures" / "advisor_accuracy.pdf")
    hw_table(hw_dict, rep / "generated" / "advisor_hw.tex")
    ladder_table(val, rep / "generated" / "advisor_ladder.tex")
    decisions_table(val, rep / "generated" / "advisor_decisions.tex")
    err = (pred[valid] / points.latency_ns[valid] - 1).abs()
    print(f"held-out points {valid.sum()}, median |err| {err.median():.3f}, "
          f"p90 {err.quantile(0.9):.3f}")


if __name__ == "__main__":
    main()
