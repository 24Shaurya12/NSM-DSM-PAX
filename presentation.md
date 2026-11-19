# Presentation outline: NSM vs DSM vs PAX, and a layout advisor

**Course:** COL868 · **Talk length:** ~20–25 min (about 29 slides + backup) · **Audience:** instructor and classmates who know the three layouts but not our engine.

**Storyline in one sentence:** we built a scan engine for the three storage layouts and measured where each one wins and why. We then turned those mechanisms into a calibrated cost model that, given a machine, a workload and a data distribution, picks the layout and its parameters, and we checked its picks against 10,000 measurements.

Notation in this outline: **Visual** = what goes on the slide; **Say** = speaker notes. Advisor numbers come from `results/validation/laptop_v1/` (fast mode: 500 workloads, calibration up to 512 MB).

---

## Part 0 — Setting the scene (3 slides)

### 1. Title
- *Row, Column or Both? Measuring NSM, DSM and PAX, and Predicting Which to Use*
- Team names, COL868, date

### 2. The question
- The same table can be stored row-wise (NSM), column-wise (DSM) or column-wise inside pages (PAX).
- The right choice depends on the query (columns, selectivity, lookups), the data, and the **machine** (caches, bandwidth, prefetchers).
- Two goals:
  1. **Measure and explain** the crossovers with hardware counters.
  2. **Predict**: given machine + workload + data, recommend the layout and its parameters, and prove the recommendation is right.
- **Visual:** the three inputs → "advisor" → layout + parameters + predicted time.

### 3. The three layouts
- **NSM:** rows back to back. **DSM:** one array per column. **PAX:** fixed-size pages, with one minipage per column inside each page.
- PAX has a parameter, the page size. Its minipage holds `page / (cols × 8)` values per column.
- **Visual:** a 4-row × 4-column table drawn in each layout, with cache lines (64 B) and OS pages (4 KB) overlaid.

---

## Part 1 — The engine and how we measured (3 slides)

### 4. Engine design
- C++17, in-memory `int64` tables. Operators:
  - scan
  - conjunctive equality/range predicates
  - SUM/COUNT
  - tuple reconstruction (materialize)
  - point lookups
- Execution options are separate from storage: DSM batch size, branchy vs branch-free filters, predicate order.
- Extensible through registries: layouts, operators, data distributions, row-id distributions.
- Every result is checked against a reference executor; the test suite has 53k checks.
- **Visual:** block diagram (QuerySpec → Layout → TupleSink; perf counters on the side).

### 5. Methodology
- Intel i7-1360P (hybrid): pinned to one P-core, on AC power, `-O2` with no extra compiler tricks (scalar code, as written).
- **Cold** measurements: the table is flushed from all caches before every query; 5 samples, median.
- Counters through `perf_event_open`: cycles, instructions, branch misses, LLC misses, dTLB misses, **DRAM bytes** read from the memory controller.
- **Say:** explain why cold mode (a repeated query on a 256 MB table would otherwise be partly served from the 18 MB L3).

### 6. Ground-truth dataset
- 3 data sets: 16 columns and 64 columns with uniform data, and 16 columns with a sorted `c0`; 256 MB each.
- 12 storage configurations: NSM, DSM, and PAX with page sizes from 1 KB to 16 MB.
- 134 query templates per width:
  - scans
  - one-predicate filters at 0.1%–100% selectivity
  - two-predicate filters in both orders
  - lookups (10³–10⁵ ids, uniform/sorted)
- × all execution options = **10,368 measured points, 51,840 samples, 100 min**.
- Repeatability: a 10% re-run has a **median deviation of 4.3%**, but only 54% of points within ±5% (90th percentile 20%, mostly ~1 ms points). That is the noise floor of a laptop.
- **Visual:** a small table of dimensions × values.

---

## Part 2 — What we found (6 slides)

### 7. Bytes moved explain most of it
- Scans are bound by DRAM bandwidth: a single core sustains **~17 GB/s** (11.5–26).
- Unfiltered SUM of one column (16-column table): **DSM 8 B/row, 0.49 ns/row** vs **NSM 140 B/row, 6.4 ns/row** (13×).
  - NSM reads ≈ the whole 128 B row even for one 8-byte column: line + adjacent-line prefetch = an effective 128 B transfer unit.
- With all columns, the layouts converge (DSM 7.6, NSM 7.8, PAX-16K 8.2 ns/row on 16 columns).
- **Visual:** bar chart of DRAM bytes/row and ns/row for k = 1, 4, all.

### 8. Selectivity, branches and the 50% hump
- Branch mispredictions per evaluated predicate ≈ **2·f·(1−f)** on random data: measured 0.0124 at 1% (model 0.0198), up to 0.5 at 50%.
- On sorted data the branch becomes predictable: < 0.0006 misses/row.
- Branch-free filtering helps DSM most at 50% (**1.35×**), but is **slower at low selectivity** (0.89× at 0.1%).
  - So branch-free is an execution choice, not a default.
- **Visual:** misses/row vs selectivity (uniform vs sorted), plus the speed-up curve per layout.

### 9. PAX page size: the prefetcher matters
- SUM(c0), 16 columns: **1 KB pages read 65 B/row** from DRAM for 8 useful bytes. 256 KB+ pages read ~9.6 B/row ≈ DSM.
- Cause: short minipage runs (64 B at 1 KB pages) trigger the hardware prefetchers, which fetch the next lines (other columns' minipages).
- The time follows the bytes: 2.57 ns/row (1 KB) → 0.57 (256 KB) vs DSM 0.49.
- The common default of 16 KB is 2.5× slower than DSM for narrow scans. PAX ≈ DSM needs pages of at least ~256 KB.
- **Visual:** ns/row and DRAM B/row vs page size (log x), with a DSM reference line.

### 10. Point lookups: NSM's home ground
- Full-tuple lookup, 16 columns: **NSM 25–45 ns**, PAX ~107 ns, **DSM 190–230 ns**.
  - DSM pays one cache miss and one TLB miss per column, while NSM pays 2 lines.
- One-column lookups: DSM and PAX are as fast as or faster than NSM (fewer bytes per probe).
- Sorted ids help every layout (sequential-ish access): DSM one-column lookups go from 9 to 3.4 ns on 64 columns.
- **Visual:** grouped bars, ns/lookup by layout × projected columns.

### 11. Who wins overall
- Winners per query family (best execution options per layout):
  - DSM wins most filters and reconstructions.
  - NSM wins 12 of 18 lookup templates per width.
  - PAX wins some narrow sums on sorted data and scans of 64-column tables (large pages).
- **30% of queries have a runner-up within 5%**, and 50% within 10%: many decisions are close calls.
- **Visual:** stacked bars of winner counts per data set × family (from `results/ground_truth/laptop_v1/report.md`).

### 12. Execution options matter as much as the layout
- For DSM, worst ÷ best over (batch, filter mode, predicate order): **median 1.44×, max 7.9×**.
- Predicate order (selective last ÷ selective first): median 1.5–1.9×, up to **3.1×** for PAX.
- The best DSM batch varies by query: 256, 1K, 16K and 128K rows all win somewhere.
- ⇒ The advisor must choose execution options per query, not just the layout.
- **Visual:** distribution (box plot) of worst/best ratios per layout.

---

## Part 2b — Under the hood: hardware insights (7 slides)

Every insight was found in the counters and confirmed with a targeted experiment
(`results/insights/`, `tools/assoc_probe.cpp`, calibration kernels).

### 12a. Divider: what the hardware is really doing (the five insights, numbered)

### 12b. DSM rebuilds thrash the 12-way L1 past ~10 columns
- L1 = 48 KB = 64 sets × 12 ways; the set is chosen by address bits 6–11, i.e. the offset
  inside a 4-KB page.
- Every DSM column array starts at the same page offset, so value r of every column maps to
  one set.
- Probe (k arrays read tuple-at-a-time): at 32 columns, aligned arrays take **24.5 L1 misses
  per row** vs **4.0** when each array is shifted by 64 B (6×), and run **1.7× slower**.
  Thrashing starts at ~10–11 columns, before 12, because prefetched lines share the ways.
- In the engine at 32 columns: DSM 20.4, PAX (64-KB pages, minipages 2 KB apart) 12.2,
  NSM 0.7 L1 misses per row.
- Fix: offset column c by c × 64 B.
- **Visual:** L1 misses/row vs k, aligned vs staggered, with the 12-way line.

### 12c. The prefetcher fills the 4-KB page, then stops
- A 1-KB run at the start of a page pulls in ~41 more lines (48 were left); the same run
  2 KB into the page pulls in ~21 (16 were left). 128-B runs pull in ~12.
- It never crosses the 4-KB page boundary; a run that ends exactly at the boundary pulls
  20–50 lines of the next page.
- Consequence: small PAX pages pay a per-OS-page tax (PAX 1 KB: 65 B/row for 8 useful bytes).
- **Visual:** extra lines by run length, page start vs mid-page, against lines left.

### 12d. Touch two lines per row, pay for the whole row
- NSM with 512-B rows: 1 column → 162 B/row, 8.9 ns; 16 contiguous columns (2 lines) →
  512 B/row, 22.9 ns; all 64 → 518 B/row, 32.1 ns.
- One line per row looks like a stride (only the 128-B pair arrives); two consecutive lines
  look like a stream, and the streamer fetches the whole row.
- 25 % of the columns cost 99 % of the bytes.
- **Visual:** DRAM bytes per row and ns per row for 1 / 16 / 64 columns.

### 12e. Mispredicts cost 26 cycles, or almost nothing
- Cycles lost per mispredict (Δcycles ÷ Δmisses, branchy vs branch-free):
  - DSM 26.9 / 26.3 and PAX 22.7 / 26.7 (at 25 % / 50 % pass) — the full calibrated penalty
  - NSM 0.6 / 10.5: it is waiting for DRAM anyway
- Observation: NSM's branch mispredicts at the optimum min(f, 1−f); the tight DSM/PAX loops
  1.2–1.8× more. Hypothesis: the global history holds only the branch's own random outcomes.
- **Visual:** cycles per mispredict by layout; mispredictions ÷ min(f, 1−f) vs pass rate.

### 12f. For lookups, the TLB separates PAX from DSM
- TLB misses per full-tuple lookup (64 columns): 1 for PAX pages ≤ 4 KB, 11 at 64 KB, 62 from
  256 KB on (DSM 62). Time follows: 194 ns → 949 ns (DSM 996, NSM 92).
- Calibrated TLB reach: 96 + 2,048 entries = 8 MB, less than the 18-MB L3. A walk costs
  ~15 ns with cached page tables, ~100 ns without.
- Walks cut memory-level parallelism: 15 misses in flight with 2-MB pages, 12 with 4-KB pages;
  DSM's 64-column lookups reach only ~7.
- **Visual:** TLB misses and ns per lookup vs PAX page size.

### 12g. What the hardware means for the layout choice (table: mechanism → consequence)
- Includes a methodology lesson: two ground-truth anomalies (PAX 4-MB pages slower; extra
  DRAM bytes for branchy DSM) did not reproduce in a controlled re-run. ~1-ms cold runs vary
  by up to 30–40 %, and the DRAM counters are system-wide.

---

## Part 3 — The layout advisor (5 slides)

### 13. Problem statement and design
- Inputs:
  - `hw.json` (calibrated machine)
  - `data.yaml` (rows, columns, distributions)
  - `workload.yaml` (weighted queries)
  - candidate space
- Output: storage (layout + page size) **once per table**, best execution options **per query**, predicted time, runner-up and margin, where the time goes, and sensitivity.
- Exhaustive search (a few hundred predictions, < 1 s). Pluggable models, search strategies, selectivity estimators.
- **Visual:** architecture figure (inputs → estimator → search → cost model → report).

### 14. Cost model: Manegold et al. (VLDB 2002) + modern hardware
- Each (layout, operator) is described as **memory access patterns**: sequential traversal, sparse traversal after a selection, random access, re-used buffers. Each pattern gives misses per cache level and per TLB level.
- T_mem = Σ_levels (sequential misses × l^s + random misses × l^r); T = T_mem + T_cpu.
- Our extensions, each motivated by a measurement in Part 2:
  - effective transfer unit **B_eff = 128 B**
  - **prefetch over-fetch** table
  - **memory-level parallelism**
  - **branch misses** 2f(1−f)
  - per-batch / per-page overhead
  - overlap of memory and CPU time
- Model ladder: **v0** bytes/bandwidth → **v1** Manegold as published → **v2** + modern memory → **v3** + modern CPU.
- **Visual:** one PAX page with the pattern annotations (which lines are touched at 1% selectivity).

### 15. Calibration: the machine as numbers
- A micro-benchmark suite (`build/calibrate`, ~2 min) measures, per level:
  - capacity, bandwidth and latency
  - TLB reach and walk cost
  - effective transfer unit and over-fetch
  - memory-level parallelism
  - CPU cycles per engine kernel and branch-miss cost
- Nothing about the machine is hard-coded, so the same code calibrates any other machine (e.g. an AMD EPYC VM).
- Calibrated laptop (`configs/hw/laptop.json`):

  | | L1 (48 KB) | L2 (1.25 MB) | L3 (18 MB) | DRAM |
  |---|---|---|---|---|
  | sequential bandwidth | 82 GB/s | 46 GB/s | 40 GB/s | **17.8 GB/s** |
  | dependent-load latency | 1.3 ns | 4.0 ns | 14 ns | **109 ns** |

  - Effective transfer unit **B_eff = 145 B** (a line plus its neighbour)
  - Memory-level parallelism **15** for independent misses
  - Page walk ≈ 100 ns
  - Branch miss **26 cycles**
  - Clock 3.79 GHz
- Sanity check: bandwidth 17.8 GB/s and branch miss 26 cycles match what the scans and filters measured.
- **Visual:** latency vs region-size curve with the cache-level steps.

### 16. Accuracy (V1)
- Predicted vs measured time on held-out points (fit on 30% of queries, test on the other 70%).
- Model ladder (median / 90th-percentile |error|, held-out points):

  | | v0 bytes÷BW | v1 Manegold 2002 | v2 + modern memory | v3 + modern CPU |
  |---|---|---|---|---|
  | median | 59% | 77% | 26% | **16%** |
  | p90 | 89% | 728% | 67% | **47%** |
  | within ±15% | 2% | 14% | 32% | **48%** |

- **Say:** the 2002 model applied as published is worse than plain "bytes ÷ bandwidth". It knows nothing about prefetching, overlapping misses or hidden page walks. Each modern mechanism removes a large chunk of the error.
- The counters revealed one more mechanism. The model predicted one page walk per 4 KB page in sequential scans; measured walks were 10–35× fewer (next-page prefetch). Random lookups matched the model exactly (DSM 3.55 vs 3.58 walks per lookup).
- Per data set: 16 columns uniform **13%**, 16 columns sorted 15%, 64 columns 20%. Groups over 30%: only unfiltered reconstruction scans (98 of 9,300 points).
- **Honest status:** the median is on target (16% vs 15%); the tail (p90 47%) is not.
- **Visual:** log-log scatter of predicted vs measured (coloured by layout, ±15% band).

### 17. Does it pick correctly? (V2, V3)
- 500 random workload mixes (2–6 queries, frequency weights 1–1000), evaluated against the measurements.
- Results (regret = measured time of the choice ÷ best measured):

  | chooser | top-1 | median regret | p90 | worst |
  |---|---|---|---|---|
  | **advisor** (picks storage + execution options) | 73% | 1.07 | 1.24 | **1.54** |
  | always DSM (best options, oracle) | 85% | 1.00 | 1.12 | 8.35 |
  | DSM unless lookups > 25% | 80% | 1.00 | 1.52 | 10.7 |
  | always PAX-16K | 2% | 1.85 | 2.81 | 6.51 |
  | always NSM | 10% | 3.48 | 8.06 | 11.8 |

- **Say:** on this machine DSM is a very strong default, and with oracle execution options it wins the median. But every fixed rule has a catastrophic tail (8–12×), and the advisor never loses more than 1.54×. Its storage choice alone has median regret 1.00; most of its 7% median regret comes from execution-option choices.
- Baselines compared:
  - always-NSM / always-DSM / always-PAX-16K
  - "DSM unless lookups > 25%"
  - best fixed storage in hindsight
- Wrong decisions are mostly **DSM vs PAX with 1 MB pages**: two layouts that read nearly the same bytes, so the decision is a close call.
- **Visual:** regret CDF, one line per chooser.

---

## Part 4 — Evidence of robustness, and wrap-up (4 slides)

### 18. Decision maps (V5)
- Selectivity × projection grid: predicted winner vs measured winner, per data set and op.
- Exact winner agreement per map:
  - SUM: 92% (64 columns), 68% (16 columns), 45% (sorted)
  - reconstruction: 32–65%
- Most disagreements are near boundaries: for SUM, 30 of 38 have a measured margin under 10%. Worst cell regret 1.14–1.55×.
- **Say:** the maps show the model gets the regions right and misses the exact boundary where layouts are within a few percent.
- **Visual:** the map figures from `plots/v5_map_*.png`.

### 19. Generalisation and robustness (V4, V7)
- V4: fit only on 16-column uniform tables without lookups, test on 64 columns and sorted data. Errors are about the same as with a random split (64 columns 20%, sorted 16%), so the model generalises across width and data distribution.
- V7, without refitting:

  | perturbation | decisions that change |
  |---|---|
  | DRAM bandwidth −20% | 2% |
  | CPU constants +20% | 8% |
  | selectivity ×0.5 | 13% |
  | selectivity ×2 | 10% |

  Median regret stays at 1.07–1.10.

### 20. Demo
- `python3 -m advisor recommend --hw … --data configs/data/w64_8m.yaml --workload wide_mixed.yaml`
- Show the text report: recommendation, runner-up with margin, per-query execution options, where the time goes, sensitivity.
- Real outputs on 8M rows × 64 columns:

  | workload | recommendation | runner-up | worst option |
  |---|---|---|---|
  | lookup-heavy | **NSM** | PAX-64K, 1.27× slower | PAX-1K, 2.6× |
  | scan-heavy | **DSM** | PAX-1M, 1.04× (close call) | PAX-1K, 14.9× |
  | wide mixed | **PAX-4M** | PAX-1M, 1.00× (DSM 1.01×) | — |

  (`results/validation/laptop_v1/recommend/`)

### 21. Lessons, limitations, future work
- Lessons:
  - "Bytes moved" explains scans, but prefetchers, branch prediction and memory-level parallelism decide the close cases.
  - Execution options matter as much as the layout.
  - Many decisions are within 5–10%, so a good advisor must also report its margin.
- Limitations:
  - one machine so far (VM portability pending)
  - single-threaded
  - read-only (no updates)
  - independent predicates
  - laptop measurement noise (~4% median, up to 20%)
- Future work:
  - multithreading (bandwidth per thread)
  - column groups / hybrid layouts
  - zone maps
  - updates
  - second-machine validation (EPYC VM)
  - robust (worst-case) recommendation

### 22. Conclusion
- The three takeaways in one line each, plus the headline advisor result:
  - median prediction error 16%
  - storage choice within 1.54× of the best on every one of 500 random workloads, while every fixed rule loses up to 8–12×

---

## Backup slides
- B1. Full PAX page-size table (both widths, all projections).
- B2. Lookup table (all projections × counts × id distributions).
- B3. Working-set experiment (earlier results: in-cache vs DRAM crossover, `results/working_set/`).
- B4. Where PAX genuinely wins: 64 columns, `SELECT * WHERE c0 < 1`, 1 GB: PAX-4K 6.86 vs DSM 9.32 vs NSM 10.00 ns/row (earlier experiment). Explain why the ground truth shows fewer PAX wins: DSM with the best batch size.
- B5. The Manegold patterns per layout × operator (table from `further_plan.md` §5.3).
- B6. Calibration curves (sequential bandwidth vs size, TLB curve, over-fetch table).
- B7. Repeatability details and noise sources (system-wide DRAM counters, frequency, background load).
- B8. Extensibility: how to add a layout / model / search strategy (one file each).

## Figures to produce (checklist)
1. Layout diagram (slide 3) — drawn by hand.
2. Bytes/row and ns/row bars (slide 7) — from the ground-truth summary.
3. Branch misses and branch-free speed-up vs selectivity (slide 8).
4. PAX page-size curves (slide 9).
5. Lookup bars (slide 10).
6. Winner counts (slide 11) and execution-option ratios (slide 12).
7. Calibration latency curve (slide 15).
8. V1 scatter, V2 regret CDF, V5 maps (slides 16–18) — produced by `scripts/run_validation.sh`.
