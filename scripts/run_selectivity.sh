#!/usr/bin/env bash
# Selectivity sweep: SELECT COUNT(*), SUM(c0 + ... + c{k-1}) FROM T WHERE c0 < S, for S% of rows
# qualifying, at a cache-resident and a DRAM-resident table size, for every layout.
#
# Each (size, layout) pair runs in its own process pinned to one P-core; all selectivities and
# k values run inside that process, so the table is built once per pair.
#
# Usage: scripts/run_selectivity.sh            (defaults below; override with env vars)
#        SIZES="256M" KS=4 scripts/run_selectivity.sh
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/common.sh

OUT=${OUT:-results/selectivity}
CPU=${CPU:-2}              # a P-core (cpu_core PMU) that is not CPU 0
COLS=${COLS:-16}
KS=${KS:-1,4,16}
PAGE_SIZE=${PAGE_SIZE:-16384}
ITERATIONS=${ITERATIONS:-10}
MODES=${MODES:-warm}
LAYOUTS=${LAYOUTS:-"nsm dsm pax"}
SIZES=${SIZES:-"1M 256M"}  # fits in L2 / far larger than the LLC
# Values are uniform in [0, 100), so c0 < S selects about S% of the rows.
PERCENTS=${PERCENTS:-"0 1 2 5 10 20 30 40 50 60 70 80 90 95 98 99 100"}

make -s
mkdir -p "$OUT"
RAW="$OUT/raw.csv"
rm -f "$RAW"

write_machine_info "cols=$COLS ks=$KS page_size=$PAGE_SIZE iterations=$ITERATIONS modes=$MODES" \
                   "sizes=$SIZES percents=$PERCENTS"

# One --where per selectivity. The unfiltered scan has no --where, so it runs separately below.
WHERE=()
for p in $PERCENTS; do WHERE+=(--where "c0<$p"); done

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

for size in $SIZES; do
    for layout in $LAYOUTS; do
        echo "== size=$size layout=$layout" >&2
        common=(--size "$size" --cols "$COLS" --k "$KS" --layout "$layout"
                --page-size "$PAGE_SIZE" --modes "$MODES" --iterations "$ITERATIONS")
        taskset -c "$CPU" ./build/scan_bench "${common[@]}" "${WHERE[@]}" --output "$TMP" \
            2> >(grep -v "rows qualify" | sed 's/^/   /' >&2)
        append_csv "$TMP"
        # Baseline: the same query without a WHERE clause (plain full scan).
        taskset -c "$CPU" ./build/scan_bench "${common[@]}" --output "$TMP" 2>/dev/null
        append_csv "$TMP"
    done
done

echo "Wrote $RAW" >&2
