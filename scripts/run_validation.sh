#!/usr/bin/env bash
# Phase 3-6 evidence, once a ground-truth store exists (plan CP4-CP7):
#   calibrate -> hw.json; CP5 accuracy (validate); V1-V5 + V7 (evaluate, random split);
#   V4 generalisation (evaluate, fit on one data set); example recommendations; decision maps.
# Keep the machine idle and on AC power while calibrating.
#
#   STORE=results/ground_truth/laptop_v1 HW=configs/hw/laptop.json scripts/run_validation.sh
set -euo pipefail
cd "$(dirname "$0")/.."
STORE=${STORE:-results/ground_truth/laptop_v1}
HW=${HW:-configs/hw/laptop.json}
NAME=$(basename "$STORE")
OUT=${OUT:-results/validation/$NAME}
FIT_DATASET=${FIT_DATASET:-w16_uniform}
# FAST=1: calibration up to 512 MB with 3 samples, 500 workloads, 4 robustness perturbations
if [[ -n "${FAST:-}" ]]; then
    CAL_ARGS=(--max-size 512M --samples 3); EVAL_ARGS=(--workloads 500 --quick)
else
    CAL_ARGS=(); EVAL_ARGS=()
fi

make -s
if [[ ! -f "$HW" || -n "${RECALIBRATE:-}" ]]; then
    python3 -m advisor calibrate --out "$HW" "${CAL_ARGS[@]}"
fi
python3 tests/advisor/test_patterns.py
python3 tests/advisor/test_advisor.py

mkdir -p "$OUT"
python3 -m advisor validate --store "$STORE" --hw "$HW" --out "$OUT/cp5" | tee "$OUT.cp5.txt"
python3 -m advisor evaluate --store "$STORE" --hw "$HW" --out "$OUT/main" "${EVAL_ARGS[@]}"
python3 -m advisor evaluate --store "$STORE" --hw "$HW" --no-robustness "${EVAL_ARGS[@]}" \
    --fit-split "dataset:$FIT_DATASET:nolookup" --out "$OUT/v4_generalisation"

mkdir -p "$OUT/recommend"
for w in advisor/workloads/examples/*.yaml; do
    python3 -m advisor recommend --hw "$HW" --data configs/data/w64_8m.yaml --workload "$w" \
        --params "$OUT/main/params_v3.json" > "$OUT/recommend/$(basename "$w" .yaml).txt"
done
for grid in selectivity-projection lookup-selectivity; do
    for op in sum materialize; do
        python3 -m advisor maps --hw "$HW" --data configs/data/w64_8m.yaml --grid "$grid" \
            --op "$op" --params "$OUT/main/params_v3.json" --out "$OUT/maps"
    done
done
echo "reports: $OUT/main/report.md  $OUT/v4_generalisation/report.md  $OUT.cp5.txt"
