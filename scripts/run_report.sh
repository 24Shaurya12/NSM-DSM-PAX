#!/usr/bin/env bash
# Full pipeline for the report: build + tests, characterisation sweeps + plots, full-size
# calibration, ground-truth analysis (existing store), full validation (CP5, V1-V5, V7, V4),
# example recommendations and decision maps. Every stage runs even if an earlier one fails;
# the status of each is logged. Keep the machine idle and on AC power.
#
#   scripts/run_report.sh            (log: results/report/run.log)
set -uo pipefail
cd "$(dirname "$0")/.."
R=${R:-results/report}
STORE=${STORE:-results/ground_truth/laptop_v1}
HW=${HW:-configs/hw/laptop_v2.json}
mkdir -p "$R"
STATUS="$R/status.txt"
: > "$STATUS"

stage() {   # stage <name> <command...>
    local name=$1; shift
    local t0=$SECONDS
    echo "=== [$(date +%T)] $name" >&2
    if "$@"; then
        echo "OK    $name  $(( (SECONDS - t0) / 60 ))m$(( (SECONDS - t0) % 60 ))s" >> "$STATUS"
    else
        echo "FAIL  $name  (exit $?)" >> "$STATUS"
    fi
}

# 0. build + correctness tests
stage build make -s
stage engine_tests make -s test
stage advisor_tests bash -c "python3 tests/advisor/test_patterns.py && python3 tests/advisor/test_advisor.py"

# 1. characterisation sweeps on the current engine, each followed by its plots
for s in working_set selectivity conjunction pagesize pax_wins; do
    stage "sweep_$s" env OUT="$R/$s" "scripts/run_$s.sh"
    stage "plot_$s" python3 "scripts/plot_$s.py" "$R/$s"
done

# 2. full-size calibration (1 GB regions, 5 samples)
stage calibrate python3 -m advisor calibrate --out "$HW" --raw "${HW%.json}.raw.json"

# 3. ground truth: reuse the existing store; regenerate its analysis tables
stage gt_analysis python3 scripts/analyze_ground_truth.py "$STORE"

# 4. full validation (1000 workloads, 10 perturbations) + recommendations + maps
stage validation env STORE="$STORE" HW="$HW" OUT="$R/validation" scripts/run_validation.sh

echo "=== [$(date +%T)] done" >&2
cat "$STATUS" >&2
