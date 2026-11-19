"""Advisor tests (CP6): parsers, estimators, search, report, and the registries (a dummy model
and a dummy search strategy plug in without touching any other file).
Run: python3 tests/advisor/test_advisor.py"""
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from advisor.candidates import load_space  # noqa: E402
from advisor.engine import REPO  # noqa: E402
from advisor.models import make, register_model  # noqa: E402
from advisor.models.base import CostBreakdown, CostModel, DataInfo, HardwareModel  # noqa: E402
from advisor.report import to_json, to_text  # noqa: E402
from advisor.search import SearchContext, make_search, register_search  # noqa: E402
from advisor.search.base import RankedResults, Search, StorageResult, best_choice  # noqa: E402
from advisor.selectivity import make_estimator  # noqa: E402
from advisor.workload import (Workload, load_data_profile, parse_engine_query,  # noqa: E402
                              parse_where, query_from_dict)

HW = {
    "freq_ghz": 3.0,
    "levels": [{"name": "L1", "bytes": 48 << 10, "line": 64, "seq_gb_s": 100, "lat_ns": 1},
               {"name": "L2", "bytes": 1 << 20, "line": 64, "seq_gb_s": 60, "lat_ns": 4},
               {"name": "L3", "bytes": 16 << 20, "line": 64, "seq_gb_s": 35, "lat_ns": 15},
               {"name": "DRAM", "bytes": None, "line": 64, "seq_gb_s": 20, "lat_ns": 90}],
    "tlb": {"page": 4096, "l1_entries": 64, "l2_entries": 2048, "l1_miss_ns": 2, "walk_ns": 25},
    "dram": {"B_eff": 128, "mlp": 6},
    "overfetch": [],
    "cpu": {"c_scan": 1, "c_pred": 1.3, "c_pred_branchless": 2, "c_select_sum": 2,
            "c_select_sum_branchless": 1, "o_batch": 30, "c_gather_k1": 3, "c_gather_value": 1.5,
            "c_sum_value": 1, "c_nsm_row": 1.5, "c_nsm_value": 0.3, "c_nsm_pred": 1.5,
            "c_copy_tuple_k16": 10, "c_copy_tuple_k1": 4, "c_branch_miss": 28,
            "pax_scan_per_row": 1, "pax_scan_o_page": 27, "pax_pred_per_row": 1.4,
            "pax_pred_o_page": 40, "pax_mat_k16_per_row": 25},
}
DATA = DataInfo(1 << 21, 16, {"default": "uniform:lo=0:hi=99", "c0": "uniform:lo=0:hi=999"})
SPACE = load_space(REPO / "configs" / "spaces" / "default.yaml")


def test_where_parser():
    p = parse_where("c0 < 5 AND c3 >= 7 and c2 = 10..19, c4 BETWEEN 3 AND 8", 16)
    assert [(x.col, x.lo, x.hi) for x in p] == [(0, -math.inf, 4), (3, 7, math.inf), (2, 10, 19),
                                               (4, 3, 8)]
    for bad in ("c99 < 1", "c0 << 1", "x < 3"):
        try:
            parse_where(bad, 16)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass


def test_query_and_engine_syntax():
    q = query_from_dict({"op": "sum", "cols": [0, "c4"], "where": "c0 < 10"}, 16)
    assert q.cols == [0, 4] and q.preds[0].hi == 9
    q = parse_engine_query("op=materialize;cols=0-3;where=c0<10,c1<50", 16)
    assert q.cols == [0, 1, 2, 3] and len(q.preds) == 2
    q = parse_engine_query("op=lookup;cols=all;n=100;dist=sorted", 16)
    assert q.lookups == 100 and q.id_dist == "sorted" and len(q.cols) == 16


def test_data_profile_yaml():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("size: 64M\ncols: 8\ndefault: {dist: uniform, min: 0, max: 9}\n"
                "columns:\n  c1: {dist: clustered, run: 64}\n"
                "  c2: {dist: uniform, min: 0, max: 99, hist: {edges: [0, 10, 100], "
                "counts: [90, 10]}}\n")
    data, hists = load_data_profile(f.name)
    assert data.rows == (64 << 20) // 64 and data.dist["c1"] == "clustered:run=64"
    assert data.dist["default"] == "uniform:lo=0:hi=9" and 2 in hists


def test_estimators():
    q = query_from_dict({"op": "sum", "where": "c0 < 10 AND c1 < 50"}, 16)
    assert make_estimator("profile", DATA).rates(q) == [0.01, 0.5]
    hists = {1: {"edges": [0, 50, 100], "counts": [10, 90]}}
    assert abs(make_estimator("histogram", DATA, hists).rates(q)[1] - 0.1) < 1e-12
    q.rates = [0.2, 0.3]
    assert make_estimator("fixed", DATA).rates(q) == [0.2, 0.3]


def test_search_picks_options_and_ranks():
    model = make("v3", HW)
    w = Workload("t", [query_from_dict({"op": "sum", "cols": "all", "where": "c0 < 500"}, 16),
                       query_from_dict({"op": "lookup", "cols": "all", "lookups": 1000}, 16)])
    ctx = SearchContext(DATA, make_estimator("profile", DATA))
    ranked = make_search("exhaustive").run(SPACE, w, model, ctx)
    costs = [r.cost_ns for r in ranked.results]
    assert costs == sorted(costs) and len(ranked.results) == 12
    # every choice is the minimum over the options it reports
    for r in ranked.results:
        for c in r.choices:
            again = best_choice(model, r.storage, c.query, ctx, SPACE)
            assert math.isclose(again.cost.total_ns, c.cost.total_ns)
    # predicate order: selective-first is never worse for DSM on independent predicates
    q = query_from_dict({"op": "sum", "cols": [5], "where": "c1 < 50 AND c0 < 5"}, 16)
    dsm = next(r.storage for r in ranked.results if r.storage.layout == "dsm")
    assert best_choice(model, dsm, q, ctx, SPACE).exec["order"] == "selective_first"
    text = to_text(ranked, w, model.hw, DATA, "v3")
    assert "Recommended storage" in text and "Runner-up" in text
    js = to_json(ranked, w, model.hw, DATA, "v3")
    assert js["ranking"][0]["x_best"] == 1.0


@register_model("dummy_constant")
class DummyModel(CostModel):
    """Everything costs the same, except NSM which is free: the search must pick NSM."""

    def predict(self, storage, exec_opts, query, data):
        return CostBreakdown(0.0 if storage.layout == "nsm" else 1.0)


@register_search("dummy_first")
class DummySearch(Search):
    """Takes the first candidate only."""

    def run(self, space, workload, model, ctx):
        s = self.candidates(space, ctx)[0]
        return RankedResults([StorageResult(s, 0.0, [])], ctx.objective)


def test_registries_accept_new_components():
    w = Workload("t", [query_from_dict({"op": "sum", "where": "c0 < 5"}, 16)])
    ctx = SearchContext(DATA, make_estimator("profile", DATA))
    best = make_search("exhaustive").run(SPACE, w, make("dummy_constant", HW), ctx).best
    assert best.storage.layout == "nsm"
    assert len(make_search("dummy_first").run(SPACE, w, make("v1", HW), ctx).results) == 1


def test_hardware_model_penalties_telescope():
    hw = HardwareModel(HW)
    # sequential penalties per byte add up to 1 / DRAM bandwidth
    per_byte = sum(lv.seq_ns / lv.B for lv in hw.data_levels) + 1 / 100
    assert math.isclose(per_byte, 1 / 20)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
