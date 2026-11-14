# NSM / DSM / PAX scan benchmark

A small in-memory benchmark that compares three physical layouts of the same
`int64_t` table. Each query reads the first `k` of `N` columns:

| Query | SQL | Reconstructs tuples? |
|-------|-----|----------------------|
| `sum` | `SELECT SUM(c0) + ... + SUM(c{k-1}) FROM t` | No. DSM and PAX aggregate one column at a time. |
| `projection` | `SELECT c0, ..., c{k-1} FROM t` | Yes. Writes row-major tuples into a preallocated `R*k` output buffer. |

| Layout | Storage |
|--------|---------|
| NSM | One row-major array of `R*N` values; `(r, c)` is at `r*N + c`. |
| DSM | `N` separate arrays of `R` values, one per column. |
| PAX | Fixed-size pages, each holding `floor(page_size / (N*8))` tuples. Inside a page, every column has its own contiguous minipage. The tuple count is stored per page, since the last page may be partial. |

DSM can also be run with every column compressed (see [Encodings](#encodings-dsm-only)).

All buffers are 64-byte aligned. By default, data is uniform in `[0, 100)` from
`std::mt19937_64` with a fixed seed (42), so every run produces the same table.
`--dist` picks another distribution.

## Layout of the code

```
src/aligned_buffer.h   64-byte aligned owning array
src/table.{h,cpp}      logical table + reproducible data generation
src/layout.h           common Layout interface (storage only, plus run(Query&))
src/nsm.{h,cpp}        NSM layout
src/dsm.{h,cpp}        DSM layout
src/pax.{h,cpp}        PAX layout
src/encoded_dsm.{h,cpp}  DSM with encoded columns, read in value or vector mode
src/encodings/encoded_column.*  EncodedColumn / ColumnCursor interfaces + encoding registry
src/encodings/bits.*   bit-packing helpers (pack, extract one value, unrolled unpack)
src/encodings/<name>.cpp  one encoding each: plain bitpack for dict rle delta
src/query.h            Query interface: one run() overload per layout, plus result()
src/queries/sum.*      SUM query
src/queries/projection.*  projection query
src/main.cpp           query registry, argument parsing, correctness check, timing, CSV
```

Layouts only store data and expose accessors (`row(r)`, `column(c)`,
`minipage(p, c)`). All of a query's code, for every layout, lives in its own file.

### Adding a query

1. Create `src/queries/<name>.{h,cpp}` with a class that derives from `Query`:
   - Implement `run()` once per layout.
   - Have `result()` return a value that must match across layouts. Make it
     sensitive to order if the output's layout matters.
   - Allocate any buffers in the constructor, which runs outside the timed region.
2. Add one entry to `kQueries` in `src/main.cpp`.

The Makefile picks up the new file automatically.

## Build

```
make            # builds build/scan_bench with -std=c++17 -O2 -march=native
make clean
make test       # quick run at low and high projectivity, printed as a table
make test q1    # same, sum query only (q1/Q1 = sum, q2/Q2 = projection)
make graph      # latency vs k for all layouts, one graph per query (output/qN/)
make graph q2   # same, projection only -> output/q2/graph_q2.png
make graph q2 log  # same with a log y axis -> output/q2/graph_q2_log.png
make test q2 perf  # also record hardware counters (core set); perf=mem / perf=stall for others
make graph q2 perf # plus one graph per counter metric -> output/q2/graph_q2_<metric>.png
```

`make test` runs the benchmark with only `k = 1` (low projectivity, p = 1/N)
and `k = N` (high projectivity, p = 1), for every query. It pipes the CSV
through `scripts/summarize.py`, which prints the median, min and max latency
and the input read bandwidth (GB/s) for each query, k and layout. To change the table size, use
`make test TEST_ROWS=4000000 TEST_COLS=32`. The script also works on any
benchmark output, e.g. `./build/scan_bench | python3 scripts/summarize.py`.

Query ids: `q1` = sum, `q2` = projection. The ids and `log` are case-insensitive.

`make graph` runs every k (1, 2, 4, ..., N) for each selected query. For query
`qN` it writes the raw results to `output/qN/graph_qN.csv` and the plot to
`output/qN/graph_qN.png`. The plot shows median latency vs k for each layout,
with a shaded min–max band. The x axis is log₂ because k doubles each step. The
y axis is linear by default; adding `log` switches it to a log scale and saves the
plot as `graph_qN_log.png`, so it doesn't overwrite the linear one. The graphs use
the same `TEST_ROWS` / `TEST_COLS` as `make test`. Plotting needs matplotlib: the
first `make graph` creates a virtualenv in `.venv/` and installs
`requirements.txt` into it.

To add query 3, add `q3` to `QUERY_IDS` in the Makefile and set `QUERY_q3 := <name>`.

### Hardware counters (`perf`)

`--perf SET` (or `perf` / `perf=SET` with `make`) opens a group of hardware
counters with `perf_event_open`. The counters run only around each timed `run()`,
so data generation, setup and the correctness check are not counted. Raw counts
are added to the CSV as extra columns after `result`. `summarize.py` and `plot.py`
turn them into per-row metrics and derived ratios (IPC, L1D miss rate).

| Set | Events | Question it answers |
|-----|--------|---------------------|
| `core` | cycles, instructions, L1D loads, L1D misses, dTLB misses | Is the scan limited by the CPU (high IPC) or by memory? How many cache lines/pages does it touch? |
| `mem` (AMD Zen 3) | cycles; L1 fills for demand loads from L2 / L3 / DRAM; L1 fills by the hardware prefetcher from DRAM | Did the data arrive because the code asked for it (a stall) or because the prefetcher brought it in ahead of time? |
| `stall` (AMD Zen 3) | cycles, instructions, dispatch stalls on a full load queue / store queue | Is the core waiting on loads or on stores? |

Only user-space code is counted (`perf_event_paranoid=2` is enough). Each set
has at most 5 events: under WSL2 only 5 of Zen 3's 6 counters are free, and a
group that doesn't fit is never scheduled at all. The program exits with an
error in that case instead of reporting zeros. `mem` and `stall` use raw AMD
event codes and refuse to run on other CPUs.

## Encodings (DSM only)

`--encoding` adds DSM layouts whose columns are each stored in one encoding.
Only the column store gets encodings: NSM and PAX stay uncompressed as reference
points. Each encoding is run in two scan modes, so `--encoding dict` adds two
layouts, `DSM-dict-value` and `DSM-dict-vector`. Both share one encoded copy of
the data.

| Encoding | Storage | Compresses well when |
|----------|---------|----------------------|
| `plain` | The `int64` array, same as DSM | Never. Measures the cost of the scan machinery alone, with no decoding. |
| `bitpack` | `v - min(column)` in `w` bits, where `w` covers the whole column's range | The whole column has a narrow range |
| `for` | Frame of reference: like `bitpack`, but each block of 1024 values has its own min and width | Each block has a narrow range (e.g. sorted data) |
| `dict` | Sorted distinct values + a bit-packed code per row | There are few distinct values, however large or spread out |
| `rle` | One (value, run end) pair per run of equal values | Values repeat in long runs |
| `delta` | Per block of 1024: the first value + zigzag-encoded, bit-packed differences between neighbours | Neighbouring values are close (e.g. sorted data) |

| Mode | How a query reads a column |
|------|----------------------------|
| `value` | One virtual `ColumnCursor::next()` call per value, like a tuple-at-a-time (Volcano) operator that doesn't know the encoding at compile time. |
| `vector` | One virtual `EncodedColumn::decode()` call per 1024 values into a buffer, then a tight loop over the buffer. Bit-unpacking uses a kernel unrolled for each width from 0 to 64. |

Each query implements both modes in its own file. `projection` in vector mode
decodes one column's batch and scatters it into that column of the batch's
output rows. The CSV's `footprint_bytes` column, and `size_MB` / `ratio` in
`make test`, report the compressed size.

To make the encodings meaningful, `--dist` generates each column independently:

| `--dist` | Data | Favours |
|----------|------|---------|
| `uniform` (default) | uniform in `[0, 100)`; 7 bits | `bitpack`, `for`, `dict` |
| `wide` | uniform over all of `int64` | nothing; shows what each encoding costs when it can't compress |
| `sorted` | increasing; each value = previous + uniform `[0, 8)` | `delta`, then `for` |
| `runs` | runs of one value in `[0, 2^20)`, lengths uniform in `[1, 2L-1]` (`--run-length L`, default 64) | `rle` |
| `zipf` | Zipf (s = 1) over 10,000 distinct values, each a full-range `int64` | `dict` |

`sum` accumulates modulo 2^64 (unsigned), so `wide` and `zipf` data can't
overflow it.

```
make test q1 enc=all                # every encoding, both modes, plus uncompressed DSM
make test q1 enc=dict,rle dist=runs
make graph q1 enc=all dist=sorted   # -> output/q1/graph_q1_enc_sorted.png
make graph q2 enc=all mode=vector   # vector mode only
make test enc=all layout=nsm,dsm,pax  # also include NSM and PAX
```

`make encodings` runs everything at once. For each selected query and each
distribution, it runs uncompressed DSM plus every encoding over every k and
draws `output/qN/graph_qN_enc_<dist>.png`. It then summarises the runs in
`output/qN/encodings_qN.png` and the encoded sizes in `output/qN/encodings_qN_size.png`
(with the exact numbers in `encodings_qN.csv`), at
k = N: the compression ratio, and the speedup over uncompressed DSM in each scan
mode, per distribution and encoding. Bars above 1× mean better than
uncompressed DSM.

```
make encodings TEST_ROWS=1000000 TEST_COLS=16   # both queries, ~3 minutes
make encodings q1 mode=vector
```

Memory: every encoding keeps its own copy of the table. With the default
100K × 512 table (390 MB) and `wide` data, that's about 3.6 GB in total, so on a
machine with little RAM use fewer rows or columns.

With `enc=`, `make` runs only uncompressed DSM as the baseline unless you pass
`layout=`. In graphs, colour marks the encoding and line style the mode
(solid = vector, dashed = value). With more than four series, the min–max bands
are left out.

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--rows R` | 1000000 | number of rows |
| `--cols N` | 16 | number of `int64_t` columns |
| `--page-size B` | 16384 | PAX page size in bytes (must be a multiple of 64 and hold at least one tuple) |
| `--k K1,K2,...` | `1,2,4,...,N` | values of `k` (number of leading columns each query reads); `N` is always included in the default |
| `--query Q1,Q2` | all | queries to run (`sum`, `projection`) |
| `--output FILE` | stdout | where to write the CSV |
| `--perf SET` | none | also record hardware counters (`core`, `mem`, `stall`) |
| `--dist D` | `uniform` | data distribution: `uniform`, `wide`, `sorted`, `runs`, `zipf` |
| `--run-length L` | 64 | mean run length for `--dist runs` |
| `--layout L1,L2` | all | uncompressed layouts to run: `nsm`, `dsm`, `pax`, or `none` |
| `--encoding E1,E2` | none | also run DSM with encoded columns: `all`, or any of `plain`, `bitpack`, `for`, `dict`, `rle`, `delta` |
| `--mode M1,M2` | both | scan modes for encoded layouts: `value`, `vector` |

## What a run does

1. Generates the table and builds the selected layouts (and encodes the
   columns, for encoded layouts). This step is not timed.
2. Prints the table size in MB, and each layout's actual footprint, to stderr.
3. Checks that all layouts produce the same `result()` for every query and
   `k`, using a fresh query object per layout. If any differ, it prints the
   mismatch and exits with status 1. For `projection`, the result is a
   position-weighted checksum of the output, so values in the wrong row or
   column are caught too.
4. For each query, `k` and layout, runs 2 warm-up runs and then 10 timed runs
   using `std::chrono::steady_clock`. Only `run()` is timed. Query setup and
   `result()` are not. Each result is added into a `volatile` sink so the
   compiler cannot remove the work.

It writes one CSV row per timed scan:

```
query,layout,dist,rows,cols,k,page_size,footprint_bytes,iteration,latency_ns,result
```

`page_size` is `0` for every layout except PAX. `footprint_bytes` is the
layout's actual size in memory (the compressed size for encoded layouts).

## Example

```
$ ./build/scan_bench --rows 1000000 --cols 16 --output results.csv
Table: 1000000 rows x 16 cols, 122.07 MB of data, uniform distribution
  NSM footprint: 122.07 MB
  DSM footprint: 122.07 MB
  PAX footprint: 122.078 MB
Correctness check passed for queries {sum,projection}, k in {1,2,4,8,16}

$ head -3 results.csv
query,layout,dist,rows,cols,k,page_size,footprint_bytes,iteration,latency_ns,result
sum,NSM,uniform,1000000,16,1,0,128000000,0,6655101,49484225
sum,NSM,uniform,1000000,16,1,0,128000000,1,6166144,49484225
```

Note: while the layouts are being built, the logical table and all three
layouts are in memory at once, which is about 4× the table size. Each encoding
adds its compressed size on top of that.
