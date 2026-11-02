#!/usr/bin/env bash
# PAX page-size (row-group size) sweep over the existing queries, on a DRAM-resident table.
#
# PAX runs once per page size; NSM and DSM do not have pages, so they run once each and serve as
# reference lines. Every process runs the same queries:
#   ops        sum (SELECT COUNT(*), SUM(c0+...+c{k-1})) and materialize (SELECT c0,...,c{k-1})
#   predicates none, c0<1, c0<10, c0<50, c0<1 AND c12<50
#   k          1, 4, 16
# With 16 int64 columns, a page of P bytes holds P/128 tuples and each minipage is P/16 bytes.
#
# Usage: scripts/run_pagesize.sh            (defaults below; override with env vars)
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/common.sh

OUT=${OUT:-results/pagesize}
CPU=${CPU:-2}
COLS=${COLS:-16}
SIZE=${SIZE:-1G}
KS=${KS:-1,4,16}
OPS=${OPS:-sum,materialize}
ITERATIONS=${ITERATIONS:-10}
PAGE_SIZES=${PAGE_SIZES:-"1024 2048 4096 8192 16384 32768 65536 262144 1048576 4194304 16777216"}
PREDICATES=${PREDICATES:-"none c0<1 c0<10 c0<50 c0<1,c12<50"}

make -s
mkdir -p "$OUT"
RAW="$OUT/raw.csv"
rm -f "$RAW"
write_machine_info "cols=$COLS size=$SIZE ks=$KS ops=$OPS iterations=$ITERATIONS modes=warm" \
                   "page_sizes=$PAGE_SIZES" "predicates=$PREDICATES"

WHERE=()
for p in $PREDICATES; do WHERE+=(--where "$p"); done

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

run() {
    taskset -c "$CPU" ./build/scan_bench --size "$SIZE" --cols "$COLS" --k "$KS" --op "$OPS" \
        --modes warm --iterations "$ITERATIONS" "${WHERE[@]}" "$@" --output "$TMP" \
        2> >(grep -v "rows qualify" | sed 's/^/   /' >&2)
    append_csv "$TMP"
}

for layout in nsm dsm; do
    echo "== layout=$layout" >&2
    run --layout "$layout"
done
for ps in $PAGE_SIZES; do
    echo "== layout=pax page_size=$ps" >&2
    run --layout pax --page-size "$ps"
done

echo "Wrote $RAW" >&2
