"""Model geometry checks against closed forms of Manegold et al. and our measured facts.
Run: python3 tests/advisor/test_patterns.py   (or with pytest, if installed)"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from advisor.models.patterns.regions import Component, column_runs, line_stats, lines_per_access
from advisor.workloads.templates import resolve_projection


def per_row(period, comps, B, rows_per_block=1):
    return line_stats(period, comps, B).touched / rows_per_block


def test_nsm_reads_one_line_per_row_for_any_single_column():
    for c in (0, 7, 15):
        comps = (Component(column_runs([c]), 1.0, 1.0, True),)
        assert per_row(128, comps, 64) == 1.0


def test_tlb_as_cache_pax_dtlb_misses_match_measurement():
    # SUM(c0) on PAX, 16 columns: measured dTLB walks per row 0.0312 / 0.0312 / 0.0156 /
    # 0.0078 / 0.0020 for 1K / 4K / 8K / 16K / 64K pages (PROJECT_STATE.md section 7)
    expected = {1024: 1 / 32, 4096: 1 / 32, 8192: 1 / 64, 16384: 1 / 128, 65536: 1 / 512}
    for page, want in expected.items():
        T = page // 128
        comps = (Component(column_runs([0], T * 8, T * 8)),)
        assert math.isclose(per_row(page, comps, 4096, T), want, rel_tol=1e-9)


def test_dsm_line_touch_probability():
    for f in (0.001, 0.01, 0.1, 0.5):
        comps = (Component(((0, 8),), f, 1.0 / f),)
        assert math.isclose(line_stats(8, comps, 64).touched * 8, 1 - (1 - f) ** 8, rel_tol=1e-9)


def test_clustered_selection_touches_fewer_lines():
    f = 0.1
    indep = line_stats(8, (Component(((0, 8),), f, 1 / f),), 64).touched
    runs = line_stats(8, (Component(((0, 8),), f, 1000 * (1 - f) / f),), 64).touched
    assert runs < indep / 4
    assert runs >= f / 8  # at least the qualifying values' own lines


def test_wrap_runs():
    assert line_stats(8, (Component(((0, 8),), 1.0),), 64).dense_runs == ((math.inf, 0),)
    s = line_stats(4096, (Component(column_runs([0, 4], 64, 64)),), 64)
    assert s.dense_runs == ((64, 192), (64, 3776))


def test_lookup_lines():
    assert lines_per_access(tuple(range(0, 128, 8)), 64, 128) == 2.0  # full 128 B row
    assert lines_per_access((0,), 64, 8) == 1.0


def test_projections():
    assert resolve_projection({"prefix_frac": 0.25}, 16) == ("0-3", 4)
    assert resolve_projection({"stride": 4}, 16) == ("0,4,8,12", 4)
    assert resolve_projection({"prefix": 1}, 64) == ("0", 1)
    assert resolve_projection({"all": True}, 64) == ("all", 64)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
