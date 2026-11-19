"""Phase 6 validation (plan §7): the evidence that the advisor picks correctly.

  V1  accuracy: predicted vs measured time per point (median / p90 |error| per group), and
      predicted vs measured counters (DRAM bytes, TLB walks, branch misses)
  V2  decision quality on random workload mixes: top-1 accuracy (ties within 5% count as
      correct) and regret = measured time of the chosen storage / measured time of the best
  V3  the same workloads under fixed rules (always NSM / DSM / PAX-16K, "DSM unless lookups",
      best single storage in hindsight)
  V4  generalisation: fit on one data set (e.g. 16 columns, no lookups), test on the rest
      (run with --fit-split dataset:w16_uniform:nolookup)
  V5  decision maps: predicted vs measured winner over selectivity x projection (and lookups)
  V7  robustness: perturb calibration constants and selectivity estimates; how often does the
      decision change and what does it cost?
(V6 = the same command on a second machine's store and hw.json.)

  python3 -m advisor evaluate --store results/ground_truth/laptop_v1 --hw configs/hw/laptop.json \
      [--models v0,v1,v2,v3] [--fit-split random:0.3] [--workloads 1000] [--out DIR]

Everything is computed from the measurement store: a workload's measured time on a storage
configuration is the weighted sum of its queries' measured times (each with the execution
options the advisor would pick, or the best measured ones for the oracle and the baselines).
"""
import argparse
import copy
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .models import make
from .models.base import HardwareModel
from .validate import components_table, fit, load_points, make_split, total_from
from .workloads import generate as wlgen

LAYOUT_COLORS = {"nsm": "#2a78d6", "dsm": "#eb6834", "pax": "#1baf7a"}
# fixed categorical slots per chooser (validated default palette); layouts keep their colours
CHOOSER_COLORS = {"advisor": "#6250d6", "always-nsm": "#2a78d6", "always-dsm": "#eb6834",
                  "always-pax:page=16K": "#1baf7a", "dsm-unless-lookups>25%": "#eda100",
                  "best-fixed-in-hindsight": "#008300"}
INK, INK_MUTED, GRID = "#1f1f1e", "#5f5e5a", "#e4e3df"
TIE = 0.05


# --------------------------------------------------------------------------- predictions

def predict(points, data, hw, model_name, mask, params=None, do_fit=True, rate_scale=1.0):
    model = make(model_name, hw, **(params or {}))
    comp = components_table(model, points, data, rate_scale)
    fitted = {}
    if do_fit and mask is not None and mask.any() and model_name != "v0":
        fitted = fit(model, comp[mask], points.latency_ns[mask].to_numpy())
    if model_name == "v0":
        pred = model.params["k_mem"] * comp.pred_dram_bytes / model.hw.dram_gb_s
    else:
        pred = total_from(comp, model.params, model.overlap, model.hw.freq)
    return pd.Series(np.asarray(pred, float), index=points.index), comp, dict(model.params), fitted


def query_table(points, pred):
    """Per (data set, storage, logical query): best measured time, best predicted time, and
    the measured time of the execution options the model picks."""
    df = points[["dataset", "storage", "logical_id", "latency_ns"]].assign(pred_ns=pred.values)
    g = df.groupby(["dataset", "storage", "logical_id"], sort=False)
    out = pd.DataFrame({"meas": g.latency_ns.min(), "pred": g.pred_ns.min()})
    idx = g.pred_ns.idxmin()
    out["meas_at_pred"] = df.loc[idx.values, "latency_ns"].values
    return out


# --------------------------------------------------------------------------- V2 / V3

def matrices(qt):
    """{data set: (storages, logical ids, meas, pred, meas_at_pred matrices)} (storage x query),
    keeping only storages measured on every query of the data set."""
    out = {}
    for ds, g in qt.groupby(level="dataset"):
        g = g.droplevel("dataset")
        tabs = {c: g[c].unstack("logical_id") for c in ("meas", "pred", "meas_at_pred")}
        full = tabs["meas"].notna().all(axis=1)
        tabs = {c: t[full] for c, t in tabs.items()}
        lids = list(tabs["meas"].columns)
        out[ds] = (list(tabs["meas"].index), lids,
                   {c: t[lids].to_numpy(float) for c, t in tabs.items()})
    return out


def evaluate_workloads(workloads, mats, ops_by_lid, choosers):
    rows = []
    for w in workloads:
        storages, lids, m = mats[w["dataset"]]
        col = {l: i for i, l in enumerate(lids)}
        idx = [col[q["logical_id"]] for q in w["queries"] if q["logical_id"] in col]
        if len(idx) < 1:
            continue
        wt = np.array([q["weight"] for q in w["queries"] if q["logical_id"] in col])
        wt = wt / wt.sum()
        meas = m["meas"][:, idx] @ wt
        pred = m["pred"][:, idx] @ wt
        real = m["meas_at_pred"][:, idx] @ wt
        best = meas.min()
        lookup_share = sum(x for q, x in zip([lids[i] for i in idx], wt)
                           if ops_by_lid.get(q) == "lookup")
        ctx = {"storages": storages, "meas": meas, "pred": pred, "lookup_share": lookup_share}
        for name, choose in choosers.items():
            s = choose(ctx, w)
            if s is None:
                continue
            i = storages.index(s)
            # the advisor also picks the execution options; baselines get the best measured ones
            cost = real[i] if name == "advisor" else meas[i]
            rows.append({"workload": w["id"], "dataset": w["dataset"], "chooser": name,
                         "choice": s, "regret": cost / best, "storage_regret": meas[i] / best,
                         "correct": meas[i] <= best * (1 + TIE), "oracle": storages[int(meas.argmin())]})
    return pd.DataFrame(rows)


def default_choosers(best_fixed):
    def fixed(key):
        return lambda c, w: key if key in c["storages"] else None

    return {
        "advisor": lambda c, w: c["storages"][int(np.argmin(c["pred"]))],
        "always-nsm": fixed("nsm"),
        "always-dsm": fixed("dsm"),
        "always-pax:page=16K": fixed("pax:page=16K"),
        "dsm-unless-lookups>25%": lambda c, w: "nsm" if c["lookup_share"] > 0.25 else "dsm",
        "best-fixed-in-hindsight": lambda c, w: best_fixed.get(w["dataset"]),
    }


def best_fixed_storage(workloads, mats):
    """The single storage per data set with the lowest mean regret over all its workloads (an
    upper bound on what any fixed rule can achieve)."""
    out = {}
    for ds, (storages, lids, m) in mats.items():
        col = {l: i for i, l in enumerate(lids)}
        regs = []
        for w in workloads:
            if w["dataset"] != ds:
                continue
            idx = [col[q["logical_id"]] for q in w["queries"]]
            wt = np.array([q["weight"] for q in w["queries"]])
            meas = m["meas"][:, idx] @ (wt / wt.sum())
            regs.append(meas / meas.min())
        if regs:
            out[ds] = storages[int(np.argmin(np.mean(regs, axis=0)))]
    return out


def decision_summary(res):
    g = res.groupby("chooser", sort=False)
    return pd.DataFrame({
        "workloads": g.size(), "top1_accuracy": g.correct.mean(),
        "median_regret": g.regret.median(), "p90_regret": g.regret.quantile(0.9),
        "max_regret": g.regret.max(), "mean_regret": g.regret.mean(),
        "median_storage_regret": g.storage_regret.median(),
        "max_storage_regret": g.storage_regret.max(),
    }).reset_index()


# --------------------------------------------------------------------------- V5 maps

def decision_map_cells(points, qt, dataset, family="f1"):
    t = points[(points.dataset == dataset) & (points.family == family)][
        ["logical_id", "op", "projection", "pred_rates"]].drop_duplicates("logical_id")
    q = qt.xs(dataset, level="dataset")
    cells = []
    for r in t.itertuples(index=False):
        sub = q.xs(r.logical_id, level="logical_id")
        meas, pred = sub.meas.sort_values(), sub.pred.sort_values()
        cells.append({"dataset": dataset, "op": r.op, "projection": r.projection,
                      "rate": float(str(r.pred_rates).split(";")[0]),
                      "measured": meas.index[0], "predicted": pred.index[0],
                      "measured_margin": meas.iloc[1] / meas.iloc[0] if len(meas) > 1 else np.inf,
                      "choice_regret": sub.meas[pred.index[0]] / meas.iloc[0]})
    df = pd.DataFrame(cells)
    if len(df):
        df["agree"] = df.measured == df.predicted
        df["agree_layout"] = df.measured.str.split(":").str[0] == df.predicted.str.split(":").str[0]
        df["near_boundary"] = df.measured_margin < 1.10
    return df


def plot_map(cells, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch, Rectangle
    projs = [p for p in ("p1", "p25", "s25", "p100") if p in set(cells.projection)]
    rates = sorted(cells.rate.unique())
    fig, ax = plt.subplots(figsize=(1.05 * len(rates) + 2.4, 0.9 * len(projs) + 1.6))
    for i, p in enumerate(projs):
        for j, r in enumerate(rates):
            c = cells[(cells.projection == p) & (cells.rate == r)]
            if not len(c):
                continue
            c = c.iloc[0]
            lay = c.predicted.split(":")[0]
            ax.add_patch(Rectangle((j, i), 1, 1, facecolor=LAYOUT_COLORS.get(lay, "#999"),
                                   alpha=0.8 if c.agree else 0.35, edgecolor="#ffffff",
                                   linewidth=2))
            if not c.agree:  # outline + measured winner when the advisor disagrees
                ax.add_patch(Rectangle((j + 0.06, i + 0.06), 0.88, 0.88, fill=False,
                                       edgecolor=INK, linewidth=1.2,
                                       linestyle="--" if c.near_boundary else "-"))
            short = lambda s: s.replace("pax:page=", "pax ")  # noqa: E731
            ax.text(j + 0.5, i + 0.62, short(c.predicted), ha="center", va="center", fontsize=7,
                    color=INK)
            sub = "✓" if c.agree else f"meas: {short(c.measured)}"
            ax.text(j + 0.5, i + 0.32, sub, ha="center", va="center", fontsize=6, color=INK_MUTED)
    ax.set_xlim(0, len(rates))
    ax.set_ylim(0, len(projs))
    ax.set_xticks([j + 0.5 for j in range(len(rates))])
    ax.set_xticklabels([f"{r:g}" for r in rates], fontsize=8, color=INK_MUTED)
    ax.set_yticks([i + 0.5 for i in range(len(projs))])
    ax.set_yticklabels(projs, fontsize=8, color=INK_MUTED)
    ax.set_xlabel("selectivity (c0 pass rate)", color=INK_MUTED)
    ax.set_ylabel("projection", color=INK_MUTED)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(length=0)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    handles = [Patch(facecolor=LAYOUT_COLORS[k], label=f"predicted {k.upper()}")
               for k in LAYOUT_COLORS if k in {p.split(':')[0] for p in cells.predicted}]
    handles += [Patch(facecolor="none", edgecolor=INK, label="disagrees with measured"),
                Patch(facecolor="none", edgecolor=INK, linestyle="--",
                      label="disagrees, measured margin < 10%")]
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False,
              fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor="#ffffff")
    plt.close(fig)


# --------------------------------------------------------------------------- V1 plots

def plot_scatter(points, pred, mask_valid, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(5.6, 5.0))
    lay = points.storage.str.split(":").str[0]
    x = points.latency_ns / points.n_items
    y = pred / points.n_items
    for name, color in LAYOUT_COLORS.items():
        m = (lay == name) & mask_valid
        ax.scatter(x[m], y[m], s=9, color=color, alpha=0.55, linewidths=0, label=name.upper())
    lo, hi = min(x.min(), y.min()) * 0.8, max(x.max(), y.max()) * 1.25
    ax.plot([lo, hi], [lo, hi], color=INK_MUTED, linewidth=1)
    for f in (0.85, 1.15):
        ax.plot([lo, hi], [lo * f, hi * f], color=INK_MUTED, alpha=0.5, linewidth=0.8,
                linestyle="--")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("measured ns per item", color=INK_MUTED)
    ax.set_ylabel("predicted ns per item", color=INK_MUTED)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    ax.grid(color=GRID, linewidth=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(frameon=False, fontsize=8, title="dashed: ±15%", title_fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor="#ffffff")
    plt.close(fig)


def plot_regret_cdf(res, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    for name, color in CHOOSER_COLORS.items():
        r = np.sort(res[res.chooser == name].regret.to_numpy())
        if not len(r):
            continue
        ax.plot(r, np.arange(1, len(r) + 1) / len(r), color=color, linewidth=2, label=name,
                linestyle="--" if name == "best-fixed-in-hindsight" else "-")
    ax.set_xscale("log")
    from matplotlib.ticker import FixedLocator, NullFormatter
    top = max(2.0, res.regret.max() * 1.2)
    base = (1, 1.1, 1.25, 1.5, 2, 3, 5, 10, 20, 50) if top < 4 else (1, 1.25, 1.5, 2, 3, 5, 10, 20, 50)
    ticks = [t for t in base if t <= top]
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.set_xticklabels([f"{t:g}" for t in ticks], fontsize=8)
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlabel("regret = measured time of choice ÷ best measured (log)", color=INK_MUTED)
    ax.set_ylabel("fraction of workloads", color=INK_MUTED)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    ax.grid(color=GRID, linewidth=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor="#ffffff")
    plt.close(fig)


# --------------------------------------------------------------------------- V7

def perturbed_hw(hw, what, factor):
    h = copy.deepcopy(hw)
    if what == "dram_bandwidth":
        for lv in h["levels"]:
            if lv["name"] == "DRAM":
                lv["seq_gb_s"] *= factor
    elif what == "latency":
        for lv in h["levels"]:
            lv["lat_ns"] *= factor
        for k in ("l1_miss_ns", "walk_ns"):
            if h.get("tlb", {}).get(k) is not None:
                h["tlb"][k] *= factor
    elif what == "cpu":
        h["cpu"] = {k: (v * factor if isinstance(v, (int, float)) else v)
                    for k, v in h["cpu"].items()}
    elif what == "mlp":
        if h.get("dram", {}).get("mlp"):
            h["dram"]["mlp"] *= factor
    return h


# --------------------------------------------------------------------------- report

def md(df, fmt="{:.3g}"):
    df = df.copy()
    for c in df.columns:
        if df[c].dtype.kind == "f":
            df[c] = df[c].map(lambda v: "" if pd.isna(v) else fmt.format(v))
    head = "| " + " | ".join(map(str, df.columns)) + " |"
    return "\n".join([head, "|" + "---|" * len(df.columns)] +
                     ["| " + " | ".join(map(str, r)) + " |" for r in df.itertuples(index=False)])


def accuracy_table(points, pred, mask, by):
    err = pred / points.latency_ns - 1
    df = points.assign(err=err, abs_err=err.abs())[mask]
    g = df.groupby(by)
    return pd.DataFrame({"n": g.size(), "median_abs_err": g.abs_err.median(),
                         "p90_abs_err": g.abs_err.quantile(0.9),
                         "median_signed_err": g.err.median()}).reset_index()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m advisor evaluate")
    ap.add_argument("--store", required=True)
    ap.add_argument("--hw", required=True)
    ap.add_argument("--models", default="v0,v1,v2,v3", help="last one is 'the advisor'")
    ap.add_argument("--fit-split", default="random:0.3")
    ap.add_argument("--fit", default="v3", help="models whose parameters are fitted")
    ap.add_argument("--workloads", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-robustness", action="store_true")
    ap.add_argument("--quick", action="store_true",
                    help="4 robustness perturbations instead of 10")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else Path("results/validation") / Path(args.store).name
    (out / "plots").mkdir(parents=True, exist_ok=True)
    points, data = load_points(args.store)
    hw_dict = json.loads(Path(args.hw).read_text())
    hw = HardwareModel(hw_dict)
    fit_mask = make_split(points, args.fit_split)
    valid = ~fit_mask
    ops_by_lid = dict(zip(points.logical_id, points.op))
    lids = {ds: sorted(g.logical_id.unique()) for ds, g in points.groupby("dataset")}
    workloads = wlgen.generate(lids, args.workloads, args.seed)
    wlgen.save(workloads, out / "workloads.json")
    rep = [f"# Validation: {Path(args.store).name}\n",
           f"hw: `{args.hw}`; fit split: `{args.fit_split}` ({int(fit_mask.sum())} fit points, "
           f"{int(valid.sum())} validation points); {len(workloads)} random workloads "
           f"(seed {args.seed}).\n"]

    models = args.models.split(",")
    ladder, decisions, preds = [], [], {}
    for name in models:
        pred, comp, params, fitted = predict(points, data, hw, name, fit_mask,
                                             do_fit=name in args.fit.split(","))
        preds[name] = (pred, comp, params)
        err = (pred / points.latency_ns - 1).abs()
        qt = query_table(points, pred)
        mats = matrices(qt)
        res = evaluate_workloads(workloads, mats, ops_by_lid,
                                 {"advisor": default_choosers({})["advisor"]})
        ladder.append({"model": name,
                       "fitted": ", ".join(f"{k}={v:.3g}" for k, v in fitted.items()) or "-",
                       "median_abs_err": err[valid].median(), "p90_abs_err": err[valid].quantile(.9),
                       "within_15%": (err[valid] <= .15).mean(),
                       "top1_accuracy": res.correct.mean(), "median_regret": res.regret.median(),
                       "median_storage_regret": res.storage_regret.median(),
                       "p90_regret": res.regret.quantile(.9), "max_regret": res.regret.max()})
        if name == models[-1]:
            (out / f"params_{name}.json").write_text(json.dumps({"params": params}, indent=2))
    lad = pd.DataFrame(ladder)
    lad.to_csv(out / "ladder.csv", index=False)
    rep += ["## Model ladder (V1 accuracy on validation points; V2 decisions)\n", md(lad), ""]

    # ---- V1 for the advisor's model
    name = models[-1]
    pred, comp, params = preds[name]
    rep.append(f"## V1: accuracy of {name} (validation points)\n")
    acc = accuracy_table(points, pred, valid, ["dataset"])
    rep += [md(acc), ""]
    grp = accuracy_table(points.assign(layout=points.storage.str.split(":").str[0]), pred, valid,
                         ["layout", "op", "family"])
    grp.to_csv(out / "errors_by_group.csv", index=False)
    rep += ["By layout × op × family:\n", md(grp), ""]
    worst_groups = grp[grp.median_abs_err > 0.30]
    rep.append(f"Groups worse than 30% median error: {len(worst_groups)}.\n")
    if "dram_read_bytes" in points:
        c = points.assign(layout=points.storage.str.split(":").str[0],
                          dram_ratio=comp.pred_dram_bytes / points.dram_read_bytes,
                          tlb_ratio=comp.pred_tlb / points.dtlb_misses.replace(0, np.nan),
                          br_ratio=comp.pred_branch_misses / points.branch_misses.replace(0, np.nan))
        cr = c[valid].groupby(["layout", "op"])[["dram_ratio", "tlb_ratio", "br_ratio"]].median()
        rep += ["Counters, median predicted ÷ measured (1 = exact):\n", md(cr.reset_index()), ""]
    plot_scatter(points, pred, valid, out / "plots" / f"v1_scatter_{name}.png",
                 f"{name}: predicted vs measured (validation points)")
    rep.append(f"![scatter](plots/v1_scatter_{name}.png)\n")
    worst = points.assign(pred_ns=pred, err=pred / points.latency_ns - 1)[valid]
    worst = worst.reindex(worst.err.abs().sort_values(ascending=False).index).head(15)
    rep += ["Worst points:\n", md(worst[["dataset", "storage", "template_id", "dsm_batch", "filter",
                                         "latency_ns", "pred_ns", "err"]]), ""]

    # ---- V2 / V3
    qt = query_table(points, pred)
    mats = matrices(qt)
    best_fixed = best_fixed_storage(workloads, mats)
    res = evaluate_workloads(workloads, mats, ops_by_lid, default_choosers(best_fixed))
    res.to_csv(out / "decisions.csv", index=False)
    summ = decision_summary(res)
    summ.to_csv(out / "decision_summary.csv", index=False)
    rep += [f"## V2/V3: decisions on {len(workloads)} random workloads\n",
            f"Best fixed storage in hindsight: {best_fixed}. Baselines use the best measured "
            "execution options. For the advisor, `regret` includes its own execution-option "
            "choices (measured at the options it picks); `storage_regret` gives it the best "
            "measured options too, i.e. judges the storage choice alone.\n",
            md(summ), ""]
    adv = res[res.chooser == "advisor"]
    per_ds = adv.groupby("dataset").agg(top1=("correct", "mean"), median_regret=("regret", "median"),
                                        max_regret=("regret", "max")).reset_index()
    rep += ["Advisor per data set:\n", md(per_ds), ""]
    wrong = adv[~adv.correct].groupby(["dataset", "oracle", "choice"]).size().reset_index(name="n")
    rep += ["Wrong decisions (oracle → chosen):\n", md(wrong.sort_values("n", ascending=False)), ""]
    plot_regret_cdf(res, out / "plots" / "v2_regret_cdf.png",
                    f"Regret over {len(workloads)} random workloads")
    rep.append("![regret](plots/v2_regret_cdf.png)\n")

    # ---- V5
    rep.append("## V5: decision maps (one-predicate queries)\n")
    all_cells = []
    for ds in sorted(points.dataset.unique()):
        cells = decision_map_cells(points, qt, ds)
        if not len(cells):
            continue
        all_cells.append(cells)
        for op in sorted(cells.op.unique()):
            c = cells[cells.op == op]
            fn = f"v5_map_{ds}_{op}.png"
            plot_map(c, out / "plots" / fn, f"{ds} · {op}: predicted winner (✓ = measured agrees)")
            rep.append(f"- {ds} {op}: exact storage agrees in {100 * c.agree.mean():.0f}% of cells, "
                       f"layout in {100 * c.agree_layout.mean():.0f}%; disagreements near a "
                       f"boundary (measured margin < 10%): {int((~c.agree & c.near_boundary).sum())}"
                       f"/{int((~c.agree).sum())}; worst cell regret {c.choice_regret.max():.2f}x "
                       f"![map](plots/{fn})")
    if all_cells:
        pd.concat(all_cells).to_csv(out / "decision_map_cells.csv", index=False)
    rep.append("")

    # ---- V7
    if not args.no_robustness:
        rep.append("## V7: robustness (no refit; same workloads)\n")
        base_choice = adv.set_index("workload").choice
        rows = []
        perts = [("dram_bandwidth", 0.8), ("dram_bandwidth", 1.2), ("latency", 0.8),
                 ("latency", 1.2), ("cpu", 0.8), ("cpu", 1.2), ("mlp", 0.5), ("mlp", 2.0),
                 ("selectivity", 0.5), ("selectivity", 2.0)]
        if args.quick:
            perts = [("dram_bandwidth", 0.8), ("cpu", 1.2), ("selectivity", 0.5),
                     ("selectivity", 2.0)]
        for what, f in perts:
            if what == "selectivity":
                p2, *_ = predict(points, data, hw, name, None, params, False, rate_scale=f)
            else:
                p2, *_ = predict(points, data, HardwareModel(perturbed_hw(hw_dict, what, f)), name,
                                 None, params, False)
            r2 = evaluate_workloads(workloads, matrices(query_table(points, p2)), ops_by_lid,
                                    {"advisor": default_choosers({})["advisor"]})
            changed = (r2.set_index("workload").choice != base_choice).mean()
            rows.append({"perturbation": f"{what} x{f:g}", "decisions_changed": changed,
                         "top1_accuracy": r2.correct.mean(), "median_regret": r2.regret.median(),
                         "max_regret": r2.regret.max()})
        rob = pd.DataFrame(rows)
        rob.to_csv(out / "robustness.csv", index=False)
        rep += [md(rob), ""]

    (out / "report.md").write_text("\n".join(rep) + "\n")
    print("\n".join(rep[:6]))
    print(md(summ))
    print(f"-> {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
