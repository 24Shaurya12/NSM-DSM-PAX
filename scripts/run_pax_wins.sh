#!/usr/bin/env bash
# The query where PAX wins: reconstruct complete tuples of a wide table at low selectivity.
#
#   SELECT * FROM T WHERE c0 < S        (T: 64 int64 columns = 512 B per row, 1 GB)
#
# Why PAX: the predicate only needs c0, so NSM, which must touch every row, does too much work.
# Rebuilding a qualifying tuple needs all 64 attributes: DSM fetches them from 64 separate
# arrays (64 distant cache lines and pages), while PAX finds them inside one page. Small pages
# keep that page compact (4 KB = 8 tuples, so each minipage is exactly one cache line).
#
# Usage: scripts/run_pax_wins.sh            (defaults below; override with env vars)
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/common.sh

OUT=${OUT:-results/pax_wins}
CPU=${CPU:-2}
COLS=${COLS:-64}
SIZE=${SIZE:-1G}
ITERATIONS=${ITERATIONS:-10}
PAGE_SIZES=${PAGE_SIZES:-"4096 8192 16384 65536 262144 1048576"}
# c0<1,c1<50 selects ~0.5%; the rest select ~S%.
PREDICATES=${PREDICATES:-"c0<1,c1<50 c0<1 c0<2 c0<3 c0<5 c0<10"}

make -s
mkdir -p "$OUT"
RAW="$OUT/raw.csv"
rm -f "$RAW"
write_machine_info "cols=$COLS size=$SIZE k=$COLS op=materialize iterations=$ITERATIONS" \
                   "page_sizes=$PAGE_SIZES" "predicates=$PREDICATES"

WHERE=()
for p in $PREDICATES; do WHERE+=(--where "$p"); done

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

run() {
    taskset -c "$CPU" ./build/scan_bench --size "$SIZE" --cols "$COLS" --k "$COLS" \
        --op materialize --modes warm --iterations "$ITERATIONS" "${WHERE[@]}" "$@" \
        --output "$TMP" 2> >(grep -v "rows qualify" | sed 's/^/   /' >&2)
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
