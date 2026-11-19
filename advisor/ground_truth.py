"""Ground-truth measurement store (results/ground_truth/<name>/).

Files:
  config.yaml        the resolved run configuration (data sets, space, templates, measurement)
  templates.csv      every template per data set (id, logical_id, op, projection, predicates...)
  runs.csv           one row per engine process: data set, storage, status, duration, machine state
  measurements.csv   every timed sample (engine CSV + data set, storage, template id, run id)
  summary.csv        per (data set, storage, template, dsm_batch, filter, mode): medians etc.
  machine.txt        machine description (scripts/common.sh write_machine_info)

Measurements are kept per (storage config, query, execution options), so the time of any
workload mix is a weighted sum and thousands of mixes can be evaluated without re-running.

CLI:
  python3 -m advisor.ground_truth summarize STORE
  python3 -m advisor.ground_truth winners   STORE [--dataset D]
  python3 -m advisor.ground_truth compare   STORE_A STORE_B     (repeatability)
"""
import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .engine import COUNTERS

POINT = ["dataset", "storage", "template_id", "dsm_batch", "filter", "mode"]
RUN_FIELDS = ["run_id", "dataset", "storage", "status", "started", "seconds", "n_templates",
              "n_samples", "counters", "message", "cpu", "ac_power", "governor", "epp",
              "cur_mhz", "no_turbo", "loadavg1", "temp_c", "perf_event_paranoid", "virt",
              "end_loadavg1", "end_temp_c"]


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.runs_csv = self.path / "runs.csv"
        self.meas_csv = self.path / "measurements.csv"
        self.templates_csv = self.path / "templates.csv"
        self.summary_csv = self.path / "summary.csv"

    def create(self):
        self.path.mkdir(parents=True, exist_ok=True)

    def completed(self):
        """(dataset, storage) pairs already measured successfully."""
        if not self.runs_csv.exists():
            return set()
        runs = pd.read_csv(self.runs_csv)
        ok = runs[runs.status == "ok"]
        return set(zip(ok.dataset, ok.storage))

    def next_run_id(self):
        if not self.runs_csv.exists():
            return 0
        runs = pd.read_csv(self.runs_csv)
        return int(runs.run_id.max()) + 1 if len(runs) else 0

    def add_run(self, info):
        new = not self.runs_csv.exists()
        with open(self.runs_csv, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=RUN_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(info)

    def add_measurements(self, df):
        df.to_csv(self.meas_csv, mode="a", header=not self.meas_csv.exists(), index=False)

    def write_templates(self, rows):
        pd.DataFrame(rows).to_csv(self.templates_csv, index=False)

    def measurements(self):
        return pd.read_csv(self.meas_csv)

    def templates(self):
        return pd.read_csv(self.templates_csv)


def summarize(meas, templates=None):
    """Median (and spread) of the samples of every measured point."""
    meas = meas.copy()
    meas["ns_per_item"] = meas.latency_ns / meas.n_items
    agg = {"latency_ns": ["median", "min", "mean", "std", "count"], "ns_per_item": "median",
           "matches": "first", "selectivity": "first", "n_items": "first"}
    for c in COUNTERS:
        if c in meas and meas[c].notna().any():
            agg[c] = "median"
    s = meas.groupby(POINT, sort=False).agg(agg)
    s.columns = ["latency_ns" if a == "median" and c == "latency_ns" else
                 c if a in ("median", "first") else f"{c}_{a}" for c, a in s.columns]
    s = s.reset_index()
    s["cv"] = s.latency_ns_std / s.latency_ns_mean
    if "cycles" in s:
        s["ghz"] = s.cycles / s.latency_ns
    for c in ("dram_read_bytes", "dram_write_bytes", "llc_misses", "dtlb_misses",
              "branch_misses", "instructions", "cycles"):
        if c in s:
            s[f"{c}_per_item"] = s[c] / s.n_items
    if templates is not None:
        keep = ["dataset", "template_id", "logical_id", "family", "op", "projection",
                "n_project", "n_predicates", "pred_rates", "target_selectivity", "order",
                "lookups", "id_dist"]
        s = s.merge(templates[keep], on=["dataset", "template_id"], how="left")
    return s


def best_exec(summary):
    """For every (data set, storage, logical query): the fastest execution options (DSM batch,
    filter mode, predicate order). This is the storage configuration's time for that query."""
    idx = summary.groupby(["dataset", "storage", "logical_id"]).latency_ns.idxmin()
    return summary.loc[idx].reset_index(drop=True)


def winners(summary, tie=0.05):
    """Per (data set, logical query): best storage, runner-up and margins."""
    best = best_exec(summary)
    rows = []
    for (ds, lid), g in best.groupby(["dataset", "logical_id"], sort=False):
        g = g.sort_values("latency_ns")
        first, second = g.iloc[0], g.iloc[1] if len(g) > 1 else g.iloc[0]
        by_layout = g.assign(layout=g.storage.str.split(":").str[0]).groupby(
            "layout").latency_ns.min()
        rows.append({
            "dataset": ds, "logical_id": lid, "op": first.op, "projection": first.projection,
            "selectivity": first.selectivity, "n_items": first.n_items,
            "best": first.storage, "best_ns_per_item": first.ns_per_item,
            "best_exec": f"batch={first.dsm_batch} filter={first.filter} order={first.order}",
            "runner_up": second.storage, "margin": second.latency_ns / first.latency_ns,
            "within_tie": int((g.latency_ns <= first.latency_ns * (1 + tie)).sum()),
            **{f"{lay}_x": by_layout[lay] / first.latency_ns for lay in by_layout.index},
        })
    return pd.DataFrame(rows)


def compare(a, b, tol=0.05):
    """Repeatability: ratio of medians for the points both summaries contain."""
    m = a.merge(b, on=POINT, suffixes=("_a", "_b"))
    m["ratio"] = m.latency_ns_b / m.latency_ns_a
    return m, float(np.mean(np.abs(m.ratio - 1) <= tol)) if len(m) else float("nan")


def _load_summary(path):
    store = Store(path)
    if store.summary_csv.exists():
        return pd.read_csv(store.summary_csv)
    return summarize(store.measurements(), store.templates())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m advisor.ground_truth")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("summarize"); p.add_argument("store")
    p = sub.add_parser("winners"); p.add_argument("store"); p.add_argument("--dataset")
    p = sub.add_parser("compare"); p.add_argument("a"); p.add_argument("b")
    p.add_argument("--tol", type=float, default=0.05)
    args = ap.parse_args(argv)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 500)
    if args.cmd == "summarize":
        store = Store(args.store)
        s = summarize(store.measurements(), store.templates())
        s.to_csv(store.summary_csv, index=False)
        print(f"{len(s)} points -> {store.summary_csv}")
    elif args.cmd == "winners":
        w = winners(_load_summary(args.store))
        if args.dataset:
            w = w[w.dataset == args.dataset]
        out = Path(args.store) / "winners.csv"
        w.to_csv(out, index=False)
        print(w.to_string(index=False, float_format=lambda x: f"{x:.3g}"))
        print(f"-> {out}")
    elif args.cmd == "compare":
        m, frac = compare(_load_summary(args.a), _load_summary(args.b), args.tol)
        print(f"{len(m)} common points; {100 * frac:.1f}% within ±{100 * args.tol:.0f}%; "
              f"median |ratio-1| = {100 * np.median(np.abs(m.ratio - 1)):.1f}%; "
              f"90th pct = {100 * np.percentile(np.abs(m.ratio - 1), 90):.1f}%")
        worst = m.assign(dev=np.abs(m.ratio - 1)).sort_values("dev", ascending=False)
        print(worst[POINT + ["latency_ns_a", "latency_ns_b", "ratio"]].head(15).to_string(
            index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
