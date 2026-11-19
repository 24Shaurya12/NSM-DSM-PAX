# NSM / DSM / PAX scan benchmark: project summary

An in-memory C++17 benchmark comparing three physical layouts of the same
`int64_t` table (NSM = row store, DSM = column store, PAX = pages split into
per-column minipages) on scan queries. It also includes scripts for tables and
graphs, and hardware-counter collection with `perf`.

## What is on GitHub

Remote: `origin` → https://github.com/24Shaurya12/NSM-DSM-PAX (branch `main`).

| | Commit | Date | Contents |
|---|---|---|---|
| **Pushed** | `d9f0122` "Add basic framework for scanning k out of N columns" | 2026-10-12 | Stage 1 only: the three layouts, the `SUM` over the first k columns, the correctness check, the timing driver with CSV output, the Makefile and the README. 25 files, +710 lines. This includes 11 compiled files from `build/` (`.o`, `.d`, the `scan_bench` binary and `example.csv`). |

Local `main` and `origin/main` are both at `d9f0122`. Nothing is waiting to be pushed.

**Everything after stage 1 is uncommitted.** It exists only in the working tree:

| Kind | Files | Size |
|---|---|---|
| Modified, tracked | `Makefile`, `README.md`, `src/main.cpp`, `src/layout.h`, `src/{nsm,dsm,pax}.{h,cpp}` | +301 / −90 lines |
| New, untracked | `src/query.h`, `src/queries/{sum,projection}.{h,cpp}`, `src/perf_counters.{h,cpp}`, `scripts/{summarize,plot,perf_metrics}.py`, `requirements.txt`, `.gitignore` | 584 lines |
| Generated, untracked | `output/q2/*` (graphs and CSV), `build/queries/`, `build/perf_counters.*` | — |
| Yours, untracked | `questions` | — |

## What was implemented

### Stage 1: baseline (committed)
- **Data:** `R × N` `int64_t` values, uniform in [0, 100), from `std::mt19937_64` with seed 42. The data is generated once and all layouts are built from it.
- **Layouts**, each in its own `.h`/`.cpp`, all 64-byte aligned:
  - NSM: one row-major array.
  - DSM: one array per column.
  - PAX: fixed-size pages (`--page-size`, default 16 KB) holding `floor(page_size / (N·8))` tuples each, with one minipage per column. Each page stores its own tuple count, since the last page can be partial.
- **Driver:** setup is not timed. It runs 2 warm-up iterations and then 10 timed ones (`steady_clock`) per layout and k. A `volatile` sink keeps the compiler from removing the work. Output is CSV, and the table size in MB goes to stderr.
- **Correctness check:** all layouts must produce the same result for every k before any timing starts.

### Stage 2: queries, one file each (uncommitted)
- **Query interface** (`src/query.h`): a query has one `run()` per layout, plus `result()`. Layouts now only store data and provide accessors. Each layout's `run(Query&)` calls the matching overload.
- **`sum` (q1):** the original SUM, moved to `src/queries/sum.cpp`. Results are unchanged.
- **`projection` (q2):** `SELECT c0..c{k-1} FROM t`. It writes row-major tuples into an output buffer that is allocated before timing, so every layout has to rebuild tuples. Its `result()` is a checksum that depends on position, so values in the wrong row or column are caught.
- **Correctness check** uses a fresh query object per layout. I confirmed it catches a bug by deliberately swapping DSM's column order, which made it fail.
- **Adding a query:** create `src/queries/<name>.{h,cpp}`, add one entry to `kQueries` in `main.cpp`, and map a `qN` id in the Makefile.
- **New flags:** `--query`, `--k`, `--output`, `--perf`. The CSV gained a `query` column.

### Stage 3: tooling (uncommitted)
- **`make test`** runs at low and high projectivity (k = 1 and k = N) and prints a summary table (`scripts/summarize.py`) with median, min and max latency and read GB/s.
- **`make graph`** runs every k (1, 2, 4, …, N) and draws latency against k for all three layouts using matplotlib (`scripts/plot.py`). Output goes to `output/qN/graph_qN.{csv,png}`. The x axis is log₂ and the y axis is linear; `log` makes the y axis log as well.
- **Modifiers** work with both `test` and `graph` and ignore case: `q1`/`q2` select a query, `log` switches to a log y axis, and `perf` / `perf=mem` / `perf=stall` add hardware counters.
- **Python environment:** matplotlib lives in `.venv/` (git-ignored), which the first `make graph` creates.

### Stage 4: hardware counters (uncommitted)
- **`src/perf_counters.{h,cpp}`:** a group of counters opened with `perf_event_open`. They are turned on only around each timed `run()`, and the clock reads sit inside that window. Raw counts are written as extra CSV columns.
- **Event sets.** Each has at most 5 events, because only 5 of Zen 3's 6 counters are free under WSL2:
  - `core` (works on any CPU): cycles, instructions, L1D loads and misses, dTLB misses.
  - `mem` (AMD Zen 3): L1 fills for demand loads from L2, L3 or DRAM, and L1 fills by the hardware prefetcher from DRAM.
  - `stall` (AMD Zen 3): dispatch stalls on a full load queue or store queue.
- **Error handling:** if a counter group never runs, the program exits with an error instead of reporting zeros. This was a real bug: the first version of `mem` had 6 events and silently produced all zeros.
- **Metrics:** `scripts/perf_metrics.py` turns raw counts into per-row values, IPC and L1 miss rate. With `perf`, `make graph` draws one graph per metric.

## Findings so far

Measured on a Ryzen 5 5600H (32 KB 8-way L1D, 16 MB L3) under WSL2. Medians.

1. **Projection: DSM wins when k is small, NSM wins at k = N.** Rebuilding tuples makes DSM read k separate column streams at once. The crossover was between k = 8 and 16 at N = 16.
2. **DSM's projection slows sharply from k = 8 to 16** (3.5 → 13.2 ms at 300K rows, about 3.8× for 2× the data). Every DSM column starts at the same offset within a 4 KB page (`addr % 4096 = 64`), so `col0[r]..col15[r]` all compete for one 8-way L1 set. The counters confirm it: **9.9 L1 misses per row against an ideal of 2**, while NSM and PAX are at about 4.
3. **SUM at k = 1: DSM is 11–16× faster than NSM, not "at least 16×".** 16× is the most it could gain (the ratio of bytes moved). The scan is sequential, so the prefetcher hides most of the miss latency: NSM has only 0.37 demand fills from DRAM per row. DSM's loop is limited by the CPU (IPC 5.6 against NSM's 0.94), because GCC at `-O2` didn't use SIMD for it.
4. **NSM's projection at k = 16 is limited by writes,** with 8.4 store-queue stall cycles per row.

## Open issues and caveats

- **`build/` is committed to git.** Compiled files show up in every diff. I suggest adding `build/` to `.gitignore` and running `git rm -r --cached build`.
- **`output/` is neither ignored nor committed.** Decide whether the graphs belong in the repo.
- **PAX page size can't be set from make.** `make test` and `make graph` always use 16 KB. With the current `TEST_COLS=512` that is only 4 tuples per page, with 32-byte minipages (half a cache line), so PAX ends up behaving almost like NSM. Use a page size of at least `N × 64` bytes. A `TEST_PAGE_SIZE` Makefile variable would fix this but hasn't been added.
- **The `output/q2` graphs** came from a 300K × 16 test run, not from the current Makefile settings (100K × 512).
- **NSM is oddly slow at k = 8** in both queries, slower than at k = 9–16. It's still unexplained; the counters could be used to investigate it.
- **WSL2 adds noise** (NSM's spread is especially wide). Pin runs with `taskset -c 2`, and repeat them before drawing conclusions.
- **Not tried yet:** `-O3`, or several running totals in `sum`, so DSM becomes limited by memory rather than the CPU; rows large enough that one column exceeds L3 (≥ 2M rows at 8 B); staggering DSM column start addresses to confirm finding 2.
