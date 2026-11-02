#!/usr/bin/env bash
# Two-predicate experiment: a very selective predicate plus a 50% predicate on another column.
#
#   SELECT COUNT(*), SUM(c0 + ... + c{k-1}) FROM T WHERE c0 < S1 AND c12 < 50   (selective first)
#   SELECT COUNT(*), SUM(c0 + ... + c{k-1}) FROM T WHERE c12 < 50 AND c0 < S1   (selective last)
#
# Predicates are evaluated in the order written. With the selective predicate first, DSM and PAX
# test c12 only on the ~S1% of rows that pass c0 < S1, so they make ~(1 + S1) comparisons per row
# and read c12 only at those positions. The reversed order is the contrast: every row is tested
# against the 50% predicate first. The default 1 GB table keeps even a single DSM column (64 MB)
# out of the LLC.
#
# Usage: scripts/run_conjunction.sh            (defaults below; override with env vars)
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/common.sh

OUT=${OUT:-results/conjunction}
CPU=${CPU:-2}
COLS=${COLS:-16}
KS=${KS:-1,4,16}
PAGE_SIZE=${PAGE_SIZE:-16384}
ITERATIONS=${ITERATIONS:-10}
LAYOUTS=${LAYOUTS:-"nsm dsm pax"}
SIZES=${SIZES:-"1G"}
SELECTIVE_PERCENTS=${SELECTIVE_PERCENTS:-"0 1 2 5 10 20 50 100"}
OTHER=${OTHER:-"c12<50"}   # the second, unselective predicate

make -s
mkdir -p "$OUT"
RAW="$OUT/raw.csv"
rm -f "$RAW"
write_machine_info "cols=$COLS ks=$KS page_size=$PAGE_SIZE iterations=$ITERATIONS modes=warm" \
                   "sizes=$SIZES selective_percents=$SELECTIVE_PERCENTS other=$OTHER"

WHERE=()
for s in $SELECTIVE_PERCENTS; do
    WHERE+=(--where "c0<$s,$OTHER" --where "$OTHER,c0<$s")
done

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

for size in $SIZES; do
    for layout in $LAYOUTS; do
        echo "== size=$size layout=$layout" >&2
        taskset -c "$CPU" ./build/scan_bench --size "$size" --cols "$COLS" --k "$KS" \
            --layout "$layout" --page-size "$PAGE_SIZE" --modes warm \
            --iterations "$ITERATIONS" "${WHERE[@]}" --output "$TMP" \
            2> >(grep -v "rows qualify" | sed 's/^/   /' >&2)
        append_csv "$TMP"
    done
done

echo "Wrote $RAW" >&2
