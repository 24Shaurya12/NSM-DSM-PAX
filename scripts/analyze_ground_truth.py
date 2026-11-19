#!/usr/bin/env python3
"""Summarises a ground-truth store into <store>/report.md: run conditions, noise, winners per
query family, and the hardware-level facts (bandwidth, bytes per row, lookup latency, branch
misses, frequency) that the cost model (phase 4) must reproduce.

  python3 scripts/analyze_ground_truth.py results/ground_truth/laptop_v1
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from advisor.ground_truth import Store, best_exec, summarize, winners  # noqa: E402


def md(df, floatfmt="{:.3g}"):
    df = df.copy()
    for c in df.columns:
        if df[c].dtype.kind == "f":
            df[c] = df[c].map(lambda x: "" if pd.isna(x) else floatfmt.format(x))
    head = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "|" + "---|" * len(df.columns)
    rows = ["| " + " | ".join(map(str, r)) + " |" for r in df.itertuples(index=False)]
    return "\n".join([head, sep] + rows)


def layout_of(storage):
    return storage.split(":")[0]


def main():
    store = Store(sys.argv[1])
    s = summarize(store.measurements(), store.templates())
    s.to_csv(store.summary_csv, index=False)
    runs = pd.read_csv(store.runs_csv)
    best = best_exec(s)
    w = winners(s)
    w.to_csv(store.path / "winners.csv", index=False)
    s["layout"] = s.storage.map(layout_of)
    best["layout"] = best.storage.map(layout_of)
    out = [f"# Ground truth: {store.path.name}\n"]

    # --- conditions and noise
    ok = runs[runs.status == "ok"]
    out.append("## Run conditions\n")
    out.append(f"- {len(ok)} engine runs ok, {int((runs.status != 'ok').sum())} failed; "
               f"{len(s)} measured points, {int(s.latency_ns_count.sum())} samples; "
               f"{ok.seconds.sum() / 60:.0f} min of engine time.")
    out.append(f"- AC power: {sorted(ok.ac_power.astype(str).unique())}; governor "
               f"{sorted(ok.governor.unique())}, EPP {sorted(ok.epp.unique())}; "
               f"virtualisation {sorted(ok.virt.unique())}; counters: {ok.counters.iloc[0]}.")
    out.append(f"- Package temperature {ok.temp_c.min():.0f}–{ok.end_temp_c.max():.0f} °C; "
               f"load average at start {ok.loadavg1.min():.2f}–{ok.loadavg1.max():.2f}.")
    if "ghz" in s:
        out.append(f"- Effective core clock (cycles ÷ time) per point: median {s.ghz.median():.2f} "
                   f"GHz, 5th–95th pct {s.ghz.quantile(.05):.2f}–{s.ghz.quantile(.95):.2f} GHz.")
    long = s[s.latency_ns >= 1e6]
    out.append(f"- Sample-to-sample coefficient of variation: median {100 * s.cv.median():.1f}%, "
               f"90th pct {100 * s.cv.quantile(.9):.1f}% (points ≥ 1 ms: median "
               f"{100 * long.cv.median():.1f}%, 90th {100 * long.cv.quantile(.9):.1f}%).\n")

    # --- winners
    out.append("## Which storage wins (best execution options per storage)\n")
    w["family"] = w.logical_id.str.split("/").str[0]
    w["best_layout"] = w.best.map(layout_of)
    tab = w.groupby(["dataset", "family", "op"]).best_layout.value_counts().unstack(fill_value=0)
    out.append(md(tab.reset_index()))
    out.append("")
    out.append(f"Close calls: {100 * (w.margin < 1.05).mean():.0f}% of queries have a runner-up "
               f"within 5%; {100 * (w.margin < 1.10).mean():.0f}% within 10%.\n")
    cols = ["dataset", "logical_id", "best", "best_ns_per_item", "runner_up", "margin"] + \
        [c for c in ("nsm_x", "dsm_x", "pax_x") if c in w]
    out.append("### Every query\n")
    out.append(md(w[cols].sort_values(["dataset", "logical_id"])))
    out.append("")

    # --- scans: bytes and bandwidth
    out.append("## Hardware facts\n")
    scan = best[(best.family == "scan") & (best.op == "sum")].copy()
    if len(scan) and "dram_read_bytes" in scan:
        scan["dram_B_per_row"] = scan.dram_read_bytes / scan.n_items
        scan["GB_s"] = scan.dram_read_bytes / scan.latency_ns
        scan["ns_per_row"] = scan.latency_ns / scan.n_items
        piv = scan.pivot_table(index=["dataset", "storage"], columns="projection",
                               values=["ns_per_row", "dram_B_per_row", "GB_s"])
        piv.columns = [f"{a}:{b}" for a, b in piv.columns]
        out.append("### Unfiltered SUM: time, DRAM bytes and bandwidth per row\n")
        out.append(md(piv.reset_index()))
        out.append("")
        dram_bound = scan[scan.dram_B_per_row > 32]
        out.append(f"DRAM-bound scans (> 32 B/row) sustain {dram_bound.GB_s.median():.1f} GB/s "
                   f"(range {dram_bound.GB_s.min():.1f}–{dram_bound.GB_s.max():.1f}) on one core.\n")

    look = best[best.op == "lookup"].copy()
    if len(look):
        look["ns_per_lookup"] = look.latency_ns / look.n_items
        piv = look.pivot_table(index=["dataset", "projection", "id_dist", "lookups"],
                               columns="layout", values="ns_per_lookup", aggfunc="min")
        out.append("### Point lookups: ns per lookup (best storage of each layout)\n")
        out.append(md(piv.reset_index()))
        out.append("")
        if "dram_read_bytes" in look:
            u = look[(look.id_dist == "uniform") & (look.lookups == look.lookups.max())]
            u = u.assign(B_per_lookup=u.dram_read_bytes / u.n_items,
                         dtlb_per_lookup=u.dtlb_misses / u.n_items)
            out.append("Largest uniform lookup batches (DRAM bytes and dTLB misses per lookup):\n")
            out.append(md(u[["dataset", "storage", "projection", "ns_per_lookup", "B_per_lookup",
                             "dtlb_per_lookup"]].sort_values(["dataset", "projection",
                                                              "ns_per_lookup"])))
            out.append("")

    pax = best[(best.layout == "pax") & (best.family == "scan") & (best.op == "sum")].copy()
    if len(pax):
        pax["page"] = pax.storage.str.split("=").str[1]
        pax["ns_per_row"] = pax.latency_ns / pax.n_items
        order = {p: i for i, p in enumerate(["1K", "2K", "4K", "8K", "16K", "64K", "256K", "1M",
                                             "4M", "16M"])}
        pax["o"] = pax.page.map(order)
        piv = pax.sort_values("o").pivot_table(index="page", columns=["dataset", "projection"],
                                               values="ns_per_row", sort=False)
        piv.columns = [f"{a}:{b}" for a, b in piv.columns]
        out.append("### PAX page size vs unfiltered SUM (ns/row)\n")
        out.append(md(piv.reset_index()))
        dsm = scan[scan.storage == "dsm"].set_index(["dataset", "projection"]).ns_per_row
        out.append("\nDSM for comparison: " + ", ".join(
            f"{d}:{p} {v:.2f}" for (d, p), v in dsm.items()) + "\n")

    # --- execution options
    filt = s[s.n_predicates > 0]
    if len(filt):
        out.append("## Execution options\n")
        g = filt.groupby(["dataset", "storage", "logical_id"])
        bl = filt.pivot_table(index=["dataset", "storage", "template_id", "dsm_batch"],
                              columns="filter", values="latency_ns")
        if {"branchy", "branchless"} <= set(bl.columns):
            bl = bl.reset_index().merge(
                filt[["dataset", "template_id", "pred_rates", "n_predicates"]].drop_duplicates(),
                on=["dataset", "template_id"])
            bl["speedup"] = bl.branchy / bl.branchless
            bl["rate0"] = bl.pred_rates.str.split(";").str[0].astype(float)
            bl["layout"] = bl.storage.map(layout_of)
            piv = bl[bl.n_predicates == 1].pivot_table(index="rate0", columns=["layout"],
                                                       values="speedup", aggfunc="median")
            out.append("### Branch-free speed-up (branchy ÷ branchless time), one predicate\n")
            out.append(md(piv.reset_index()))
            out.append("")
        dsm = filt[filt.storage == "dsm"]
        if len(dsm):
            bb = dsm.loc[dsm.groupby(["dataset", "logical_id"]).latency_ns.idxmin()]
            out.append("Best DSM batch size (count of queries): " + ", ".join(
                f"{b}: {n}" for b, n in bb.dsm_batch.value_counts().sort_index().items()))
            worst = dsm.groupby(["dataset", "logical_id"]).latency_ns.agg(["min", "max"])
            out.append(f"; worst ÷ best over (batch, filter, order) for DSM: median "
                       f"{(worst['max'] / worst['min']).median():.2f}×, max "
                       f"{(worst['max'] / worst['min']).max():.1f}×.\n")
        two = filt[filt.n_predicates == 2]
        if len(two):
            po = two.groupby(["dataset", "storage", "logical_id", "order"]).latency_ns.min(
            ).unstack()
            if {"selective_first", "selective_last"} <= set(po.columns):
                r = (po.selective_last / po.selective_first).reset_index()
                r["layout"] = r.storage.map(layout_of)
                out.append("### Predicate order: selective-last ÷ selective-first (best other options)\n")
                out.append(md(r.groupby(["dataset", "layout"])[0].describe()[
                    ["count", "min", "50%", "max"]].reset_index()))
                out.append("")
        if "branch_misses_per_item" in filt:
            br = filt[(filt.n_predicates == 1) & (filt["filter"] == "branchy")].copy()
            br["rate0"] = br.pred_rates.str.split(";").str[0].astype(float)
            br["model"] = 2 * br.rate0 * (1 - br.rate0)
            piv = br[br.op == "sum"].groupby(["dataset", "rate0"]).agg(
                measured=("branch_misses_per_item", "median"), model_2f1f=("model", "first"))
            out.append("### Branch misses per row, branchy single predicate (median over storages)\n")
            out.append(md(piv.reset_index()))
            out.append("")

    (store.path / "report.md").write_text("\n".join(out) + "\n")
    print(f"-> {store.path / 'report.md'}")


if __name__ == "__main__":
    main()
