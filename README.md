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

All buffers are 64-byte aligned. Data is uniform in `[0, 100)` from
`std::mt19937_64` with a fixed seed (42), so every run produces the same table.

## Layout of the code

```
src/aligned_buffer.h   64-byte aligned owning array
src/table.{h,cpp}      logical table + reproducible data generation
src/layout.h           common Layout interface (storage only, plus run(Query&))
src/nsm.{h,cpp}        NSM layout
src/dsm.{h,cpp}        DSM layout
src/pax.{h,cpp}        PAX layout
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
```

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--rows R` | 1000000 | number of rows |
| `--cols N` | 16 | number of `int64_t` columns |
| `--page-size B` | 16384 | PAX page size in bytes (must be a multiple of 64 and hold at least one tuple) |
| `--k K1,K2,...` | `1,2,4,...,N` | values of `k` (number of leading columns each query reads); `N` is always included in the default |
| `--query Q1,Q2` | all | queries to run (`sum`, `projection`) |
| `--output FILE` | stdout | where to write the CSV |

## What a run does

1. Generates the table and builds all three layouts. This step is not timed.
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
query,layout,rows,cols,k,page_size,iteration,latency_ns,result
```

`page_size` is `0` for NSM and DSM because they have no pages.

## Example

```
$ ./build/scan_bench --rows 1000000 --cols 16 --output results.csv
Table: 1000000 rows x 16 cols, 122.07 MB of data
  NSM footprint: 122.07 MB
  DSM footprint: 122.07 MB
  PAX footprint: 122.078 MB
Correctness check passed for queries {sum,projection}, k in {1,2,4,8,16}

$ head -3 results.csv
query,layout,rows,cols,k,page_size,iteration,latency_ns,result
sum,NSM,1000000,16,1,0,0,4064834,49484225
sum,NSM,1000000,16,1,0,1,3789312,49484225
```

Note: while the layouts are being built, the logical table and all three
layouts are in memory at once, which is about 4× the table size.
