#!/usr/bin/env python3
"""Extracts every number the presentation's charts need from the result files into one JSON
(results/presentation/deck_data.json), so the deck is reproducible from the measurements."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from advisor.ground_truth import best_exec  # noqa: E402

GT = REPO / "results/ground_truth/laptop_v1"
VAL = REPO / "results/validation/laptop_v1"
OUT = REPO / "results/presentation"
PAGES = ["1K", "2K", "4K", "8K", "16K", "64K", "256K", "1M", "4M", "16M"]


def layout(s):
    return s.split(":")[0]


def main():
    s = pd.read_csv(GT / "summary.csv")
    s["layout"] = s.storage.map(layout)
    best = best_exec(s)
    best["layout"] = best.storage.map(layout)
    best["ns_item"] = best.latency_ns / best.n_items
    best["dram_item"] = best.dram_read_bytes / best.n_items
    d = {}

    # slide 7: unfiltered SUM, w16, k = 1 / 4 / 16
    scan = best[(best.dataset == "w16_uniform") & (best.family == "scan") & (best.op == "sum")]
    d["scan"] = {st: {p: [round(float(r.ns_item), 3), round(float(r.dram_item), 1)]
                      for p in ("p1", "p25", "p100")
                      for r in scan[(scan.storage == st) & (scan.projection == p)].itertuples()}
                 for st in ("dsm", "nsm", "pax:page=16K")}

    # slide 8: branch misses per row (branchy, one predicate, sum), median over storages
    one = s[(s.n_predicates == 1) & (s.op == "sum")].copy()
    one["rate"] = one.pred_rates.astype(str).str.split(";").str[0].astype(float)
    one["br_item"] = one.branch_misses / one.n_items
    br = one[one["filter"] == "branchy"].groupby(["dataset", "rate"]).br_item.median()
    rates = sorted(one.rate.unique())
    d["branch"] = {"rates": rates,
                   "uniform": [round(float(br[("w16_uniform", r)]), 5) for r in rates],
                   "sorted": [round(float(br[("w16_c0sorted", r)]), 5) for r in rates],
                   "model": [round(2 * r * (1 - r), 5) for r in rates]}
    # branch-free speed-up (branchy / branchless), one predicate, all ops, median per layout
    f1 = s[s.n_predicates == 1].copy()
    f1["rate"] = f1.pred_rates.astype(str).str.split(";").str[0].astype(float)
    pv = f1.pivot_table(index=["dataset", "storage", "template_id", "dsm_batch", "rate"],
                        columns="filter", values="latency_ns").reset_index()
    pv["speedup"] = pv.branchy / pv.branchless
    pv["layout"] = pv.storage.map(layout)
    pv = pv[pv.dataset != "w16_c0sorted"]
    sp = pv.groupby(["layout", "rate"]).speedup.median()
    d["branchfree"] = {lay: [round(float(sp[(lay, r)]), 3) for r in rates]
                       for lay in ("nsm", "dsm", "pax")}

    # slide 9: PAX page size, SUM(c0) on 16 columns
    p1 = best[(best.dataset == "w16_uniform") & (best.family == "scan") & (best.op == "sum") &
              (best.projection == "p1")].set_index("storage")
    d["pagesize"] = {"pages": PAGES,
                     "ns": [round(float(p1.loc[f"pax:page={p}"].ns_item), 3) for p in PAGES],
                     "dram": [round(float(p1.loc[f"pax:page={p}"].dram_item), 1) for p in PAGES],
                     "dsm_ns": round(float(p1.loc["dsm"].ns_item), 3),
                     "dsm_dram": round(float(p1.loc["dsm"].dram_item), 1)}

    # slide 10: lookups, n = 10000 uniform, best storage of each layout
    lk = best[(best.op == "lookup") & (best.lookups == 10000) & (best.id_dist == "uniform")]
    d["lookups"] = {}
    for ds in ("w16_uniform", "w64_uniform"):
        g = lk[lk.dataset == ds].groupby(["projection", "layout"]).ns_item.min()
        d["lookups"][ds] = {lay: [round(float(g[(p, lay)]), 1) for p in ("p1", "p25", "p100")]
                            for lay in ("nsm", "dsm", "pax")}

    # slide 11: winner counts per data set
    w = pd.read_csv(GT / "winners.csv")
    w["best_layout"] = w.best.map(layout)
    wc = w.groupby(["dataset", "best_layout"]).size().unstack(fill_value=0)
    d["winners"] = {ds: {lay: int(wc.loc[ds].get(lay, 0)) for lay in ("nsm", "dsm", "pax")}
                    for ds in wc.index}
    d["close_5"] = round(float((w.margin < 1.05).mean()), 3)
    d["close_10"] = round(float((w.margin < 1.10).mean()), 3)

    # slide 12: worst / best over execution options, per layout
    filt = s[s.n_predicates > 0]
    r = filt.groupby(["dataset", "storage", "logical_id"]).latency_ns.agg(["min", "max"])
    r = (r["max"] / r["min"]).reset_index()
    r["layout"] = r.storage.map(layout)
    d["exec_ratio"] = {lay: {"median": round(float(g[0].median()), 2),
                             "p90": round(float(g[0].quantile(.9)), 2),
                             "max": round(float(g[0].max()), 2)}
                       for lay, g in r.groupby("layout")}
    two = filt[filt.n_predicates == 2]
    po = two.groupby(["dataset", "storage", "logical_id", "order"]).latency_ns.min().unstack()
    po = (po.selective_last / po.selective_first).reset_index()
    po["layout"] = po.storage.map(layout)
    d["order_ratio"] = {lay: {"median": round(float(g[0].median()), 2),
                              "max": round(float(g[0].max()), 2)}
                        for lay, g in po.groupby("layout")}

    # slide 15: calibration curves
    raw = json.loads((REPO / "configs/hw/laptop.raw.json").read_text())
    hw = json.loads((REPO / "configs/hw/laptop.json").read_text())
    d["cal_latency"] = [[x["bytes"], round(x["ns"], 2)] for x in raw["chase_huge"]]
    d["cal_bw"] = [[x["bytes"], round(x["gb_s"], 1)] for x in raw["seq_read"]]
    d["hw"] = {"levels": hw["levels"], "dram": hw["dram"], "freq": hw["freq_ghz"],
               "branch": hw["cpu"]["c_branch_miss"], "tlb": {k: hw["tlb"][k] for k in
                                                             ("l1_entries", "l2_entries", "walk_ns")}}

    # slide 16: ladder + scatter sample (v3, validation points)
    lad = pd.read_csv(VAL / "main/ladder.csv")
    d["ladder"] = lad[["model", "median_abs_err", "p90_abs_err", "within_15%", "top1_accuracy",
                       "median_regret", "max_regret"]].round(4).to_dict("records")
    pr = pd.read_csv(VAL / "cp5/predictions.csv")
    v = pr[(pr.model == "v3") & (pr.split == "validation")]
    v = v.sample(n=min(900, len(v)), random_state=1)
    d["scatter"] = {lay: [[round(float(a / n), 3), round(float(b / n), 3)]
                          for a, b, n in zip(g.latency_ns, g.pred_ns, g.n_items)]
                    for lay, g in v.groupby("layout")}
    by_ds = pd.read_csv(VAL / "cp5/predictions.csv")
    by_ds = by_ds[(by_ds.model == "v3") & (by_ds.split == "validation")]
    d["err_by_dataset"] = by_ds.groupby("dataset").abs_err.median().round(3).to_dict()

    # slide 17: decisions
    dec = pd.read_csv(VAL / "main/decisions.csv")
    summ = pd.read_csv(VAL / "main/decision_summary.csv")
    d["decisions"] = summ.round(4).to_dict("records")
    qs = np.linspace(0, 1, 41)
    d["regret_cdf"] = {c: [round(float(np.quantile(g.regret, q)), 4) for q in qs]
                       for c, g in dec.groupby("chooser")}
    d["cdf_q"] = [round(float(q), 3) for q in qs]

    # slide 18: decision maps
    cells = pd.read_csv(VAL / "main/decision_map_cells.csv")
    d["maps"] = {}
    for (ds, op), g in cells.groupby(["dataset", "op"]):
        d["maps"][f"{ds}|{op}"] = {
            "agree": round(float(g.agree.mean()), 3),
            "cells": [{"proj": r.projection, "rate": float(r.rate), "pred": r.predicted,
                       "meas": r.measured, "agree": bool(r.agree),
                       "near": bool(r.near_boundary), "margin": round(float(r.measured_margin), 3)}
                      for r in g.itertuples()]}

    # slide 19: robustness + generalisation
    d["robustness"] = pd.read_csv(VAL / "main/robustness.csv").round(4).to_dict("records")
    d["recommend"] = {p.stem: p.read_text() for p in sorted((VAL / "recommend").glob("*.txt"))}

    # ---------------- hardware insights (results/insights + calibration + ground truth)
    ins = REPO / "results/insights"
    a = pd.read_csv(ins / "assoc_probe.csv")
    d["assoc"] = {v: {"k": g.k.tolist(), "l1": g.l1d_misses_per_row.round(3).tolist(),
                      "ns": g.ns_per_row.round(3).tolist()} for v, g in a.groupby("variant")}
    e = pd.read_csv(ins / "assoc_engine.csv")
    e["l1"] = e.l1d_misses / e.n_items
    e["ns"] = e.latency_ns / e.n_items
    eg = e.groupby(["k", "layout"])[["l1", "ns"]].median().unstack()
    d["assoc_engine"] = {"k": [int(k) for k in eg.index],
                         **{lay: {"l1": eg[("l1", lay)].round(3).tolist(),
                                  "ns": eg[("ns", lay)].round(3).tolist()}
                            for lay in ("DSM", "NSM", "PAX")}}
    pts = []
    for x in raw["overfetch"]:
        per, run, off = x["period"], x["run"], x["offset"]
        if per >= 8192 and off + run <= 4096:
            rem = (4096 - off - run) / 64
            extra = (x["dram_read_bytes"] - run) / 64
            if extra > -2:
                pts.append([rem, round(extra, 1), run, off])
    d["overfetch_pts"] = pts
    d["stride_bytes"] = [[x["stride"], round(x["dram_read_bytes"], 1)] for x in raw["stride"]]
    nr = best[(best.layout == "nsm") & (best.family == "scan") & (best.op == "sum")]
    d["nsm_rows"] = {ds: {p: [round(float(r.ns_item), 2), round(float(r.dram_item), 1)]
                          for p in ("p1", "p25", "p100")
                          for r in nr[(nr.dataset == ds) & (nr.projection == p)].itertuples()}
                     for ds in ("w16_uniform", "w64_uniform")}
    mp = {}
    sm = s.copy()
    sm["ns_i"] = sm.latency_ns / sm.n_items
    sm["cyc_i"] = sm.cycles / sm.n_items
    sm["br_i"] = sm.branch_misses / sm.n_items
    for f in ("0.25", "0.5"):
        for lay, st in (("nsm", "nsm"), ("dsm", "dsm"), ("pax", "pax:page=64K")):
            q = sm[(sm.n_predicates == 1) & (sm.op == "sum") & (sm.dataset == "w16_uniform") &
                   (sm.pred_rates.astype(str) == f) & (sm.storage == st) & (sm.projection == "p1")]
            if lay == "dsm":
                q = q[q.dsm_batch == 1024]
            b_, c_ = q[q["filter"] == "branchy"].iloc[0], q[q["filter"] == "branchless"].iloc[0]
            mp[f"{lay}|{f}"] = {"cyc_per_miss": round(float((b_.cyc_i - c_.cyc_i) / (b_.br_i - c_.br_i)), 1),
                               "dram_B": round(float(b_.dram_read_bytes / b_.n_items), 1)}
    d["mispredict"] = mp
    br1 = one[(one["filter"] == "branchy") & (one.dataset == "w16_uniform")].copy()
    br1["layout"] = br1.storage.map(layout)
    gb = br1.groupby(["layout", "rate"]).br_item.median()
    rr = [r for r in rates if 0 < r < 1]
    d["predictor"] = {"rates": rr, **{lay: [round(float(gb[(lay, r)] / min(r, 1 - r)), 3) for r in rr]
                                      for lay in ("nsm", "dsm", "pax")},
                      "coin": [round(2 * r * (1 - r) / min(r, 1 - r), 3) for r in rr]}
    lk2 = s[(s.op == "lookup") & (s.id_dist == "uniform") & (s.lookups == 100000) &
            (s.projection == "p100")].copy()
    lk2["tlb"] = lk2.dtlb_misses / lk2.n_items
    lk2["ns"] = lk2.latency_ns / lk2.n_items
    d["lookup_tlb"] = {}
    for ds in ("w16_uniform", "w64_uniform"):
        g = lk2[lk2.dataset == ds].set_index("storage")
        d["lookup_tlb"][ds] = {"pages": PAGES,
                               "tlb": [round(float(g.loc[f"pax:page={p}"].tlb), 2) for p in PAGES],
                               "ns": [round(float(g.loc[f"pax:page={p}"].ns), 1) for p in PAGES],
                               "dsm": [round(float(g.loc["dsm"].tlb), 2), round(float(g.loc["dsm"].ns), 1)],
                               "nsm": [round(float(g.loc["nsm"].tlb), 2), round(float(g.loc["nsm"].ns), 1)]}
    d["tlb_curve"] = [[c["pages"], c["extra_ns"], c["walks_per_load"]] for c in hw["tlb"]["curve"]]
    d["mlp"] = hw["mlp"]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "deck_data.json").write_text(json.dumps(d, indent=1))
    print("->", OUT / "deck_data.json")


if __name__ == "__main__":
    main()
