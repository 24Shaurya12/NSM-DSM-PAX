# Proposed commits

Nothing has been committed. Everything below is in the working tree on branch `shaurya`
(and in untracked files), ready to stage. The commits are listed in an order that applies
cleanly. Each one says what it contains and how to stage it. Pick what you want; later
commits do not depend on the optional ones (6 and 7).

Before you start: `git status` shows the full state. To see any commit's content first, use
`git diff -- <paths>` for tracked files or `git status --short <paths>` for new ones.

---

## 1. Your pre-session work: DSM column encodings

**Message:** `Add DSM column encodings (bitpack, for, dict, rle, delta) and compare_encodings`

This is exactly the uncommitted work that was in the tree when I started, before any of my
changes: `src/encodings/*`, `src/encoded_dsm.*`, `scripts/compare_encodings.py`, and your
edits to 13 tracked files (26 files, +1452/−58). I saved it as a patch so it can be committed
under your name as it was:

```sh
git apply --cached proposed-commits/01-your-encodings-work.patch   # stages it; working tree untouched
git commit -m "Add DSM column encodings (bitpack, for, dict, rle, delta) and compare_encodings"
```

`git apply --cached --check` passes on it against `HEAD` (checked in a temporary index).
After this commit, `git diff` shows only my changes on top of yours.

## 2. Ignore build, report and scratch outputs

**Message:** `Ignore build-dev, quick results and LaTeX intermediates`

```sh
git add .gitignore
```

Adds `build-dev/`, `results_quick/`, `*.csv.tmp` and LaTeX intermediates (`report/*.aux`, …).
`report/report.pdf` itself is not ignored.

## 3. Engine: chunks/kernels, strategies, predicates, machine-aware knobs, SIMD, encodings

**Message:** `Rework scan engine: shared kernels, five strategies, predicates, machine-derived tuning`

```sh
git add src Makefile scripts/perf_metrics.py scripts/plot.py scripts/summarize.py scripts/compare_encodings.py
```

The files are too intertwined to split cleanly, so this is one commit. It contains:

- **Execution framework.** `chunks.h` (`RowChunk`/`ColumnChunk`) and `kernels.h`: one set of
  kernels for NSM, DSM, PAX pages and decoded vectors, specialised on the number of output
  attributes and predicates. This took DSM early at k=2 from 54 to 12 instructions per row.
- **Strategies.** `early`, `early-pipe`, `late`, `late-branch`, `auto` (`query.h/.cpp`,
  `queries/*`). Projection, SUM/COUNT/AVG, full-tuple reconstruction.
- **Predicates.** `predicate.h/.cpp`: conjunctions of `=`, `<`, `<=`, `>`, `>=`, ranges
  `a..b` on any columns (`--where`), plus the `--sel` predicate.
- **Machine model.** `machine.h/.cpp`: reads the cache hierarchy from sysfs. Rules for
  `--batch auto` (¼ of L2), `--dsm-offset auto` (17 lines), and the `auto` threshold
  (`late_threshold`, φ = 0.55, σ = 2).
- **Deterministic placement.** `aligned_buffer.h`: buffers of 64 KB or more get their own
  page-aligned `mmap`. With `malloc`, glibc's dynamic mmap threshold sometimes placed the DSM
  columns on the heap, staggered across L1 sets by accident. The same `SELECT *` then took
  21 ms or 63 ms depending on whether an earlier 32 MB temporary had been freed.
- **Knobs.** `--hugepages`, `--stage`/`--nt-stores`, `--simd`, `--pax-pad auto` (odd-line
  minipage stride where minipages alias in L1), and `--auto-measure`. That last one makes
  `auto` time every span of at least 1024 rows and keep the cheaper strategy per selectivity
  range, instead of relying only on the fitted threshold. Samples are clipped at twice the
  running estimate, so a vector hit by an interrupt (about 1 in 250) cannot flip the choice.
- **Auto spans.** `auto` (both modes) now decides once per span of at least 1024 rows rather
  than per PAX page: a 128-row page at 1% has 0-4 qualifying rows, too few to estimate the
  selectivity from, and deciding per page cost about 1 cycle per row.
- **AVX2.** Selection vector (256-entry LUT), bit-unpacking for widths 1–25 (`encodings/bits.*`),
  and fused SUM on bitpack/for/rle (`EncodedColumn::sum`).
- **Encoded predicate pushdown.** `select`/`refine`/`gather` on packed data, FOR zone maps,
  and dictionary code ranges.
- **Tooling.** `perf_counters.*` (counter sets core/mem/stall, ≤ 5 events for WSL2),
  `tools/membench.cpp`, and a correctness check of every configuration against a naive
  reference.
- **Makefile.** `make test` now defaults to 1M × 16 (it was 100k × 512). New targets:
  `make experiments [only=…]`, `make figures`, `make report`. `make test` passes `sel=`,
  `strategy=`, `page=`, `batch=` and `knobs=`.
- **Scripts.** `summarize.py`/`plot.py` group by strategy and selectivity, and
  `compare_encodings.py` ignores WHERE rows.
- **Encodings, 2026-11-15** (final numbers from the re-run, `results/suite_enc.log`, AVX2,
  4M rows, against the best of early/late on plain DSM in the same process):
  - **RLE run hint**: `RleColumn::first_run` checks the run the previous call stopped in
    before binary-searching all run ends (the search was 60% of RLE SUM's time).
  - **Late SUM without WHERE skips vectors** (`SumQuery::run`): one `EncodedColumn::sum`
    call per column.
  - **RLE `sum`** = the two cut runs plus a loop over whole runs (length = `ends[r] -
    ends[r-1]`, no `min` per run), which gcc auto-vectorizes; `--simd` uses a hand-written
    `vpmuludq` version when values and run lengths fit 32 bits. RLE SUM on runs: 1.9 ms
    (4.3x plain) -> **0.10 ms (87x)**. The 7.6 MB of runs stay in L3 between repetitions;
    from DRAM it would be ~0.3 ms.
  - **Delta SUM from the deltas** (`DeltaColumn::sum`, `bits::weighted_zigzag_sum`): a
    block sums to m*f + sum_k d_k (m - k); the AVX2 kernel keeps two running sums per lane
    (no multiplication). Delta SUM 24.5 -> **6.0-6.4 ms, 1.25-1.31x plain** (was 0.30x).
  - **New encoding `dictb`** (`src/encodings/dictb.cpp`): dict with byte-aligned codes (1,
    2 or 4 bytes), compared in place with AVX2 (`min_epu` + `cmpeq` + `movemask`); empty
    32-code groups are skipped only when the previous vector's selectivity was below 0.2%
    (the branch mispredicts above). Zipf data (2-byte codes, 4.0x compression instead of
    4.5x), `SUM(c0) WHERE c0 < x`: **0.24 / 0.57 / 0.66 ms at 0.05 / 1 / 5%, i.e. 5.4x /
    2.2x / 1.9x faster than plain** (packed dict: 0.8x). SUM of 8 columns: dict 18.4,
    dictb 9.3, plain 8.2 ms.
  - Bitpack/for late SUM with AVX2: **1.56-1.60x plain** on uniform data (was 1.4-1.5x).
  - Tried and dropped: dict SUM by counting codes (32-bit x4 and 16-bit L1-resident
    counts), 10-25% *slower* than one lookup per row.
  - Also: `dictb` in `--encoding all`, README, Makefile comment, plot colours.
- **Late vs early, 2026-11-15:** only a comment changed in code. `src/machine.h`
  (`late_threshold`) now quotes the threshold's current predictions (2.5% / 7.8%, measured
  2.4% / 7.4%; it said 2.1% / 7.7%). It also says φ and σ are a joint fit, not separately
  measured costs. A fused gather (one pass over the selection vector for all columns) was
  tried and reverted (see the state section below); `src/kernels.h` is unchanged.

Check: `make && make test` (every configuration is checked against the reference before it
is timed; a mismatch exits 1).

## 4. README

**Message:** `Document layouts, strategies, knobs, experiments and the report in README`

```sh
git add README.md
```

## 5. Experiment suite and figure generation

**Message:** `Add experiment suite and figure/table generation for the report`

(Includes the 2026-11-15 additions: `dictb` in the encoding experiments, the new `enc_dict`
experiment and its figure/table, and wider y-axes for rle's 87x.)

```sh
git add scripts/experiments.py scripts/figures.py
```

`scripts/experiments.py --list` lists the 18 experiments. `make experiments` runs them all
(about 70 minutes on this laptop) and resumes where it stopped. `make figures` regenerates every
figure and table from `results/`.

## 6. (optional) Raw results

**Message:** `Add raw benchmark results behind the report`

```sh
git add results
```

About 12 MB of CSVs plus logs (each log starts with the exact command). `results/late_breakdown/` (new 2026-11-15) holds the hand-run timings
behind the report's "Where late's time goes". Its `commands.log` lists the command for each
CSV; it's not part of `scripts/experiments.py`. Commit this if you
want the report to be reproducible without re-running 70 minutes of experiments. Otherwise,
add `results/` to `.gitignore`.

## 7. (optional) Report

**Message:** `Add LaTeX report`

```sh
git add report
```

`report/*.tex`, the generated `report/figures/` and `report/generated/` (both rebuilt by
`make figures`), and `report/report.pdf` (rebuilt by `make report`). If you'd rather not
commit generated files, commit only `report/*.tex` and ignore the rest.

---

## Not proposed

- `SUMMARY.md` and `questions` are your untracked notes; I didn't touch them.
  `SUMMARY.md` is now out of date. Its note about `build/` is stale (`build/` is ignored and
  untracked), and the NSM k=8 anomaly it describes does not show up in the current data
  (it was most likely generic-kernel codegen, which the specialised kernels replaced).
- `proposed-commits/` only holds the patch for commit 1. Delete it after committing.

## Answers to `questions`

1. **SQL parser?** Not needed, and I didn't add one. A query is a class with a `run()` per
   layout. `--where 'c1<500&c2=7&c3=10..20'` covers the conjunctive equality/range predicates
   the project asks for.
2. **Longer runs.** Use `--iters N --rounds R` (rounds interleave all configurations, so a
   slow period on the host hits all of them), `--min-time-ms T` for tables that fit in cache,
   and `taskset -c 2`. `scripts/experiments.py` uses `--iters 5 --rounds 3 --warmup 1`.
3. **L1 or L3?** Both, and DRAM. The default table (4M × 16 × 8 B = 512 MB) is 32× the L3, so
   the main experiments run from DRAM, as the project asks (> LLC). The `workingset`
   experiment sweeps the size from 16 KB (L1) to 512 MB. `simd` and `selectivity_cache`
   repeat the key comparisons with the data in L2 and L3. The crossovers move by an order
   of magnitude between them.

## State of the work (2026-11-20, report restructured)

- `report/` rewritten from 31 to 19 pages, following the final deck's flow: Introduction,
  Design, Methodology, Results (layouts), Analysis (counting cache lines), Early or late
  materialization, Encodings, Tuning for this machine, Tried and dropped, Conclusions. Files:
  `report.tex`, `results.tex`, `analysis.tex`, `materialization.tex` (new), `encodings.tex`
  (new), `tuning.tex`, `tried.tex` (new), `conclusions.tex`; the old split files
  (`results_more.tex`, `analysis_enc.tex`, `tuning_auto.tex`, ...) are removed.
- Added from the deck: late's per-line and first-pass costs with the lines-per-8,000-rows
  table; in cache, bytes vs instructions and the SIMD selection vector; the cache-aware auto
  threshold (`results/auto_cache_aware/`); decode-first vs compressed table; the
  max(compute, load) budget table; vector size by workload (`results/batch_demo/`); the
  stage/streaming-store table; huge pages for late vs early. Measuring auto, the fused gather,
  L1-sized vectors, 64-B padding, memcpy staging, code counting and dictb group skipping are
  in "Tried and dropped".
- Fixed: the vector-size claim (now 1.2x, 11.9 -> 9.9 ms, with the 128 rows an L1 rule gives).
- `make report` / latexmk: 19 pages, no warnings, no undefined references. The previous
  version is not in the repo; commit 7 should take the new files.

## State of the work (2026-11-19, cache-aware auto threshold)

- New, opt-in: `--auto-cache` (plus `--auto-cache-phi`, `--auto-cache-sigma`, defaults
  1.0 and 2). When the table's footprint fits in L3, auto's threshold uses the in-cache
  constants instead of the DRAM ones. Files: `src/query.h`, `src/query.cpp`
  (`auto_reset` now takes the footprint), `src/main.cpp`, and the `auto_reset` calls in
  `src/queries/sum.cpp` and `src/queries/projection.cpp`. Belongs in commit 3.
- The constants were fitted on `results/auto/auto_cache_*` (grid search, geomean regret),
  then checked in a fresh run: `results/auto_cache_aware/` (`run.sh`, `summarize.py`).
  The correctness check passed.
- Result in L3 (geomean / worst vs the better fixed strategy): DRAM constants 1.12 / 2.58,
  in-cache constants 1.08 / 1.55, measuring 1.07 / 1.16. Fixes `SELECT *` (2.28-2.58x ->
  1.13-1.30x); SUM stays wrong (early SUM vectorizes in cache, early SELECT doesn't, and
  one pair of constants can't fit both). From DRAM nothing changes (512 MB > L3).
- In the report (section 6.4); not in the README.

## State of the work (2026-11-15, deck restructured)

- `presentation/deck_outline.md` was rewritten for a 20-minute talk: 17 main slides in four
  parts (encodings 3-7, layouts and materialization 8-11, tuning 12-15, result and
  conclusions 16-17), plus backups A-H. Nothing was dropped: detail moved into speaker notes
  or backups. The numbers are the same ones as before. The previous version is in the
  session scratchpad (`deck_outline_before_restructure.md`), not in the repo. The slide
  numbers in the sections below refer to that earlier version.

## State of the work (2026-11-15, vector size)

- New tool `src/tools/batchbench.cpp`, with a `Makefile` target (`build/batchbench`, built by
  `make`). It is a standalone pipeline benchmark: vector size against work per value and a
  fixed cost per vector. Its runs are in `results/batch_demo/` (each file starts with its
  command). Add the tool and Makefile change to commit 3, and the results to commit 6.
- Findings:
  - Random reads inside a vector, and 8 light passes over one buffer, are fastest with
    L1-sized vectors (1.3-1.8x and 1.2-1.3x over L2 sizes).
  - Random reads plus a 2 µs fixed cost per vector are fastest with L2-sized vectors (1.27x over
    L1, 1.37x over L3; `probe_4_2.txt`). Our own scans also favour L2, but only 5% over L3.
  - A 20 µs fixed cost per vector makes L3-sized vectors fastest: 2.5x over the 128 KB the L2
    rule would pick.
- Deck: new Part 7, slides 19-19c (layouts: line model, projectivity, selectivity, width,
  working set), and Part 8, slide 20 (tuned vs untuned, 1.46x).
- Deck: new Part 6, slides 17 (PAX short minipages vs the prefetcher, `results/pax/`) and 18
  (huge pages, `results/dsm_knobs/`).
- Deck: new Part 5, slides 16 and 16b (write-allocate, write-combining buffers, the stage;
  numbers from `results/output_knobs/` and `results/machine/`).
- Deck: new Part 4, slide 15 (L1 aliasing, shown on PAX, with DSM in the speaker notes;
  numbers from `results/pax/` and `results/aliasing/`).
- Deck: new Part 3, slides 14 and 14b. Slide 14 uses the corrected decoding gain: 1.2x, 128 ->
  1024 rows, 11.9 -> 9.9 ms.
- Not yet fixed: the report still says "decoding 1.4x faster than the L1-based rule" and "the
  L1 rule gave 64" (`tuning.tex`: table and `sec:tuning:batch`). The 1.4x compares 64-row
  vectors, but the L1 rule picks 128 rows for SUM (1.2x), and projection doesn't gain at all.

## State of the work (2026-11-15, late vs early)

- Measured where late materialization's time goes (DSM projection from DRAM, k = 1..16, 0.01-10%;
  raw data in `results/late_breakdown/`):
  - The scalar first pass takes 2.05 ms per 4M rows, 1.21 ms with the AVX2 selection vector.
    So it is CPU-bound and costs ~0.7 scans more than reading c0.
  - A gathered line costs 2.2-3.2 streamed lines.
  - So σ = 2, φ = 0.55 are a joint fit, not the two costs.
- The fused gather was 0.97-1.11× below 5% and 0.7-0.9× above 7%. It moved the k=16 crossover
  only from ~7% to 8-9%, so it was dropped.
- Report changes (commit 7):
  - `analysis.tex`: the fixed-cost item is corrected (it said "two extra column scans"), and a new
    paragraph "Where late's time goes" covers the breakdown and the fused-gather result.
  - `tuning.tex`: σ is described as late's fixed cost, with a note on why the fitted values
    differ from the measured ones; the "selection vector is σ" sentence is corrected; the
    in-cache SUM explanation is rewritten (masked SUM vectorizes, projection is a compaction).
  - `results.tex`: the PAX early bullet now names the 16 KB pages and the tuned result.
  - `make report`: 31 pages, no overfull boxes, no undefined references.
- README (commit 4): the `--auto-phi/--auto-sigma` row says the constants are a joint fit.
- `presentation/deck_outline.md`: slide 13 is replaced by 13 (what each strategy reads), 13b
  (why late loses), 13c (the model and its fitted constants) and 13d (in cache). New
  Backup D covers the fused gather. At your request, the deck now covers only encodings and
  early vs late: slide 12 (layouts) and slides 14, 15, 17 and 18 (tuning table, L1 aliasing,
  tuned vs untuned, conclusions) are removed. Slide 16 (choosing early or late automatically)
  is kept as 13e, with speaker notes.

## State of the work (2026-11-15, after the encoding re-run)

- The encoding experiments (`encodings`, `enc_selectivity`, `enc_workingset`, and the new
  `enc_dict`) were re-run with the final code on 2026-11-15 (`results/suite_enc.log`, every
  run's correctness check passed). The old encoding CSVs are not kept in the repo.
- Every encoding number in the report (`results_enc*.tex`, `analysis_enc.tex`, the tuning
  table and Decoding paragraph, conclusions, abstract), in `presentation/deck_outline.md`
  and in this file was checked against those results. `make report`: 30 pages, no overfull
  boxes, no undefined references.
- `presentation/deck_outline.md` (outline for the slides) is new and not in any commit
  above; add it to commit 7 or its own commit.

### Earlier state (2026-11-14, after the last full run)

- `results/` comes from one run of the whole suite from scratch with deterministic placement
  (`results/suite.log`, `exit 0`, no FAIL lines), except `auto` and `tuned`, which were
  re-run after the last fix to the auto strategy (`results/suite_auto.log`,
  `results/suite_tuned.log`). No other experiment runs the auto strategy.
- Every number in `report/*.tex` was checked against these results, and every figure and
  generated table was regenerated from them. `make report` builds `report/report.pdf`
  (29 pages) with no undefined references and no overfull boxes.
- The cross-layout correctness check passes for every strategy including `auto` (with and
  without `--auto-measure`), all layouts, PAX padding 0/auto, all encodings, and one- and
  two-predicate WHERE clauses. `make test` runs.
- Last fixes to `auto --auto-measure` (in commit 3), found because it ran 10-30% slower than
  the better fixed strategy:
  1. samples clipped at twice the running estimate (interrupted vectors flipped the choice);
  2. one decision per span of at least 1024 rows instead of per PAX page;
  3. measured mode also compares early and late without a WHERE clause (late SUM over 16
     DSM columns was 1.2x slower than early).
  Now measured auto is within 5% of the better fixed strategy from DRAM (geometric mean)
  and within 10% in L3, and its worst point is 1.5x where the threshold model's is 2.5x.
