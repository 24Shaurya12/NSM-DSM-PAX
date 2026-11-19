#!/usr/bin/env python3
"""Ground-truth runner (plan §3): measure every storage configuration of the candidate space on
every query template and execution option, for each data set of a run configuration.

One engine process per (data set, storage configuration): the table is generated and the
layout built once, then all templates x execution options are checked against the reference
and timed. Results go to results/ground_truth/<name>/ (see advisor/ground_truth.py). The run is
resumable: (data set, storage) pairs already measured are skipped, so an interrupted run can be
restarted with the same command.

Examples:
  python3 scripts/run_ground_truth.py --dry-run
  python3 scripts/run_ground_truth.py                         # full run (~1 h, keep idle, on AC)
  python3 scripts/run_ground_truth.py --sample 0.1 --name laptop_v1_repeat   # repeatability
  python3 scripts/run_ground_truth.py --datasets w16_uniform --storage nsm,dsm --name quick
Environment: CPU=<id> overrides the core the engine is pinned to.
"""
import argparse
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from advisor.candidates import exec_grid, load_space, predicate_orders, storage_configs  # noqa: E402
from advisor.engine import (DataSpec, Engine, EngineError, Measurement, machine_state,  # noqa: E402
                            parse_size)
from advisor.ground_truth import Store, summarize  # noqa: E402
from advisor.workloads.templates import expand_templates, load_template_file  # noqa: E402


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["datasets"] = [dict(d) for d in cfg["datasets"]]
    return cfg


def make_dataset(d):
    return DataSpec(d["name"], int(d["cols"]), size=parse_size(d["size"]) if "size" in d else 0,
                    rows=int(d.get("rows", 0)), dist=dict(d.get("dist", {})))


def n_points(storage, templates, grid):
    """Execution-option combinations the engine will time (mirrors main.cpp variants())."""
    n = 0
    for t in templates:
        batch = storage.layout == "dsm" and t.op != "lookup" and not (t.op == "sum" and not t.where)
        n += (len(grid.dsm_batch) if batch else 1) * (len(grid.filter) if t.where else 1)
    return n


def write_machine_info(out, cpu):
    cmd = f'source scripts/common.sh && OUT="{out}" CPU={cpu} write_machine_info'
    subprocess.run(["bash", "-c", cmd], cwd=REPO, check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default="configs/ground_truth/default.yaml")
    ap.add_argument("--name", help="store name (default: config name)")
    ap.add_argument("--out", help="store directory (default results/ground_truth/<name>)")
    ap.add_argument("--datasets", help="comma-separated data set names to run")
    ap.add_argument("--storage", help="comma-separated storage keys or layout names to run")
    ap.add_argument("--sample", type=float, default=1.0,
                    help="fraction of templates per data set (seeded), e.g. 0.1")
    ap.add_argument("--sample-seed", type=int, default=7)
    ap.add_argument("--iterations", type=int, help="override measure.iterations")
    ap.add_argument("--binary", default=str(REPO / "build" / "scan_bench"))
    ap.add_argument("--dry-run", action="store_true", help="list the jobs and exit")
    args = ap.parse_args()

    cfg = load_config(REPO / args.config)
    name = args.name or cfg["name"]
    out = Path(args.out) if args.out else REPO / "results" / "ground_truth" / name
    space = load_space(REPO / cfg["space"])
    tfile = load_template_file(REPO / cfg["templates"])
    grid, orders = exec_grid(space), predicate_orders(space)
    m = cfg.get("measure", {})
    measure = Measurement(modes=m.get("modes", ["cold"]),
                          iterations=args.iterations or m.get("iterations", 5),
                          warmup=m.get("warmup", 1), min_sample_ms=m.get("min_sample_ms", 20.0))
    seed = int(cfg.get("seed", 1))

    wanted_ds = set(args.datasets.split(",")) if args.datasets else None
    wanted_st = set(args.storage.split(",")) if args.storage else None
    jobs, template_rows = [], []
    for d in cfg["datasets"]:
        if wanted_ds and d["name"] not in wanted_ds:
            continue
        data = make_dataset(d)
        templates = expand_templates(tfile, data, orders, d.get("families"))
        if args.sample < 1.0:
            rng = random.Random(f"{args.sample_seed}:{data.name}")
            templates = [t for t in templates if rng.random() < args.sample]
        template_rows += [{"dataset": data.name, "template_id": t.id,
                           **{k: v for k, v in t.row().items() if k != "id"},
                           "engine_query": t.engine_query(seed)} for t in templates]
        for s in storage_configs(space, data):
            if wanted_st and s.key() not in wanted_st and s.layout not in wanted_st:
                continue
            jobs.append((data, s, templates))

    total_points = sum(n_points(s, ts, grid) for _, s, ts in jobs)
    print(f"store: {out}")
    print(f"{len(jobs)} engine runs, {len(template_rows)} templates, {total_points} points "
          f"x {measure.iterations} samples, modes {measure.modes}")
    if args.dry_run:
        for data, s, ts in jobs:
            print(f"  {data.name:14s} {s.key():16s} {len(ts):4d} templates "
                  f"{n_points(s, ts, grid):5d} points")
        return 0

    subprocess.run(["make", "-s"], cwd=REPO, check=True)
    engine = Engine(args.binary)
    store = Store(out)
    store.create()
    store.write_templates(template_rows)
    resolved = dict(cfg, name=name, sample=args.sample, sample_seed=args.sample_seed,
                    measure=vars(measure), space_resolved=space, cpu=engine.cpu,
                    started=datetime.now().isoformat(timespec="seconds"))
    (out / "config.yaml").write_text(yaml.safe_dump(resolved, sort_keys=False))
    write_machine_info(out, engine.cpu)

    done = store.completed()
    todo = [j for j in jobs if (j[0].name, j[1].key()) not in done]
    print(f"{len(jobs) - len(todo)} runs already in the store, {len(todo)} to go; "
          f"engine pinned to CPU {engine.cpu}")
    t_start, points_done = time.time(), 0
    todo_points = sum(n_points(s, ts, grid) for _, s, ts in todo)
    for i, (data, s, templates) in enumerate(todo):
        state = machine_state(engine.cpu)
        if state["ac_power"] not in ("1", "unknown"):
            print("  warning: not on AC power; timings may be throttled", flush=True)
        if state["loadavg1"] > 1.5:
            print(f"  warning: load average {state['loadavg1']}; keep the machine idle", flush=True)
        run_id = store.next_run_id()
        info = {"run_id": run_id, "dataset": data.name, "storage": s.key(),
                "started": datetime.now().isoformat(timespec="seconds"),
                "n_templates": len(templates), **state}
        queries = [t.engine_query(seed) for t in templates]
        try:
            df, seconds = engine.run(s, data, queries, grid, measure, log=out / "engine.log")
        except EngineError as e:
            store.add_run({**info, "status": "failed", "message": str(e)[:300]})
            print(f"[{i + 1}/{len(todo)}] {data.name} {s.key()}: FAILED {e}", flush=True)
            continue
        end = machine_state(engine.cpu)
        df.insert(0, "run_id", run_id)
        df.insert(1, "dataset", data.name)
        df.insert(2, "storage", s.key())
        df.insert(3, "template_id", df["query"])
        store.add_measurements(df)
        counters = [c for c in ("cycles", "llc_misses", "dtlb_misses", "dram_read_bytes")
                    if c in df and df[c].notna().any()]
        store.add_run({**info, "status": "ok", "seconds": round(seconds, 1),
                       "n_samples": len(df), "counters": "|".join(counters),
                       "end_loadavg1": end["loadavg1"], "end_temp_c": end["temp_c"]})
        points_done += n_points(s, templates, grid)
        elapsed = time.time() - t_start
        eta = elapsed / points_done * (todo_points - points_done)
        print(f"[{i + 1}/{len(todo)}] {data.name} {s.key()}: {len(df)} samples in "
              f"{seconds:.0f} s (temp {state['temp_c']:.0f}->{end['temp_c']:.0f} C); "
              f"ETA {eta / 60:.0f} min", flush=True)

    summary = summarize(store.measurements(), store.templates())
    summary.to_csv(store.summary_csv, index=False)
    print(f"done in {(time.time() - t_start) / 60:.1f} min; {len(summary)} points -> "
          f"{store.summary_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
