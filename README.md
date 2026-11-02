# NSM / DSM / PAX scan benchmark

A small in-memory benchmark that compares three physical layouts of the same
`int64_t` table on the query

```sql
SELECT COUNT(*), SUM(c0 + ... + c{k-1}) FROM T [WHERE p1 AND p2 AND ...]
```

where each `p` is an equality or range predicate on any column.

| Layout | Storage |
|--------|---------|
| NSM | One row-major array of `R*N` values; `(r, c)` is at `r*N + c`. |
| DSM | `N` separate arrays of `R` values, one per column. |
| PAX | Fixed-size pages, each holding `floor(page_size / (N*8))` tuples. Inside a page, every column has its own contiguous minipage. The tuple count is stored per page, since the last page may be partial. |

All buffers are 64-byte aligned. Data is uniform in `[0, 100)` from
`std::mt19937_64` with a fixed seed (42), so every run produces the same table.

## Layout of the code

```
src/aligned_buffer.h   64-byte aligned owning array
src/table.{h,cpp}      logical table + reproducible data generation
src/layout.h           common Layout interface (sum_first_k, filter_sum, ...)
src/predicate.{h,cpp}  predicates, --where parsing, selection-vector evaluation
src/nsm.{h,cpp}        NSM layout
src/dsm.{h,cpp}        DSM layout
src/pax.{h,cpp}        PAX layout
src/perf_counters.{h,cpp}  hardware counters via perf_event_open (core + memory-controller)
src/main.cpp           argument parsing, correctness check, timing, CSV output
scripts/common.sh            shared helpers for the sweep scripts (machine info, CSV append)
scripts/run_working_set.sh   working-set-size sweep (one process per size x layout)
scripts/plot_working_set.py  figures + summary tables for that sweep
scripts/run_selectivity.sh   single-predicate selectivity sweep
scripts/plot_selectivity.py  figures + summary tables for that sweep
scripts/run_conjunction.sh   two-predicate (selective + unselective) sweep, both orders
scripts/plot_conjunction.py  figures + summary tables for that sweep
```

To add a new operator (such as a predicated scan or tuple reconstruction), add
a virtual method to `Layout` and implement it in each layout.

## Build

```
make            # builds build/scan_bench with -std=c++17 -O2 -march=native
make clean
```

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--rows R` | 1000000 | number of rows |
| `--size BYTES` | - | table size instead of `--rows` (suffix `K`/`M`/`G`); rows = BYTES / (N*8) |
| `--cols N` | 16 | number of `int64_t` columns |
| `--page-size B` | 16384 | PAX page size in bytes (must be a multiple of 64 and hold at least one tuple) |
| `--k K1,K2,...` | `1,2,4,...,N` | values of `k` to benchmark (`N` is always included in the default) |
| `--where PREDS` | none | filter rows (see below); repeat the flag to run several queries in one process |
| `--layout L,...` | `nsm,dsm,pax` | which layouts to build and run |
| `--modes M,...` | `warm` | `warm` and/or `cold` (cold flushes the table from every cache level with `clflush` before each timed scan) |
| `--warmup W` | 2 | untimed scans before measuring each `k` |
| `--iterations I` | 10 | timed samples per (layout, mode, k) |
| `--min-sample-ms T` | 20 | warm mode repeats the scan until a sample lasts at least T ms |
| `--output FILE` | stdout | where to write the CSV |

## Predicates

`--where` takes a comma-separated conjunction (all terms must hold):

| Term | Meaning |
|------|---------|
| `c3=42` | equality |
| `c0<10`, `c0<=10`, `c0>90`, `c0>=90` | one-sided range |
| `c2=10..19` | inclusive range (`BETWEEN 10 AND 19`) |

Values are uniform in `[0, 100)`, so `c0<S` selects about S% of rows, and two
predicates on different columns multiply: `c0<50,c5<50` selects about 25%.
Quote the argument in the shell: `--where 'c0<10,c3=42'`.

Internally every term is the closed range `lo <= v <= hi`. How each layout runs
a filtered query:

| Layout | Strategy |
|--------|----------|
| NSM | Tuple at a time. Tests the predicates on the tuple's own fields, stopping at the first that fails, and adds the first `k` fields of each qualifying tuple. |
| DSM | Batches of 1024 rows. The first predicate scans its column and writes the qualifying positions to a selection vector; each further predicate re-checks only those positions; then columns `0..k-1` are read only at the selected positions. |
| PAX | The same selection-vector algorithm, one page at a time, over the page's minipages. |

Without `--where`, every layout runs the plain full scan (`sum_first_k`).

## What a run does

1. Generates the table and builds the requested layouts, then frees the logical
   table. This step is not timed.
2. Prints the table size in MB, and each layout's actual footprint, to stderr.
3. Checks every layout's `COUNT` and `SUM` for every query and `k` against
   results computed from the logical table. On a mismatch it prints it and
   exits with status 1. It also prints how many rows each query selects.
4. Opens the hardware counters and lists any the kernel refused.
5. For each layout, query, mode and `k`: runs the warm-up scans, then the timed
   samples using `std::chrono::steady_clock`.
   - **warm:** each sample repeats the scan `reps` times (calibrated so a sample
     lasts at least `--min-sample-ms`), so data that fits in cache stays there.
   - **cold:** each sample is a single scan, right after `clflush`ing the
     whole table.
   Each result is added into a `volatile` sink so the compiler cannot remove the scan.

It writes one CSV row per timed sample. Latency and counters are **per scan**
(the sample total divided by `reps`):

```
layout,rows,cols,row_bytes,data_bytes,footprint_bytes,k,predicate,page_size,mode,iteration,reps,
latency_ns,matches,selectivity,result,
cycles,instructions,branch_misses,llc_refs,llc_misses,l1d_misses,dram_read_bytes,dram_write_bytes
```

`predicate` is the `--where` text with `,` shown as ` & ` (`none` without a
predicate), `matches` is `COUNT(*)`, `selectivity` is `matches / rows`, and
`result` is the `SUM`. `page_size` is `0` for NSM and DSM because they have no
pages. Counter fields are empty when the counter is unavailable.

## Hardware counters

Core counters (`cycles`, `instructions`, `branch_misses`, `llc_refs`,
`llc_misses`, `l1d_misses`) count only this process in user space. On hybrid Intel CPUs they
are opened on the P-core PMU, so **pin the benchmark to a P-core**
(`taskset -c 2 ...`). `dram_read_bytes`/`dram_write_bytes` come from the
memory controller's free-running uncore counters and are **system-wide**.

Ubuntu blocks perf events by default (`kernel.perf_event_paranoid=4`). Allow them with:

```
sudo sysctl -w kernel.perf_event_paranoid=0   # 2 is enough for the core counters only
```

## Working-set-size experiment

```
scripts/run_working_set.sh                 # sweep 16K..1G, all layouts, warm+cold
python3 scripts/plot_working_set.py        # figures + tables in results/working_set/
```

The sweep sums k in {1,2,4,8,16} of 16 columns, for table sizes from 16 KB to
1 GB in powers of two. Each (size, layout) pair runs as its own process pinned
to CPU 2, so only one layout is in memory at a time. Settings can be
overridden with environment variables (`SIZES`, `KS`, `COLS`, `CPU`, `MODES`,
`ITERATIONS`, `OUT`, ...). `machine.txt` records the CPU, cache sizes,
governor and power state next to the data.

Outputs in `results/working_set/`:

| File | Content |
|------|---------|
| `raw.csv` | every timed sample |
| `summary.csv` | median and quartiles per (layout, size, k, mode) |
| `winners.md` | fastest layout per (size, k, mode): the crossover table |
| `plots/ws_throughput.png` | useful GB/s vs size, per k, warm and cold |
| `plots/ws_latency_per_row.png` | ns per row vs size |
| `plots/ws_relative_to_dsm.png` | NSM and PAX latency divided by DSM latency |
| `plots/ws_cold_penalty.png` | cold / warm latency per layout |
| `plots/ws_llc_misses.png` | LLC misses per row (needs counters) |
| `plots/ws_dram_bytes.png` | DRAM bytes read per row vs useful bytes (needs counters) |
| `plots/projectivity_by_size.png` | ns per row vs k for tables in L2, in L3, in DRAM |

For stable numbers, run on AC power with the CPU in performance mode
(`energy_performance_preference=performance`) and an otherwise idle machine.

## Selectivity experiment

```
scripts/run_selectivity.sh                 # 1M and 256M tables, k in {1,4,16}
python3 scripts/plot_selectivity.py        # figures + tables in results/selectivity/
```

Runs `SELECT COUNT(*), SUM(c0 + ... + c{k-1}) FROM T WHERE c0 < S` for
S in {0,1,2,5,10,20,...,90,95,98,99,100}, plus the same query without a WHERE
clause as a baseline. Figures: `sel_latency.png`, `sel_relative_to_dsm.png`,
`sel_branch_misses.png`, `sel_llc_misses.png`, and `sel_dram_bytes.png` (only
for tables larger than the LLC, where DRAM traffic is meaningful).

## Two-predicate experiment

```
scripts/run_conjunction.sh                 # 1G table, k in {1,4,16}
python3 scripts/plot_conjunction.py        # figures + tables in results/conjunction/
```

Runs a very selective predicate together with a 50% predicate on another column,
in both orders:

```sql
WHERE c0 < S1 AND c12 < 50      -- selective first: ~(1 + S1) comparisons per row
WHERE c12 < 50 AND c0 < S1      -- selective last: 1.5 comparisons per row
```

for S1 in {0,1,2,5,10,20,50,100}. The 1 GB default keeps even one DSM column
(64 MB) out of the LLC. Figures: `conj_latency.png`, `conj_relative_to_dsm.png`,
`conj_order_penalty.png` (selective-last ÷ selective-first) and
`conj_counters.png` (comparisons, branch misses and DRAM bytes per row).

## Example

```
$ ./build/scan_bench --rows 1000000 --cols 16 --output results.csv
Table: 1000000 rows x 16 cols, 122.07 MB of data
  NSM footprint: 122.07 MB
  DSM footprint: 122.07 MB
  PAX footprint: 122.078 MB
Correctness check passed for k in {1,2,4,8,16}

$ head -3 results.csv
layout,rows,cols,k,page_size,iteration,latency_ns,result
NSM,1000000,16,1,0,0,4064834,49484225
NSM,1000000,16,1,0,1,3789312,49484225
```

Note: while the layouts are being built, the logical table and every
requested layout are in memory at once (about 4× the table size for all
three; 2× with `--layout` set to one layout).
