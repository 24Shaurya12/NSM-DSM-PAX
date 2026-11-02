#!/usr/bin/env bash
# Working-set-size sweep: SUM over k of N columns for table sizes from far below L1 to far above
# the LLC, for every layout, with warm and cold caches.
#
# Each (size, layout) pair runs in its own process, pinned to one P-core, so only one layout is
# in memory at a time and layouts cannot disturb each other's cache state.
#
# Usage: scripts/run_working_set.sh            (defaults below; override with env vars)
#        SIZES="1M 64M" KS=1,16 scripts/run_working_set.sh
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/common.sh

OUT=${OUT:-results/working_set}
CPU=${CPU:-2}              # a P-core (cpu_core PMU) that is not CPU 0
COLS=${COLS:-16}
KS=${KS:-1,2,4,8,16}
PAGE_SIZE=${PAGE_SIZE:-16384}
ITERATIONS=${ITERATIONS:-10}
MODES=${MODES:-warm,cold}
LAYOUTS=${LAYOUTS:-"nsm dsm pax"}
SIZES=${SIZES:-"16K 32K 64K 128K 256K 512K 1M 2M 4M 8M 16M 32M 64M 128M 256M 512M 1G"}

make -s
mkdir -p "$OUT"
RAW="$OUT/raw.csv"
rm -f "$RAW"

write_machine_info "cols=$COLS ks=$KS page_size=$PAGE_SIZE iterations=$ITERATIONS modes=$MODES"

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

for size in $SIZES; do
    for layout in $LAYOUTS; do
        echo "== size=$size layout=$layout" >&2
        taskset -c "$CPU" ./build/scan_bench --size "$size" --cols "$COLS" --k "$KS" \
            --layout "$layout" --page-size "$PAGE_SIZE" --modes "$MODES" \
            --iterations "$ITERATIONS" --output "$TMP" 2> >(sed 's/^/   /' >&2)
        append_csv "$TMP"
    done
done

echo "Wrote $RAW" >&2
