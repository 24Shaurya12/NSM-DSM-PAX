"""python3 -m advisor <command>  (plan §6.4)

  recommend  --hw H --data D --workload W [--space S] [--model v3] [--params P] [--json]
  predict    --hw H --data D --query "op=sum;cols=0-3;where=c0<5"   (every storage, best options)
  maps       --hw H --data D [--grid selectivity-projection|lookup-selectivity] [--op sum]
  validate   --store STORE --hw H [...]                (advisor.validate: CP5 accuracy)
  evaluate   --store STORE --hw H [...]                (advisor.evaluate: V1-V5, V7 evidence)
  calibrate  --out hw.json                             (advisor.hardware)
  derive     RAW.json --out hw.json
  ground-truth summarize|winners|compare ...           (advisor.ground_truth)
"""
import argparse
import json
import sys
from pathlib import Path

from . import evaluate, ground_truth, hardware, validate
from .candidates import load_space
from .engine import REPO
from .maps import lookup_selectivity, selectivity_projection, write
from .models import make
from .models.base import HardwareModel
from .report import dump_json, sensitivity, to_json, to_text
from .search import SearchContext, make_search
from .selectivity import make_estimator
from .workload import Workload, load_data_profile, load_workload, parse_engine_query

DEFAULT_SPACE = REPO / "configs" / "spaces" / "default.yaml"


def _common(p):
    p.add_argument("--hw", required=True, help="hw.json from `calibrate`")
    p.add_argument("--data", required=True, help="data.yaml")
    p.add_argument("--space", default=str(DEFAULT_SPACE))
    p.add_argument("--model", default="v3")
    p.add_argument("--params", help="fitted parameters (params_<model>.json from validate)")
    p.add_argument("--search", default="exhaustive")
    p.add_argument("--estimator", default="profile")
    p.add_argument("--objective", default="mean", choices=["mean", "total"])


def _setup(args):
    hw = HardwareModel.load(args.hw)
    params = {}
    if args.params:
        params = json.loads(Path(args.params).read_text())["params"]
    model = make(args.model, hw, **params)
    data, hists = load_data_profile(args.data)
    ctx = SearchContext(data, make_estimator(args.estimator, data, hists), args.objective)
    return hw, model, data, ctx, load_space(args.space), make_search(args.search)


def cmd_recommend(args):
    hw, model, data, ctx, space, search = _setup(args)
    workload = load_workload(args.workload, data.cols)
    ranked = search.run(space, workload, model, ctx)
    sens = None if args.no_sensitivity else sensitivity(space, workload, model, ctx, search,
                                                        ranked.best.storage.key())
    if args.json:
        print(dump_json(to_json(ranked, workload, hw, data, args.model, sens)))
    else:
        print(to_text(ranked, workload, hw, data, args.model, sens))
    return 0


def cmd_predict(args):
    hw, model, data, ctx, space, search = _setup(args)
    q = parse_engine_query(args.query, data.cols)
    ranked = search.run(space, Workload("predict", [q]), model, ctx)
    if args.json:
        print(dump_json(to_json(ranked, Workload("predict", [q]), hw, data, args.model)))
        return 0
    n = q.lookups if q.op == "lookup" else data.rows
    print(f"{q.describe()}  on {data.rows:,} x {data.cols}  (model {args.model})")
    print(f"{'storage':16s} {'time':>10s} {'ns/item':>8s} {'x best':>7s} {'mem':>5s} "
          f"{'DRAM MB':>8s}  exec")
    best = ranked.best.cost_ns
    for r in ranked.results:
        c = r.choices[0]
        b = c.cost
        print(f"{r.storage.key():16s} {b.total_ns / 1e6:9.3f}ms {b.total_ns / n:8.3f} "
              f"{b.total_ns / best:7.2f} {100 * b.mem_ns / max(b.mem_ns + b.cpu_ns, 1e-9):4.0f}% "
              f"{b.dram_bytes / 1e6:8.2f}  {c.exec}")
    return 0


def cmd_maps(args):
    hw, model, data, ctx, space, search = _setup(args)
    if args.grid == "selectivity-projection":
        cells, xk, yk, xs, ys = selectivity_projection(space, model, search, data, args.op)
        title = f"Advisor choice: {args.op} of k columns WHERE c0 passes f ({data.cols} cols)"
    else:
        cells, xk, yk, xs, ys = lookup_selectivity(space, model, search, data, args.op)
        title = f"Advisor choice: SELECT * filter vs 10k lookups mix ({data.cols} cols)"
    name = f"map_{args.grid}_{args.op}_{data.cols}cols"
    path = write(cells, xk, yk, xs, ys, args.out, name, title)
    print(f"-> {path}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("validate",):
        return validate.main(argv[1:])
    if argv and argv[0] == "evaluate":
        return evaluate.main(argv[1:])
    if argv and argv[0] in ("calibrate", "derive"):
        return hardware.main(argv)
    if argv and argv[0] == "ground-truth":
        return ground_truth.main(argv[1:])
    ap = argparse.ArgumentParser(prog="python3 -m advisor", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("recommend")
    _common(p)
    p.add_argument("--workload", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-sensitivity", action="store_true")
    p.set_defaults(fn=cmd_recommend)
    p = sub.add_parser("predict")
    _common(p)
    p.add_argument("--query", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_predict)
    p = sub.add_parser("maps")
    _common(p)
    p.add_argument("--grid", default="selectivity-projection",
                   choices=["selectivity-projection", "lookup-selectivity"])
    p.add_argument("--op", default="sum", choices=["sum", "materialize"])
    p.add_argument("--out", default="results/maps")
    p.set_defaults(fn=cmd_maps)
    args = ap.parse_args(argv)
    return args.fn(args)
