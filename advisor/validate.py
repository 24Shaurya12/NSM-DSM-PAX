"""Model validation against a ground-truth store (plan CP5 / V1).

For every measured point (data set, storage, template, DSM batch, filter mode) each model
predicts the time and the counter-comparable quantities (DRAM bytes, TLB walks, branch misses).
Parameters of fitted models are estimated on a *calibration split* only (default: 30% of the
logical queries of the first data set, chosen by a seeded hash) and the errors are reported on
the remaining points.

  python3 -m advisor.validate --store results/ground_truth/laptop_v1 --hw configs/hw/laptop.json \
      [--models v0,v1,v2,v3] [--fit v3] [--out results/validation/laptop_v1]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .engine import parse_size
from .ground_truth import Store, summarize
from .models import make
from .models.base import DataInfo, ExecInfo, HardwareModel, StorageInfo, query_from_template

FIT_PARAMS = ["k_mem", "k_cpu", "k_branch", "k_over", "gamma", "pax_lookup_extra"]


def load_points(store_path):
    store = Store(store_path)
    s = (pd.read_csv(store.summary_csv) if store.summary_csv.exists()
         else summarize(store.measurements(), store.templates()))
    t = store.templates()
    cfg = yaml.safe_load((Path(store_path) / "config.yaml").read_text())
    data = {}
    for d in cfg["datasets"]:
        cols = int(d["cols"])
        rows = int(d["rows"]) if "rows" in d else parse_size(d["size"]) // (cols * 8)
        data[d["name"]] = DataInfo(rows, cols, dict(d.get("dist", {})))
    keep = [c for c in ("cols", "where", "pred_rates", "lookups", "id_dist", "op")
            if c in t.columns and (c == "op" or c not in s.columns)]
    t = t[["dataset", "template_id"] + keep].rename(columns={"op": "t_op"})
    s = s.merge(t, on=["dataset", "template_id"], how="left")
    return s, data


def storage_info(key):
    parts = key.split(":")
    page = 0
    for p in parts[1:]:
        k, _, v = p.partition("=")
        if k == "page":
            page = parse_size(v)
    return StorageInfo(parts[0], page)


def point_inputs(row, data, rate_scale=1.0):
    d = data[row.dataset]
    t = {"cols": row.cols, "where": row.where if isinstance(row.where, str) else "",
         "pred_rates": row.pred_rates, "lookups": row.lookups, "id_dist": row.id_dist,
         "op": row.t_op, "n_cols": d.cols}
    rates = None
    if rate_scale != 1.0 and isinstance(row.pred_rates, str):
        rates = [min(1.0, float(x) * rate_scale) for x in row.pred_rates.split(";")]
    q = query_from_template(t, rates)
    ex = ExecInfo(int(row.dsm_batch) or 1024,
                  row.filter if row.filter in ("branchy", "branchless") else "branchy")
    return storage_info(row.storage), ex, q, d


def calibration_split(points, frac=0.3, dataset=None, seed="cp5"):
    ds = dataset or points.dataset.iloc[0]

    def pick(lid):
        h = int(hashlib.sha1(f"{seed}:{lid}".encode()).hexdigest(), 16)
        return (h % 1000) / 1000 < frac

    return (points.dataset == ds) & points.logical_id.map(pick)


def make_split(points, spec="random:0.3"):
    """Boolean mask of the points used for fitting.
      random:F                 F of the logical queries of the first data set (seeded hash)
      dataset:NAME[:nolookup]  every point of one data set (optionally without lookups):
                               the V4 generalisation split
    """
    kind, _, arg = spec.partition(":")
    if kind == "random":
        return calibration_split(points, float(arg or 0.3))
    if kind == "dataset":
        name, _, opt = arg.partition(":")
        m = points.dataset == name
        if opt == "nolookup":
            m &= points.op != "lookup"
        return m
    raise ValueError(f"unknown split '{spec}'")


def components_table(model, points, data, rate_scale=1.0):
    rows = []
    for r in points.itertuples(index=False):
        st, ex, q, d = point_inputs(r, data, rate_scale)
        mem, terms, branch, over, desc = model.components(st, ex, q, d)
        extra_n = q.lookups if (st.layout == "pax" and q.op == "lookup") else 0
        tlb = sum(v[0] + v[1] for k, v in mem.misses.items() if k.startswith("TLB2")) or \
            sum(v[0] + v[1] for k, v in mem.misses.items() if k.startswith("TLB"))
        rows.append({"mem_ns": mem.total_ns, "cpu_ns": sum(terms.values()),
                     "branch_ns": branch, "over_ns": over, "pax_lookups": extra_n,
                     "pred_dram_bytes": mem.dram_bytes, "pred_tlb": tlb,
                     "pred_branch_misses": desc.branch_misses})
    return pd.DataFrame(rows, index=points.index)


def total_from(comp, p, overlap, freq):
    mem = p["k_mem"] * comp.mem_ns
    cpu = (p["k_cpu"] * comp.cpu_ns + p["k_branch"] * comp.branch_ns + p["k_over"] * comp.over_ns
           + p["pax_lookup_extra"] * comp.pax_lookups / freq)
    if overlap:
        return np.maximum(mem, cpu) + p["gamma"] * np.minimum(mem, cpu)
    return mem + cpu


def fit(model, comp, measured, names=FIT_PARAMS):
    from scipy.optimize import least_squares
    names = [n for n in names if n in model.params and (n != "gamma" or model.overlap)]
    lo = {"gamma": 0.0, "pax_lookup_extra": 0.0}
    hi = {"gamma": 1.0, "pax_lookup_extra": 200.0}
    x0 = [model.params[n] for n in names]

    def resid(x):
        p = dict(model.params, **dict(zip(names, x)))
        return np.log(total_from(comp, p, model.overlap, model.hw.freq) / measured)

    res = least_squares(resid, x0, bounds=([lo.get(n, 0.05) for n in names],
                                           [hi.get(n, 20.0) for n in names]))
    model.params.update(dict(zip(names, res.x)))
    return dict(zip(names, res.x))


def error_table(df, by):
    g = df.groupby(by)
    return pd.DataFrame({
        "n": g.size(),
        "median_abs_err": g.abs_err.median(),
        "p90_abs_err": g.abs_err.quantile(0.9),
        "median_signed": g.err.median(),
    }).reset_index()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m advisor.validate")
    ap.add_argument("--store", required=True)
    ap.add_argument("--hw", required=True)
    ap.add_argument("--models", default="v0,v1,v2,v3")
    ap.add_argument("--fit", default="v3", help="models whose parameters are fitted")
    ap.add_argument("--fit-frac", type=float, default=0.3)
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    points, data = load_points(args.store)
    hw = HardwareModel.load(args.hw)
    calib = calibration_split(points, args.fit_frac)
    out = Path(args.out) if args.out else Path("results/validation") / Path(args.store).name
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 220)
    all_pred, ladder = [], []
    for name in args.models.split(","):
        model = make(name, hw)
        comp = components_table(model, points, data)
        fitted = {}
        if name in args.fit.split(","):
            fitted = fit(model, comp[calib], points.latency_ns[calib].to_numpy())
            (out / f"params_{name}.json").write_text(json.dumps(
                {"model": name, "store": str(args.store), "hw": str(args.hw),
                 "fit_frac": args.fit_frac, "params": model.params}, indent=2))
        if name == "v0":
            pred = model.params["k_mem"] * comp.pred_dram_bytes / hw.dram_gb_s
            # v0 counts lines at the cache line size, not B_eff
        else:
            pred = total_from(comp, model.params, model.overlap, hw.freq)
        df = points[["dataset", "storage", "template_id", "logical_id", "family", "op",
                     "projection", "dsm_batch", "filter", "latency_ns", "n_items"]].copy()
        df["layout"] = df.storage.str.split(":").str[0]
        df["model"] = name
        df["pred_ns"] = pred
        df["err"] = df.pred_ns / df.latency_ns - 1
        df["abs_err"] = df.err.abs()
        df["split"] = np.where(calib, "calibration", "validation")
        for c in ("pred_dram_bytes", "pred_tlb", "pred_branch_misses", "mem_ns", "cpu_ns",
                  "branch_ns", "over_ns"):
            df[c] = comp[c]
        for c in ("dram_read_bytes", "dtlb_misses", "branch_misses"):
            if c in points:
                df[c] = points[c]
        all_pred.append(df)
        v = df[df.split == "validation"]
        ladder.append({"model": name, "fitted": ", ".join(f"{k}={x:.3g}" for k, x in fitted.items()),
                       "median_abs_err": v.abs_err.median(), "p90_abs_err": v.abs_err.quantile(.9),
                       "within_15%": (v.abs_err <= 0.15).mean()})
    pred = pd.concat(all_pred)
    pred.to_csv(out / "predictions.csv", index=False)
    lad = pd.DataFrame(ladder)
    lad.to_csv(out / "ladder.csv", index=False)
    print("Model ladder (validation split):")
    print(lad.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    best = pred[pred.model == args.models.split(",")[-1]]
    v = best[best.split == "validation"]
    groups = error_table(v, ["layout", "op", "family"])
    groups.to_csv(out / "errors_by_group.csv", index=False)
    print(f"\nErrors by group ({best.model.iloc[0]}):")
    print(groups.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    if "dram_read_bytes" in v:
        c = v.assign(meas_B=v.dram_read_bytes / v.n_items, pred_B=v.pred_dram_bytes / v.n_items,
                     meas_tlb=v.dtlb_misses / v.n_items, pred_tlb_i=v.pred_tlb / v.n_items)
        cnt = c.groupby(["layout", "op"])[["meas_B", "pred_B", "meas_tlb", "pred_tlb_i"]].median()
        print("\nCounters, median per item (measured vs predicted):")
        print(cnt.to_string(float_format=lambda x: f"{x:.4g}"))
    worst = v.sort_values("abs_err", ascending=False).head(15)
    print("\nWorst points:")
    print(worst[["dataset", "storage", "template_id", "dsm_batch", "filter", "latency_ns",
                 "pred_ns", "err"]].to_string(index=False))
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
