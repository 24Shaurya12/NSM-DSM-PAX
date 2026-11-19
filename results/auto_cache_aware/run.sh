#!/bin/bash
# In-cache auto comparison: threshold (DRAM constants), threshold with --auto-cache
# (in-cache constants phi=1.0, sigma=2), and --auto-measure. 32K rows x 16 cols = 4 MB (in L3).
cd "$(dirname "$0")/../.."
SELS=0.0001,0.001,0.005,0.01,0.02,0.05,0.1,0.2,0.5,1
for qk in "sum 4" "projection 4" "projection 16"; do
  set -- $qk
  for mode in threshold cache measure; do
    case $mode in threshold) f="";; cache) f="--auto-cache";; measure) f="--auto-measure";; esac
    out=results/auto_cache_aware/${1}_k${2}_${mode}.csv
    cmd="taskset -c 2 ./build/scan_bench --rows 32768 --cols 16 --domain 1000000 --iters 5 --rounds 3 --warmup 1 --dsm-offset auto --query $1 --k $2 --sel $SELS --strategy early,late,auto --min-time-ms 10 $f --output $out"
    echo "$cmd" > ${out%.csv}.log
    $cmd 2>> ${out%.csv}.log
  done
done
echo done
