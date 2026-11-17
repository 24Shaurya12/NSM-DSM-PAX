# NSM / DSM / PAX scan engine and benchmark

An in-memory C++17 scan engine with a benchmark harness. It measures how the physical
layout of a table changes the latency and cache behaviour of analytical queries, and how
to tune query execution for the cache hierarchy of a specific machine.

The table holds `R` rows of `N` `int64_t` columns. There are three uncompressed layouts,
and DSM can also store every column in one of six encodings:

| Layout | Storage |
|--------|---------|
| NSM | One row-major array of `R*N` values; `(r, c)` is at `r*N + c`. |
| DSM | `N` separate arrays of `R` values, one per column, each its own page-aligned `mmap` (every buffer of 64 KB or more is, so placement never depends on allocator state). Column starts can be staggered (`--dsm-offset`). |
| PAX | Fixed-size pages, each holding `floor(page_size / (N*8 + pad))` tuples. Inside a page, every column has its own contiguous minipage, optionally followed by `--pax-pad` bytes. Rows never span pages, and each page records its own tuple count. |
| DSM-`<enc>`-`<mode>` | DSM with every column encoded (`plain bitpack for dict dictb rle delta`), read in `value` or `vector` mode. |

Two queries cover everything the project asks for:

| Query | SQL | Notes |
|-------|-----|-------|
| `sum` | `SELECT SUM(c0)+…+SUM(c{k-1}), COUNT(*) FROM t [WHERE …]` | `AVG = SUM / COUNT`. No tuples are built. |
| `projection` | `SELECT c0, …, c{k-1} FROM t [WHERE …]` | Writes qualifying tuples row-major into a preallocated `R*k` buffer, so every layout must reconstruct them. With `k = N` this is `SELECT *`, i.e. complete-tuple reconstruction. |

`WHERE` is a conjunction of equality and range predicates on any columns
(`--where 'c1<500&c3=7&c5=10..20'`). `--sel S` adds the predicate `c0 < x`, with `x` taken
from the column's quantiles so that a fraction `S` of the rows qualifies.

Every query can run under five **execution strategies** (early/late materialization,
branching or not; see [below](#execution-strategies)). Every layout×strategy result is
checked against a naive reference loop over the logical table before anything is timed.

Data comes from `std::mt19937_64` with seed 42, so every run builds the same table. By
default it is uniform in `[0, 100)` (`--domain` changes the range); `--dist` picks another
distribution.

## Layout of the code

```
src/aligned_buffer.h      owning array: page-aligned mmap from 64 KB up (2 MB with huge pages), optional start offset
src/table.{h,cpp}         logical table + reproducible data generation
src/machine.{h,cpp}       cache hierarchy from sysfs; machine-derived defaults for the tuning knobs
src/predicate.{h,cpp}     Predicate (col, lo, hi) and the --where parser
src/layout.h              common Layout interface (storage only, plus run(Query&))
src/nsm.* dsm.* pax.*     the uncompressed layouts
src/encoded_dsm.*         DSM with encoded columns, read in value or vector mode
src/encodings/            EncodedColumn interface (decode, select, refine, gather) + one file per encoding
src/query.{h,cpp}         Query interface, QueryParams, Strategy, the auto strategy
src/kernels.h             the scan kernels, shared by all layouts (see below)
src/chunks.h              splits each layout into vectors of rows for the kernels
src/queries/sum.*         SUM / COUNT query
src/queries/projection.*  projection query
src/main.cpp              query registry, argument parsing, correctness check, timing, CSV
src/perf_counters.*       hardware counters via perf_event_open
src/tools/membench.cpp    memory-hierarchy microbenchmark (latency, read/write bandwidth)
src/tools/batchbench.cpp  synthetic benchmark: best vector size vs work per value and fixed cost per vector
scripts/experiments.py    the experiment suite behind the report -> results/<experiment>/
scripts/brief.py          the short results table printed after each experiment run
scripts/figures.py        figures and LaTeX tables from results/ -> report/figures, report/generated
report/report.tex         the report (ACM two-column, acmart sigconf), one file per section in report/sections/
report/extended/          a longer single-column version with every experiment
```

Layouts only store data and expose accessors (`row(r)`, `column(c)`, `minipage(p, c)`).
A query turns any layout into a sequence of **chunks** (`chunks.h`):

- `RowChunk` (a base pointer and a row stride) for NSM;
- `ColumnChunk` (one pointer per referenced column) for DSM, for each PAX page, and for
  decoded vectors of an encoded column.

The kernels in `kernels.h` are templates over the chunk type, so DSM and PAX share the same
code, and NSM differs only in how the next value is addressed. They are also specialised
at compile time on the number of output columns (1, 2, 3, 4, 6, 8, 12, 16 or dynamic) and
predicates (0, 1, 2 or dynamic). This lets the compiler keep every column pointer and
predicate bound in registers. Without it, the early kernels spent about 50 instructions
per row reloading them.

### Adding a query

1. Create `src/queries/<name>.{h,cpp}` with a class that derives from `Query`:
   - Implement `run()` once per layout. The simplest way is a `chunk()` function
     templated on the chunk type, called from `for_each_chunk`.
   - Have `result()` return a value that must match across layouts and strategies. Make
     it sensitive to order if the output's layout matters.
   - Add a static `reference(const Table&, const QueryParams&)` that computes the expected
     result with a naive loop.
   - Allocate any buffers in the constructor, which runs outside the timed region.
2. Add one entry to `kQueries` in `src/main.cpp`.

The Makefile picks up the new file automatically.

## Execution strategies

`--strategy` (default: all but `auto`):

| Strategy | What it does per row / vector |
|----------|-------------------------------|
| `early` | Tuple at a time. Read every referenced attribute (stitch the tuple), then test the predicates. Write the tuple unconditionally and advance the output cursor by `pass` (0/1), so there is no branch. |
| `early-pipe` | Tuple at a time, pipelined: test the predicate attributes first, and read the other attributes only for qualifying rows (one branch per row). |
| `late` | A vector of `--batch` rows at a time. The predicate columns produce a **selection vector** of qualifying positions without branches (`sel[n] = i; n += pass`); further predicates refine it. Then each output column is gathered at those positions, column at a time. |
| `late-branch` | Like `late`, but builds the selection vector with a branch per row. |
| `auto` | Picks `early` or `late` for every span of at least 1024 rows (a vector, or several PAX pages), from the selectivity of the previous span and a threshold derived from the cache line size (`late_threshold()` in `machine.h`), or, with `--auto-measure`, from the measured cost per row of each. Without a WHERE clause it runs late for `sum` and early for `projection`. |

Without a WHERE clause, `early` = `early-pipe` and `late` = `late-branch`, so the driver
runs only one of each pair.

Range predicates compile to a single unsigned comparison:
`(uint64)(v - lo) <= (uint64)(hi - lo)`.

### Encoded columns: predicate pushdown

In vector mode, the late strategies evaluate predicates on the encoded data and decode
only what they must:

| Encoding | `select` (predicate → selection vector) | `gather` (values at selected positions) |
|----------|----------------------------------------|----------------------------------------|
| `plain` | direct comparison | direct loads |
| `bitpack` | compare the packed offsets against `lo-min .. hi-min`, with no decode | extract single values |
| `for` | per block: **zone map** (block min/max) skips whole blocks or takes them entirely; otherwise compare in the block's offset domain | extract single values |
| `dict` | binary-search the sorted dictionary for the code range `[lo', hi']`, then compare codes | extract the code, look it up |
| `dictb` | as `dict`, but the codes are compared where they lie: AVX2 tests 32 one-byte or 16 two-byte codes per step, with no unpacking | look the code up |
| `rle` | one test per run, emitting every position of a qualifying run | walk the runs alongside the sorted positions |
| `delta` | decode-based (the default implementation) | decode-based; SUM without WHERE works on the deltas: a block sums to `m*first + sum_k d_k (m-k)` |

The early strategies on encoded columns decode every referenced column's vector, then run
the uncompressed kernel on it. Value mode (one virtual `ColumnCursor::next()` per value,
like a Volcano operator) supports only `early`.

## Tuning knobs for the machine

Everything machine-specific is read from sysfs at startup (`./build/scan_bench --machine`).
The knobs are all off by default, so the defaults are the untuned baseline.

| Flag | What it changes | Why it helps on this machine |
|------|-----------------|------------------------------|
| `--batch auto` | Vector size = the largest power of two in [256, 16384] whose per-vector state (selection vector, decoded/gathered values, output) fits in a quarter of L2. | Large enough to amortise per-vector costs (a virtual decode call per column, loop setup, a prefetcher restart per column), small enough to stay in L2 between the select and gather phases. Our scans read their buffers sequentially and L2 hits stream almost as fast as L1, so an L1-sized vector (128 rows for decoding SUM) is 1.2× slower (11.9 vs 9.9 ms). The right level depends on the work: `build/batchbench` shows random access inside a vector wants L1, and a large fixed cost per vector wants L3 (`batch_demo` experiment). |
| `--dsm-offset auto` | Column `c` starts `c × 17` cache lines (mod the L2 set stride) into its allocation. | Page-aligned (and especially 2 MB-aligned) column arrays map `col0[r] … col{N-1}[r]` to the **same L1 set**. That makes row-at-a-time reconstruction of more than 8 columns about 3× slower (early `SELECT *` at 1%: 63 vs 22 ms, `aliasing` experiment). 17 is coprime to 64 and 1024, so up to 64 columns land in distinct L1 sets and up to 1024 in distinct L2 sets. |
| `--hugepages` | `madvise(MADV_HUGEPAGE)` on 2 MB-aligned buffers. | Fewer dTLB misses on scans across many columns / pages. |
| `--page-size`, `--pax-pad` | PAX page size, and padding after each minipage. `--pax-pad auto`: where plain packing would put more of a page's minipages in one L1 set than it has ways (stride a multiple of 4 KB: pages ≥ 64 KB with 16 columns), shrink each minipage to an odd number of cache lines. | Page size trades prefetcher-friendly long minipages against reconstruction locality. A fixed byte pad cannot fix aliasing (the stride stays page/N); the odd stride does: early `SELECT *` at 1% on 256 KB pages drops from 63 to 24 ms, close to DSM's 23. |
| `--simd` | AVX2 kernels. **Selection vector:** compare 8 values, `movemask` to an 8-bit pass mask, store that mask's positions from a 256-entry lookup table, advance by `popcount` (also used on bit-packed/FOR/dict codes). **Bit-unpacking** (widths 1–25): 8 values occupy exactly `w` bytes, so one byte shuffle + one variable shift + a mask unpacks 8 at a time. **Fused SUM** on bitpack/for (no WHERE): add the unpacked offsets directly, plus `n × min`; rle multiplies each run's value by its length. | The selection vector drops from ~2 to ~0.9 cycles per row. Decoding has to cost less than the ~1.1 cycles DRAM takes to deliver an 8-byte value for compression to pay off; scalar unpacking does not, AVX2 does (bit-packed SUM 1.6× faster than uncompressed DSM from DRAM). Without decoding, rle SUM is ~87×, delta SUM 1.3× and a `dictb` predicate 2–5× faster than plain. |
| `--stage` | Projection builds each vector's tuples in a cache-resident buffer, then copies them out with `memcpy`. | Turns the column-at-a-time scatter of `late` into sequential writes. |
| `--nt-stores` | Like `--stage`, but copies with non-temporal (streaming) stores. | The output buffer is written once and never read back by the scan, so a read-for-ownership of every output line wastes half the DRAM bandwidth. |
| `--auto-phi F`, `--auto-sigma S` | The `auto` strategy's threshold: late while `f(s) < φ(1 − σ/g)`, with `f(s) = 1 − (1 − s)^v` the fraction of lines holding a selected value, `v` values per line, `g` gathered columns (defaults φ = 0.55, σ = 2). | `v` comes from the line size; φ (a gathered line costs ~1/φ streamed ones) and σ (the selection vector costs ~σ column scans) were fitted to the DSM crossovers from DRAM (2.4% for g = 3, 7.4% for g = 15). See `late_threshold()` in `machine.h`. |
| `--auto-cache` (plus `--auto-cache-phi F`, `--auto-cache-sigma S`) | When the table's footprint fits in L3, `auto` uses a second pair of threshold constants (defaults φ = 1.0, σ = 2) instead of `--auto-phi`/`--auto-sigma`. | In cache a gathered line costs about as much as a streamed one, so late wins far longer for projections. With the table in L3, `SELECT *`'s worst case drops from 2.6× to 1.3× of the better fixed strategy (`auto_cache_aware` experiment). SUM in cache would need its own constants. |
| `--auto-measure` | `auto` times every span (rdtsc) and keeps a moving average of cycles per row for early and for late, per power-of-two range of the previous span's selectivity; samples are clipped at twice the estimate, so an interrupted vector does not flip the choice. Once both are measured in a range, it runs the cheaper one, and re-measures the other every 32 × (cost ratio) spans. The threshold above only decides until then. | Replaces the fitted constants with measurements, so it also adapts to the layout (PAX's early is far slower than DSM's) and to where the data lives (in L3 late wins up to 20–33%, not 1.4–7.4%). |

The `knobs` CSV column records which knobs a row ran with, e.g. `off1088+huge+simd+nt`.

## Build and quick use

```
make                 # build/scan_bench, build/membench, build/batchbench (-std=c++17 -O3 -march=native)
make BUILD=build-dev # a second build directory (e.g. while experiments run from build/)
make clean

make test            # k = 1 and k = N for every query, layout and strategy, as a table
make test q2 sel=0.01,0.5 strategy=early,late
make test q1 enc=all dist=sorted
make test q2 knobs="--hugepages --dsm-offset auto --nt-stores"
make test q2 perf    # also record hardware counters (core set); perf=mem / perf=stall
make graph q2        # latency vs k -> output/q2/graph_q2.png (log: log y axis)
make encodings q1    # every encoding x distribution, plus a summary of ratio and speedup

make experiments                   # every experiment behind the report (~80 min) -> results/
make experiments selectivity pax   # only the named experiments (list below)
make experiments list              # names and number of runs
make figures         # report/figures/*.pdf|png and report/generated/*.tex from results/
make report          # figures + latexmk -> report/report.pdf
```

`make test` pipes the CSV through `scripts/summarize.py`, which prints the median, min and
max latency for each configuration. It reports the input read bandwidth as `k*R*8 / latency`,
which is only meaningful without a predicate. `TEST_ROWS` (default 1,000,000), `TEST_COLS`
(16) and `TEST_PAGE_SIZE` (16384) set the table. `sel=`, `strategy=`, `page=`, `batch=`,
`enc=`, `mode=`, `dist=`, `layout=` and `knobs=` pass through to `scan_bench`. The script
also works on any output: `./build/scan_bench … | python3 scripts/summarize.py`.

`make graph` draws median latency vs k, one series per layout (and per strategy, with line
style marking the strategy). Plotting needs matplotlib: the first `make graph` creates a
virtualenv in `.venv/` and installs `requirements.txt` into it.

### Hardware counters (`perf`)

`--perf SET` opens a group of hardware counters with `perf_event_open`. The counters run
only around each timed `run()`. Raw counts are added to the CSV as extra columns;
`scripts/perf_metrics.py` turns them into per-row metrics.

| Set | Events | Question it answers |
|-----|--------|---------------------|
| `core` | cycles, instructions, L1D loads, L1D misses, dTLB misses | Is the scan limited by the CPU (high IPC) or by memory? How many cache lines/pages does it touch? |
| `mem` (AMD Zen 3) | cycles; L1 fills for demand loads from L2 / L3 / DRAM; L1 fills by the hardware prefetcher from DRAM | Did data arrive because the code stalled on it, or because the prefetcher brought it in ahead of time? |
| `stall` (AMD Zen 3) | cycles, instructions, dispatch stalls on a full load queue / store queue | Is the core waiting on loads or on stores? |

Only user-space code is counted (`perf_event_paranoid=2` is enough). Each set has at most
5 events: under WSL2 only 5 of Zen 3's 6 counters are free, and a group that doesn't fit
is never scheduled. The program then exits with an error instead of reporting zeros.

### Memory microbenchmark

`./build/membench [--max-mb 512] [--hugepages]` measures load-to-use latency (a pointer
chase over a random cyclic permutation) and read / write / non-temporal-write bandwidth
for working sets from 4 KB to `--max-mb`. It prints CSV `test,bytes,value`. The report uses
it to place L1/L2/L3/DRAM boundaries and costs.

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--rows R` / `--cols N` | 1000000 / 16 | table size |
| `--dist D` | `uniform` | `uniform`, `wide`, `sorted`, `runs`, `zipf` (see Encodings) |
| `--domain D` | 100 | uniform data is uniform in `[0, D)` |
| `--run-length L` | 64 | mean run length for `--dist runs` |
| `--layout L1,L2` | all | uncompressed layouts: `nsm`, `dsm`, `pax`, or `none` |
| `--page-size B,…` | 16384 | PAX page size(s); each size builds its own PAX layout |
| `--pax-pad B` | 0 | padding after each PAX minipage, or `auto` (odd-line stride where minipages alias) |
| `--dsm-offset B` | 0 | DSM column start stagger in bytes, or `auto` |
| `--hugepages`, `--simd`, `--stage`, `--nt-stores` | off | tuning knobs (above) |
| `--encoding E1,…` / `--mode M1,…` | none / both | encoded DSM layouts and scan modes |
| `--query Q1,…` | all | `sum`, `projection` |
| `--k K1,…` | `1,2,4,…,N` | number of leading columns each query reads |
| `--sel S1,…` | none | add `c<P> < x` with selectivity S (one run per S) |
| `--pred-col P` | 0 | column of the `--sel` predicate |
| `--where W` | none | fixed predicates, ANDed with the `--sel` one: `c1<500&c2=7&c3=10..20` (`= < <= > >=`, `=a..b` inclusive range) |
| `--strategy S1,…` | all but `auto` | execution strategies |
| `--batch B1,…` | 1024 | rows per vector, a multiple of 64, or `auto` |
| `--auto-phi F`, `--auto-sigma S` | 0.55, 2 | `auto` strategy threshold parameters |
| `--auto-cache` (plus `--auto-cache-phi F`, `--auto-cache-sigma S`) | When the table's footprint fits in L3, `auto` uses a second pair of threshold constants (defaults φ = 1.0, σ = 2) instead of `--auto-phi`/`--auto-sigma`. | In cache a gathered line costs about as much as a streamed one, so late wins far longer for projections. With the table in L3, `SELECT *`'s worst case drops from 2.6× to 1.3× of the better fixed strategy (`auto_cache_aware` experiment). SUM in cache would need its own constants. |
| `--auto-cache` | off | `auto` uses separate constants when the table fits in L3 (above) |
| `--auto-measure` | off | `auto` chooses by measured cost per row (above) |
| `--iters N` / `--warmup N` | 10 / 2 | timed and untimed runs per configuration |
| `--rounds R` | 1 | repeat the whole sweep R times, interleaving configurations |
| `--min-time-ms T` | 0 | repeat `run()` inside one timed sample until it lasts T ms (cache-resident tables) |
| `--perf SET` | none | `core`, `mem`, `stall` |
| `--no-check` | | skip the correctness check |
| `--output FILE` | stdout | CSV destination |
| `--machine` | | print the detected cache hierarchy and exit |

**Longer, steadier runs.** Raise `--iters`; add `--rounds 3` so a slow period (WSL2,
another process) hits every configuration instead of one; use `--min-time-ms 20` when a
single scan takes microseconds; pin with `taskset -c 2`. `scripts/experiments.py` does all
of this.

## What a run does

1. Generates the table and builds the selected layouts (and encodes columns). Not timed.
2. Prints the table size and each layout's footprint to stderr.
3. Builds every (query, k, selectivity, batch) configuration, computes its reference
   result with a naive loop over the logical table, and checks that every layout and
   strategy reproduces it. It exits with status 1 on any mismatch. For `projection`, the
   result is a position-weighted checksum of the output, so a value in the wrong row or
   column is caught.
4. Times `--rounds` × (`--warmup` + `--iters`) runs of each configuration with
   `std::chrono::steady_clock`, folding every result into a `volatile` sink.

One CSV row per timed run:

```
query,layout,strategy,dist,rows,cols,k,where,sel,page_size,batch,knobs,footprint_bytes,iteration,latency_ns,rows_out[,counters…]
```

`where` is the full predicate (e.g. `c0<500231`), `sel` the requested selectivity (1 = no
predicate), `rows_out` the number of qualifying rows. `latency_ns` is per `run()`, even
when `--min-time-ms` repeats it inside a sample.

## Encodings (DSM only)

| Encoding | Storage | Compresses well when |
|----------|---------|----------------------|
| `plain` | The `int64` array, same as DSM | Never. Measures the cost of the scan machinery alone. |
| `bitpack` | `v - min(column)` in `w` bits, where `w` covers the whole column's range | The whole column has a narrow range |
| `for` | Frame of reference: like `bitpack`, but each block of 1024 values has its own min, max and width | Each block has a narrow range (e.g. sorted data) |
| `dict` | Sorted distinct values + a bit-packed code per row | There are few distinct values |
| `dictb` | Like `dict`, but each code takes 1, 2 or 4 bytes (the smallest that fits), so it needs no unpacking | Few distinct values, and predicates matter more than the last bits of compression |
| `rle` | One (value, run end) pair per run of equal values | Values repeat in long runs |
| `delta` | Per block of 1024: the first value + zigzag-encoded, bit-packed differences | Neighbouring values are close (e.g. sorted data) |

| `--dist` | Data | Favours |
|----------|------|---------|
| `uniform` (default) | uniform in `[0, domain)` | `bitpack`, `for`, `dict` |
| `wide` | uniform over all of `int64` | nothing; shows what each encoding costs when it can't compress |
| `sorted` | increasing; each value = previous + uniform `[0, 8)` | `delta`, `for` |
| `runs` | runs of one value in `[0, 2^20)`, lengths uniform in `[1, 2L-1]` | `rle` |
| `zipf` | Zipf (s = 1) over 10,000 distinct full-range values | `dict`, `dictb` |

`sum` accumulates modulo 2^64 (unsigned), so `wide` and `zipf` data can't overflow it.
Every encoding keeps its own copy of the table, so budget memory accordingly.

## Experiments, figures and the report

```
make experiments                         # all experiments, in the order below
make experiments <name> [<name> ...]     # e.g. make experiments selectivity late_breakdown
make experiments list                    # names and number of runs
make experiments <name> force=1          # re-run even if the results already exist
make experiments quick=1                 # 1/8 of the rows, into results_quick/ (smoke test)
make experiments <name> quiet=1          # don't print the results tables
make experiments <name> detail=1         # full statistics and hardware counters per run
```

After every run, measured now or already on disk, the script prints its results
(`scripts/brief.py`): one line per configuration and one column per strategy, with the median
latency in ms. Settings that are the same on every line are printed once above the table.
membench prints latency and bandwidth per working-set size, and batchbench its own table.
`detail=1` prints each run with `scripts/summarize.py` instead: median, min and max latency,
and, for runs recorded with `--perf`, the counters per row (IPC, cycles, instructions, L1D
loads and misses, dTLB misses). Counters are recorded by `projectivity`, `selectivity` (not the
two-predicate run), `pax`, `batch`, `dsm_knobs`, `aliasing`, `output_knobs` (`mem` set) and
`predicates`. The same works on one file:
`python3 scripts/summarize.py < results/<experiment>/<run>.csv`.

Each experiment is a list of runs in `scripts/experiments.py`; each run writes
`results/<experiment>/<run>.csv` (`.txt` for `batchbench`) plus a `.log` with the exact
command and its stderr, pinned to one core with `taskset -c 2`. A run whose output already
exists is not run again ("already measured"), but its results are still printed; so an
interrupted suite resumes where it stopped, and `make experiments <name>` on finished
results is a quick way to look at them. `force=1` re-runs it.
After new results, `make figures` redraws the figures and tables and `make report` rebuilds
the PDF. The names can also be passed as `make experiments only=a,b`, or to the script
directly: `python3 scripts/experiments.py --only a,b [--force] [--quick] [--cpu C]`.

Times are from the last full run on the test laptop (4M rows × 16 columns from DRAM unless
noted). "Report" is the section of `report/report.tex` that uses the results.

| Name | What it measures | Runs | Time | Report |
|------|------------------|-----:|-----:|--------|
| `machine` | memory hierarchy: latency and read/write/NT bandwidth vs working-set size (`membench`) | 1 | 3 min | §3, Table 1 |
| `projectivity` | SUM and projection of k = 1..16 attributes, no predicate, early vs late | 3 | 1 min | §4, Figure 1 |
| `selectivity` | 0.01%–100% selectivity; projection k = 2, 4, 16, SUM k = 4, and a second 50% predicate; all four fixed strategies | 5 | 5 min | §4, §5 |
| `width` | table width N = 2..256 at a fixed 512 MB | 8 | 5 min | §4 |
| `workingset` | table size 16 KB – 512 MB (L1 to DRAM) | 16 | 3 min | §4 |
| `pax` | PAX page size 1 KB – 4 MB; plain, 64-byte-padded and odd-line minipages | 32 | 7 min | §4, §7 |
| `encodings` | every encoding × 5 distributions (N = 8), value and vector mode, scalar and AVX2 | 10 | 12 min | §6 |
| `enc_selectivity` | predicates on compressed columns, early vs late, scalar and AVX2 | 6 | 5 min | §6 |
| `enc_workingset` | compression vs table size, 64 KB – 256 MB: does it pay in cache? | 14 | 1 min | §6 |
| `enc_dict` | dict (bit-packed codes) vs dictb (byte codes) vs plain on Zipf data | 2 | 2 min | §6 |
| `batch` | vector size 64 – 65536 rows for late projection and vectorized decoding | 2 | 3 min | §7 |
| `dsm_knobs` | DSM column offsets (aligned vs staggered) × 4 KB / 2 MB pages | 4 | 1 min | §7 |
| `aliasing` | DSM column start stagger from 0 to 32 KB, incl. the `--dsm-offset auto` 1088 B, early `SELECT *` at 1% (L1 set conflicts) | 11 | 1 min | §7 |
| `output_knobs` | output writes: direct vs stage + `memcpy` vs stage + streaming stores | 3 | 2 min | §7 |
| `simd` | scalar vs AVX2 selection vector, table in DRAM, L3 and L2 | 6 | 3 min | §5 |
| `selectivity_cache` | the selectivity sweep with a 4 MB table (in L3), then on DSM with the AVX2 selection vector | 8 | 1.5 min | §5 (Table 3) |
| `auto` | auto (threshold and measured) vs early and late; uniform and sorted data from DRAM, uniform in L3 | 18 | 11 min | §5 |
| `predicates` | equality, range and two-attribute predicates | 5 | 2 min | §4 |
| `tuned` | untuned defaults vs every machine-derived knob, 6 queries × 3 layouts (the 1.46×) | 12 | 3 min | §7 |
| `late_breakdown` | late's fixed first pass (scalar vs AVX2) and cost per gathered column, DSM, k = 1..16 | 5 | 2 min | §5 |
| `batch_demo` | best vector size vs work per value and fixed cost per vector (`batchbench`, synthetic) | 6 | 2 min | §7 |
| `auto_cache_aware` | auto with DRAM constants vs in-cache constants (`--auto-cache`) vs measuring, table in L3 | 9 | 2 min | §5 |

All experiments stagger DSM columns (`--dsm-offset auto`) except the ones that study
placement (`dsm_knobs`, `aliasing`, and the `untuned_*` runs of `tuned`): page-aligned columns
make row-at-a-time access to more than 8 columns thrash one L1 set (3× slower), which would
distort every layout comparison. `results/auto_cache_aware/summarize.py` prints that
experiment's comparison table.

`scripts/figures.py` reads `results/` and writes `report/figures/*.pdf` (and `.png` with
`--png`) and the LaTeX tables in `report/generated/`, which both versions of the report
include. Building the ACM report locally needs the `acmart` class and its fonts
(Debian/Ubuntu: `texlive-publishers texlive-fonts-extra texlive-plain-generic`).
