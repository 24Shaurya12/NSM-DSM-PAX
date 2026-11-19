# NSM / DSM / PAX scan benchmark

A small in-memory benchmark that compares three physical layouts of the same
`int64_t` table on two kinds of query:

```sql
SELECT COUNT(*), SUM(c0 + ... + c{k-1}) FROM T [WHERE p1 AND p2 AND ...]   -- --op sum
SELECT c0, ..., c{k-1}                  FROM T [WHERE p1 AND p2 AND ...]   -- --op materialize
```

where each `p` is an equality or range predicate on any column. `materialize`
reconstructs the qualifying tuples (row-major) and hands them to a consumer.

| Layout | Storage |
|--------|---------|
| NSM | One row-major array of `R*N` values; `(r, c)` is at `r*N + c`. |
| DSM | `N` separate arrays of `R` values, one per column. |
| PAX | Fixed-size pages, each holding `floor(page_size / (N*8))` tuples. Inside a page, every column has its own contiguous minipage. The tuple count is stored per page, since the last page may be partial. |

All buffers are 64-byte aligned. Data is uniform in `[0, 100)` from
`std::mt19937_64` with a fixed seed (42), so every run produces the same table.

## Layout of the code

```
src/aligned_buffer.h       64-byte aligned owning array
src/table.{h,cpp}          logical table, generated column by column from distributions
src/distributions.{h,cpp}  pluggable value generators (uniform, sorted, clustered; --dist)
src/query_spec.{h,cpp}     QuerySpec (op x projection x predicates), --query parser
src/projection.h           column lists (any subset/order), with a fast path for ranges
src/exec_options.h         per-query execution options (DSM batch, filter mode, threads)
src/layout.h               common Layout interface (scan_sum, filter_sum, filter_materialize, lookup)
src/row_ids.{h,cpp}        pluggable row-id generators for point lookups (uniform, sorted)
src/layout_factory.{h,cpp} layout registry: "nsm", "dsm", "pax:page=256K", ...
src/predicate.{h,cpp}      predicates, selection-vector evaluation (branchy / branch-free)
src/tuple_sink.h           consumer for reconstructed tuples (--op materialize)
src/reference.{h,cpp}      reference executor on the logical table (correctness checks)
src/nsm.{h,cpp}            NSM layout
src/dsm.{h,cpp}            DSM layout
src/pax.{h,cpp}            PAX layout
src/perf_counters.{h,cpp}  hardware counters via perf_event_open (core + memory-controller)
src/util.{h,cpp}           small parsing helpers
src/main.cpp               argument parsing, correctness check, timing, CSV output
tests/engine_tests.cpp     every layout x op x option vs the reference; parser tests
scripts/common.sh            shared helpers for the sweep scripts (machine info, CSV append)
scripts/run_working_set.sh   working-set-size sweep (one process per size x layout)
scripts/plot_working_set.py  figures + summary tables for that sweep
scripts/run_selectivity.sh   single-predicate selectivity sweep
scripts/plot_selectivity.py  figures + summary tables for that sweep
scripts/run_conjunction.sh   two-predicate (selective + unselective) sweep, both orders
scripts/plot_conjunction.py  figures + summary tables for that sweep
scripts/run_pagesize.sh      PAX page-size sweep over the scan, filter and reconstruction queries
scripts/plot_pagesize.py     figures + summary tables for that sweep
scripts/run_pax_wins.sh      wide-table SELECT * at low selectivity (where PAX wins)
scripts/plot_pax_wins.py     figures + summary tables for that sweep
scripts/run_ground_truth.py  ground truth: every storage config x query template x exec option
scripts/analyze_ground_truth.py  report.md for a ground-truth store
advisor/                     Python side of the layout advisor (engine adapter, templates,
                             distribution models, candidate space, measurement store)
configs/                     ground-truth run, candidate space and template-set files (YAML)
```

Extending the engine (see `further_plan.md` §9 for the full guide):

- **New layout:** subclass `Layout`, implement its operators, and register it in
  its own `.cpp` with `register_layout("name", builder)`. It is then usable as
  `--layout name[:key=value...]`.
- **New operator:** add an `Op`, a reference implementation in `reference.cpp`,
  a virtual in `Layout` implemented by every layout, and a case in `run_query`.
- **New distribution:** implement `ColumnGenerator` and call
  `register_distribution("name", factory)`; use it as `--dist cN=name:key=value`.
- **New execution option:** add a field to `ExecOptions`, a list flag in
  `main.cpp`, and say in `variants()` when it matters.

## Build

```
make            # builds build/scan_bench with -std=c++17 -O2 -march=native
make test       # builds and runs build/engine_tests (~53,000 checks, a few seconds)
make clean
```

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--rows R` | 1000000 | number of rows |
| `--dist T=SPEC` | `default=uniform:lo=0:hi=99` | value distribution; `T` is `default` or a column `cN`; repeatable. `SPEC`: `uniform[:lo=..:hi=..]`, `sorted[:lo=..:hi=..]`, `clustered[:run=1024:lo=..:hi=..]` |
| `--size BYTES` | - | table size instead of `--rows` (suffix `K`/`M`/`G`); rows = BYTES / (N*8) |
| `--cols N` | 16 | number of `int64_t` columns |
| `--page-size B` | 16384 | PAX page size in bytes (must be a multiple of 64 and hold at least one tuple) |
| `--k K1,K2,...` | `1,2,4,...,N` | values of `k` to benchmark (`N` is always included in the default) |
| `--where PREDS` | none | filter rows (see below); repeat the flag to run several queries in one process. `--where none` is the unfiltered query, so one run can mix both |
| `--op O,...` | `sum` | `sum` and/or `materialize` (tuple reconstruction of columns `0..k-1`) |
| `--layout L,...` | `nsm,dsm,pax` | layouts to build, with parameters: `nsm`, `dsm`, `pax[:page=BYTES]` (e.g. `pax:page=4K,pax:page=256K`) |
| `--query SPEC` | - | one query, repeatable: `op=sum\|materialize;cols=all\|0-3\|0,4,8;where=none\|c0<5,c12<50[;label=NAME]`, or a point-lookup batch `op=lookup;cols=...;n=COUNT[;dist=uniform\|sorted][;seed=S]`. When given, the legacy `--op`/`--where`/`--k` cross product is only added if those flags are also given |
| `--dsm-batch B,...` | `1024` | DSM rows per filter batch; swept only for DSM queries with a filter or reconstruction |
| `--filter F,...` | `branchy` | `branchy` and/or `branchless` predicate evaluation; swept only for queries with a WHERE clause |
| `--threads N` | 1 | reserved for multithreading; only 1 is accepted for now |
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

Without `--where` (or with `--where none`), `sum` runs the plain full scan
(`sum_first_k`).

## Tuple reconstruction (`--op materialize`)

Each qualifying row is written as a row-major tuple of its first `k`
attributes into a 1024-tuple output buffer (`TupleSink`). When the buffer is
full it is consumed (folded into a checksum that weights attribute `c` by
`c + 1`, so a value in the wrong attribute fails the correctness check) and
reused, so the output stays in cache and costs every layout the same.

| Layout | How a qualifying tuple is gathered |
|--------|------------------------------------|
| NSM | one `memcpy` of the row's first `k` values |
| DSM | after the selection vector, one value from each of the `k` column arrays |
| PAX | after the per-page selection vector, one value from each of the page's `k` minipages |

## Point lookups (`op=lookup`)

`--query "op=lookup;cols=all;n=100000;dist=uniform"` fetches the projected
columns of `n` rows. The row ids are generated before timing (seeded; `uniform`
draws with replacement in random order, `sorted` is the same ids ascending) and
every layout sees the same ids. The lookups are independent of each other (as a
batch of index probes), so the CPU can overlap their cache misses. Each looked-up
tuple goes through the same `TupleSink` as reconstruction:

| Layout | One lookup |
|--------|------------|
| NSM | one `memcpy` of the projected fields of the row |
| DSM | one value from each projected column array (k independent random accesses) |
| PAX | page = id / tuples_per_page, slot = id % tuples_per_page; one value per projected minipage |

New id distributions register with `register_row_id_dist` (`src/row_ids.cpp`).
In the CSV `n_items` is the number of lookups and `selectivity` is matches / n_items.

## Ground truth for the layout advisor

`scripts/run_ground_truth.py` measures every storage configuration of a candidate
space on every query template and execution option (plan §3):

```
python3 scripts/run_ground_truth.py --dry-run          # list the engine runs
python3 scripts/run_ground_truth.py                    # full run (~2 h on the laptop)
python3 scripts/run_ground_truth.py --sample 0.1 --name laptop_v1_repeat
python3 -m advisor.ground_truth compare results/ground_truth/laptop_v1 results/ground_truth/laptop_v1_repeat
python3 scripts/analyze_ground_truth.py results/ground_truth/laptop_v1
```

- The run configuration `configs/ground_truth/default.yaml` lists data sets
  (width, size, per-column distributions, optional family subset), the template
  file and the space file, and the measurement settings (cold by default: the
  table is flushed from every cache before each timed execution).
- `configs/templates/default.yaml` defines projections relative to the table
  width and query families (`scan`, `filter`, `lookup`; new family types register
  in `advisor/workloads/templates.py`). Predicate constants come from target pass
  rates through the column's distribution model (`advisor/data_profile.py`).
- `configs/spaces/default.yaml` lists storage configurations (layout x parameter
  grid) and execution options (DSM batch, filter mode, predicate order).
  Predicate order is an execution option: a two-predicate query is measured in
  both orders under one `logical_id`.
- One engine process per (data set, storage config), pinned to the core
  `pick_cpu` selects. The run is resumable (finished pairs are skipped).
- The store `results/ground_truth/<name>/` holds `measurements.csv` (every
  sample), `summary.csv` (median per data set x storage x template x options),
  `templates.csv`, `runs.csv` (machine state per engine run: AC power, governor,
  EPP, frequency, temperature, load, counters), `config.yaml` and `machine.txt`.

## Layout advisor (`python3 -m advisor`)

Given a machine (`hw.json`), a data profile (`data.yaml`) and a workload
(`workload.yaml`), the advisor recommends the storage layout and its parameters
plus per-query execution options, with a predicted time (plan §4–§6):

```
python3 -m advisor calibrate --out configs/hw/laptop.json            # ~2-3 min, machine idle
python3 -m advisor validate  --store results/ground_truth/laptop_v1 --hw configs/hw/laptop.json
python3 -m advisor recommend --hw configs/hw/laptop.json --data configs/data/w64_8m.yaml \
        --workload advisor/workloads/examples/wide_mixed.yaml \
        [--params results/validation/laptop_v1/params_v3.json] [--json]
python3 -m advisor predict   --hw ... --data ... --query "op=sum;cols=0-3;where=c0<5"
python3 -m advisor maps      --hw ... --data ... [--grid lookup-selectivity] [--op materialize]
python3 tests/advisor/test_patterns.py && python3 tests/advisor/test_advisor.py
```

- Cost models (`advisor/models/`): Manegold et al.'s access patterns per cache/TLB
  level (`patterns/`), described once per layout and operator in
  `patterns/describe.py`; ladder v0 (bytes ÷ bandwidth), v1 (Manegold as
  published), v2 (+ B_eff, prefetch over-fetch, MLP), v3 (+ branch misses,
  batch/page overheads, memory/CPU overlap; fitted on a calibration split).
- Pluggable: models (`@register_model`), search strategies (`@register_search`,
  default `exhaustive`), selectivity estimators (`@register_estimator`:
  `profile`, `histogram`, `fixed`), distribution models, template families.
- Workload syntax: see `advisor/workload.py`; examples in
  `advisor/workloads/examples/`, data profiles in `configs/data/`.

## What a run does

1. Generates the table and builds the requested layouts, then frees the logical
   table. This step is not timed.
2. Prints the table size in MB, and each layout's actual footprint, to stderr.
3. Checks every layout's result for every query and execution-option variant
   against the reference executor (`reference.cpp`). On a mismatch it prints it
   and exits with status 1. It also prints how many rows each query selects.
4. Opens the hardware counters and lists any the kernel refused.
5. For each layout, query, execution-option variant and mode: runs the warm-up queries, then the timed
   samples using `std::chrono::steady_clock`.
   - **warm:** each sample repeats the scan `reps` times (calibrated so a sample
     lasts at least `--min-sample-ms`), so data that fits in cache stays there.
   - **cold:** each sample is a single scan, right after `clflush`ing the
     whole table.
   Each result is added into a `volatile` sink so the compiler cannot remove the scan.

It writes one CSV row per timed sample. Latency and counters are **per scan**
(the sample total divided by `reps`):

```
layout,layout_params,rows,cols,row_bytes,data_bytes,footprint_bytes,dist,query,op,project,k,
predicate,n_items,page_size,dsm_batch,filter,threads,mode,iteration,reps,latency_ns,matches,
selectivity,result,
cycles,instructions,branch_misses,llc_refs,llc_misses,l1d_misses,dtlb_misses,dram_read_bytes,dram_write_bytes
```

`query` is a comma-free id such as `sum|c0..c3|c0<5 & c12<50`, `project` the
projected columns (`c0..c3`, `c0|c4|c8`) and `k` their number. `dsm_batch` is `0`
and `filter` is `none` where the option has no effect.

`predicate` is the `--where` text with `,` shown as ` & ` (`none` without a
predicate), `matches` is `COUNT(*)`, `selectivity` is `matches / rows`, and
`result` is the `SUM`. `page_size` is `0` for NSM and DSM because they have no
pages. Counter fields are empty when the counter is unavailable.

The sweep scripts pin to a core chosen by `pick_cpu` in `scripts/common.sh` (a
performance core on hybrid Intel CPUs, otherwise the last online CPU); set
`CPU=<id>` to override. `machine.txt` records the core, the virtualization
type and which counters were available.

## Hardware counters

Core counters (`cycles`, `instructions`, `branch_misses`, `llc_refs`,
`llc_misses`, `l1d_misses`, `dtlb_misses`) count only this process in user space. On hybrid Intel CPUs they
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

## PAX page-size experiment

```
scripts/run_pagesize.sh                    # 1G table, 16 cols, pages 1K..16M
python3 scripts/plot_pagesize.py           # figures + tables in results/pagesize/
```

Runs the scan, single-filter (`c0<1`, `c0<10`, `c0<50`) and two-predicate
(`c0<1 AND c12<50`) queries, each as `sum` and `materialize`, for k in
{1,4,16}. PAX runs once per page size (1 KB to 16 MB, so 8 to 131072 tuples per
page and 64 B to 1 MB minipages); NSM and DSM run once as reference lines.
Outputs `pagesize_sum.png`, `pagesize_materialize.png`, `pagesize_counters.png`
and `best_page_size.md`.

## PAX-wins experiment

```
scripts/run_pax_wins.sh                    # 1G table, 64 cols
python3 scripts/plot_pax_wins.py           # figures + tables in results/pax_wins/
```

`SELECT * FROM T WHERE c0 < S` on a 64-column table for selectivities from
0.5% to 10%, with PAX pages from 4 KB to 1 MB. The predicate needs only `c0`
(bad for NSM, which touches every row), and each qualifying tuple needs all 64
attributes (bad for DSM, which fetches them from 64 separate arrays; PAX finds
them in one page). Outputs `pax_wins_latency.png`, `pax_wins_counters.png` and
`winners.md`.

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
