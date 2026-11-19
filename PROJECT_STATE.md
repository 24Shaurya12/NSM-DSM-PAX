# Project state (handoff) — 2026-11-14

Read this first when resuming. The detailed implementation plan is `further_plan.md`
(phases, checkpoints CP1–CP8, file layout, extensibility guide, decisions, references).
This file records what is *done*, what was *decided*, what was *measured*, and what is *next*.

---

## 1. Goal

Course project (COL868, `COL868_Project_1_NSM_DSM_PAX.md`): in-memory scan engine with NSM,
DSM, PAX; explain crossovers via bytes moved, bandwidth, caches, reconstruction cost.

**Extended goal (instructor's main interest):** a *layout advisor* — given (1) a machine,
(2) a workload, (3) a data distribution, recommend the storage layout (NSM / DSM / PAX) and its
parameters, predict its speed, and **prove with measurements that it picks correctly**.
Approach: a calibrated, white-box cost model built on Manegold, Boncz & Kersten, *Generic
Database Cost Models for Hierarchical Memory Systems* (VLDB 2002; `1287369.1287387.pdf` in repo),
extended for modern CPUs, plus exhaustive search over candidates, validated against ground truth.

## 2. User decisions and corrections (must follow)

| Topic | Decision |
|---|---|
| Compiler flags | **Keep the original `-std=c++17 -O2 -march=native`.** User rejected `-O3` ("evaluate the normal code as it is"). GCC 11.4 does not auto-vectorize at -O2, so loops are scalar. Do not add optimization flags. |
| Code style | Plain, readable C++ ("normal code"); branchy code is the default; branch-free is an *option*. |
| Design | **Everything extensible** (layouts, operators, exec options, models, search, workloads, distributions, estimators) and **hardware-independent** (nothing CPU-specific hard-coded; machine facts are detected or calibrated into `hw.json`). |
| Scope | Analytical queries (scan/filter/aggregate/reconstruction) **+ point lookups**. No updates/inserts for now (design must allow adding them). |
| Second machine | AMD EPYC 9V74 (Zen 4) **KVM guest, 5 vCPUs, 1 socket, 1 NUMA node** — use for portability (CP8), but the model must not depend on it. VM caveats: counters may be missing, noisy neighbours, vCPU mapping not fixed, cache sizes may be virtualised. |
| Multithreading | Single-threaded now; `ExecOptions::threads` reserved (engine rejects ≠ 1); `bw_dram[threads]` reserved in `hw.json`. |
| Distributions | uniform + sorted + clustered now; any other (Zipf, correlated, custom file) must be pluggable later. |
| Zone maps | Extension only (not first version). |
| Accuracy targets | No instructor requirement; the targets in `further_plan.md` are ours. |
| Deadline | **Unknown** — brief said end of October (passed). Ask the user; decides "minimum convincing version" (plan §11). |
| Working style | When asked for a plan, write the plan only (no implementation). Long runs: start in background, end the turn, wait for the notification (no polling). Don't commit unless asked. |
| sudo | User authorised sudo on the laptop; the password is **not** recorded anywhere — ask the user if sudo is needed again. |

## 3. Machine (laptop) facts

Intel i7-1360P (hybrid; P-cores = CPUs 0–7 = `cpu_core` PMU; E-cores 8–15); benchmarks pinned
to **CPU 2** (`pick_cpu` in `scripts/common.sh` selects it automatically). L1d 48 KB, L2 1.25 MB
(1280K), L3 18 MB; 15 GiB RAM; THP `madvise`; governor powersave with EPP
`balance_performance`. Run on **AC power**, machine idle.
`kernel.perf_event_paranoid=0` is now **persistent** (`/etc/sysctl.d/99-perf.conf`); 0 is
required for the system-wide DRAM counters (uncore `uncore_imc_free_running_{0,1}` data_read /
data_write, 64 B per count). Verified: all 9 counters work (NSM ≈138 B/row DRAM, DSM ≈6 B/row
on 256 MB, k=1).

## 4. Repository state

Commits: `d9f0122` (teammate's framework), `d118802` (selectivity + working set),
`5450d7e` (PAX page size). **Phase 1 work is uncommitted** (user to review/commit).
Untracked user files: `handoff-session.md` (raw transcript export), PDFs, `some_notes.txt` — leave
them alone.

### Engine (C++, `src/`) after Phase 1

| File | Role |
|---|---|
| `query_spec.{h,cpp}` | `enum Op {Sum, Materialize, Lookup}`; `QuerySpec{op, project, where, where_label, lookups, custom_label}`; `parse_query_spec("op=..;cols=..;where=..;label=..;n=..;dist=..;seed=..")`; `label()` comma-free id like `sum\|c0..c3\|c0<5 & c12<50`. Lookup is parsed but **not executable yet** (Phase 1b). |
| `projection.h` | `Projection{cols}` any subset/order; `contiguous_first()`; `label()`; `sum_fields`/`copy_fields`; `parse_projection("all\|0-3\|c0-c3\|0,4,8")` (in query_spec.cpp). |
| `exec_options.h` | `FilterMode{Branchy, Branchless}`; `ExecOptions{dsm_batch=1024, filter, threads=1}`. |
| `layout.h` | `Layout`: `name, params, rows, cols, footprint_bytes, uses_batch_rows, scan_sum(proj), filter_sum(where, proj, opts), filter_materialize(where, proj, opts), flush_cache`. |
| `layout_factory.{h,cpp}` | registry `register_layout(name, builder)`, `parse_layout_spec("pax:page=4K")`, `make_layout`, param checks. Each layout registers itself in its own .cpp (static init). |
| `nsm/dsm/pax.{h,cpp}` | Layouts. NSM tuple-at-a-time; DSM batches of `opts.dsm_batch` rows with selection vector; PAX page-at-a-time selection vector (page param, default 16K). **Contiguous projections use specialised tight loops** (see §6). |
| `predicate.{h,cpp}` | `Predicate{col, lo, hi}` (closed range), `matches` / `matches_nobranch`; `select_positions(where, column_at, n, sel, mode)` (branchy or branch-free; first predicate scans, later ones refine survivors). |
| `distributions.{h,cpp}` | `ColumnGenerator::fill(out, stride, first_row, n)`; registry; built-ins `uniform:lo:hi`, `sorted:lo:hi` (ascending, equal frequency), `clustered:run=N:lo:hi` (runs share one value); `DistSpec` (`--dist default=SPEC`, `--dist cN=SPEC`). |
| `table.{h,cpp}` | row-major logical table, generated in 4096-row blocks, per-column seed `seed*1000003+c` (data differs from pre-Phase-1 runs, same distributions). |
| `reference.{h,cpp}` | reference executor: `reference_stats(table, where)` (count + per-column sums, one pass per WHERE) → `reference_result(stats, op, proj)`; materialize checksum = Σ_j (j+1)·colsum[cols[j]]. |
| `tuple_sink.h` | 1024-tuple reusable output buffer; `next()`, branch-free `slot()`+`commit(keep)`; checksum weights output position j by (j+1). |
| `perf_counters.{h,cpp}` | cycles, instructions, branch_misses, llc_refs, llc_misses, l1d_misses, dtlb_misses (core, P-core PMU on hybrid, fallback generic); dram_read/write_bytes from a **table of memory-controller PMU families** (`kMemPmus`: Intel free-running IMC, Intel IMC data_reads, Intel server cas_count); unavailable → empty CSV field. |
| `util.{h,cpp}` | `split`, `parse_size` (K/M/G), `parse_int`. |
| `main.cpp` | CLI; `build_queries` (legacy `--op×--where×--k` cross product when those flags are used or no `--query`); `run_query` dispatch; `variants()` sweeps `--dsm-batch` only for DSM filter/materialize and `--filter` only for queries with predicates; correctness check of every layout×query×variant before timing; warm (repeat to ≥20 ms) / cold (clflush) timing; CSV. |
| `tests/engine_tests.cpp` | `make test`: 50,914 checks (layouts × ops × projections × predicates × dsm_batch {1,7,1024,1M} × filter modes × shapes {10007×16 uniform/sorted/clustered, 1000×3, 2049×64, 1×16, 7×5}) + parser tests. Mutation-tested (2 injected faults caught). |

CLI additions: `--query`, `--layout nsm,dsm,pax:page=4K`, `--dist`, `--dsm-batch`, `--filter`,
`--threads`. CSV columns now:
`layout,layout_params,rows,cols,row_bytes,data_bytes,footprint_bytes,dist,query,op,project,k,predicate,n_items,page_size,dsm_batch,filter,threads,mode,iteration,reps,latency_ns,matches,selectivity,result,<9 counters>`
(`dsm_batch`=0 / `filter`=none where the option has no effect).

### Scripts (`scripts/`) and results (`results/`)

`common.sh` (`pick_cpu`, `expand_cpu_list`, `write_machine_info` — records virtualization and
available counters, cache sizes, `append_csv`). Sweeps + plots: `run_/plot_working_set`,
`run_/plot_selectivity`, `run_/plot_conjunction`, `run_/plot_pagesize`, `run_/plot_pax_wins`
(all env-var configurable; default `CPU=$(pick_cpu)`). Plot scripts skip counter figures when
counters are absent. Existing results (pre-Phase-1 data generation, -O2) in
`results/{working_set,selectivity,conjunction,pagesize,pax_wins}`.

## 5. CP1 result (Phase 1 done)

- `make test`: 50,914/50,914 pass. All five old sweep/plot scripts run unchanged.
- Regression vs baseline binary (built from `5450d7e` via `git archive` into the scratchpad):
  54 configs (256 MB; 3 layouts; sum/materialize; k 1/4/16; none, c0<1, c0<50), 2 interleaved
  rounds → 49 within ±5%, 4 faster (7–12%), 1 slower by 5.1%; median 1.004.
- Branch-free removes the 50% peak (c0<50: DSM 5.56→2.11, PAX 5.69→2.58, NSM 8.58→6.35 ns/row).
- Not yet done: portability check on the VM (CP4/CP8).

## 6. Lessons / known measurement issues

1. **Hot-loop sensitivity:** a loop-invariant branch inside NSM's row loop cost 15–20%; one
   extra dependent pointer load per value in PAX reconstruction cost ~40%. Fix pattern:
   specialised loop for contiguous projections (`contiguous_first() >= 0`), generic loop
   otherwise. Re-check any hot-loop change against a baseline binary.
2. **Output-buffer placement:** NSM reconstruction varies up to ±20% with the TupleSink
   buffer's offset within a 4 KB page (PAX/DSM ≤6%). A noise floor the model must tolerate.
3. **DSM batch-size confound:** large-page PAX "beating" DSM is a batch-size effect (DSM with
   131072-row batches matches PAX 16M pages: 1.75 vs 1.68 ns/row). Batch size is now an exec
   option; sweep it when comparing.
4. In-cache results are bounded by the scalar -O2 add chain (~30 GB/s); DRAM ≈ 19–21 GB/s/core.
5. Small tables (≤1 MB) let the branch predictor learn the data → use large tables for branch
   effects.
6. DRAM counters are system-wide (noise from other activity, unreliable for tiny tables).
7. At 256 MB, DSM's single column (16 MB) can stay in the 18 MB L3 (k=1 flattered); use 1 GB.

## 7. Measured facts the cost model must reproduce (laptop, -O2)

- time ≈ DRAM bytes ÷ ~20 GB/s when DRAM-bound (DSM 19, PAX-1K 20, NSM 21 GB/s).
- NSM reads the whole row (~125–140 B/row for 128 B rows) whatever k; ≈2 lines (adjacent-line prefetch).
- DSM line-touch probability at pass rate f: p(f) = 1 − (1 − f)^8; by f ≳ 1/8 DSM touches every line
  → NSM/DSM crossover at k=all near 10% selectivity (NSM wins 10–90% by ≤1.2×).
- Branch misses per evaluated predicate ≈ 2·f·(1−f) on random data (0.50 at 50%, 0.16 at 10%);
  ~28 cycles each (DSM), ~26 (PAX); NSM hides them (memory-bound).
- PAX per-row instructions ≈ 4 + 27/T (T = tuples per page); dTLB walks/row = 1/32 for pages
  ≤ 4 KB and halve per doubling above (exactly as Manegold eq. 3/4 with TLB as a cache, B = 4 KB).
- PAX DRAM over-fetch: SUM(c0) reads 48 B/row at 1 KB pages, 29 at 16K, 15 at 64K, 8.4 at 256K
  (Manegold w/o prefetch predicts 16 / 8 / 8 / 8) → needs a calibrated over-fetch term.
- PAX ≈ DSM for pages ≥ 256 KB (minipage ≥ 16 KB). Default 16K is 1.4–2.7× slower than DSM on narrow scans.
- PAX wins only for wide-table full-tuple reconstruction at ~1–3% selectivity with small pages
  (64 cols, 1 GB, `SELECT * WHERE c0<1`: PAX-4K 6.86 vs DSM 9.32 vs NSM 10.00 ns/row; 106 vs 168 vs 157 B/row DRAM).
- Predicate order matters: selective-first vs last up to 8× for DSM (c0<S AND c12<50, S≈0–2%).
- Effective DRAM transfer unit B_eff = 128 B (line + adjacent line).

## 8. Cost model design (summary; details in `further_plan.md` §4–§5)

- Hardware model per Manegold: N cache levels incl. **TLB as a cache** (line = 4 KB page);
  per level C_i, B_i, #_i, sequential bandwidth b^s, random latency l^r; plus modern additions:
  B_eff, `overfetch(run, align)` table, MLP, c_scan/c_pred/c_gather/c_copy, c_branch_miss,
  o_batch/o_col, freq, bw_dram[threads].
- Patterns: s_tra, rs_tra, r_tra, rr_tra, r_acc, nest (Manegold) + new **ss_tra(R, f, u)**
  (sorted sparse access, line-touch prob 1−(1−f)^{B/R.w}); combinators ⊕ (sequential, cache-state
  carry-over) and ⊙ (concurrent, footprint-proportional cache share).
- T_mem = Σ_i (M^s_i·l^s_{i+1} + M^r_i·l^r_{i+1}/mlp); T_cpu from kernels + branch misses + per-batch
  overhead; total either `T_mem + T_cpu` (Manegold) or `max + γ·min` (overlap) — both kept.
- Model ladder: v0 bytes/BW; v1 Manegold as published; v2 + B_eff/over-fetch/MLP; v3 + branch,
  overhead, overlap (+ optional associativity term for DSM set conflicts: 4.9 vs 0.8 L1 misses/row).
- Storage choice (layout, PAX page) is per table; execution choices (DSM batch, filter mode,
  predicate order, later threads) per query. Exhaustive search; report margin/runner-up.

## 9. Status of phases 1b-6 (session of 2026-11-14, evening)

Code written this session (all uncommitted):

- **Lookups (engine):** `src/row_ids.{h,cpp}` (registry: uniform, sorted), `Layout::lookup`
  in NSM/DSM/PAX, `reference_lookup`, driver support (`n_items` = lookups), tests
  (52,978 checks; mutation caught). Lookup experiment script/plot (rest of CP2) not done;
  the ground truth contains lookups (family `lk`).
- **Phase 2 (ground truth):** `advisor/{engine,registry,data_profile,candidates,ground_truth}.py`,
  `advisor/workloads/templates.py`, `configs/{ground_truth,spaces,templates}/default.yaml`,
  `scripts/run_ground_truth.py` (resumable, one process per data set x storage), and
  `scripts/analyze_ground_truth.py` (writes report.md). Ground truth is **cold** mode, 5 samples,
  256 MB, data sets w16_uniform / w64_uniform / w16_c0sorted (c0 has 1000 distinct values),
  134 templates per width, 12 storage configs, about 10.4k points. Full run started in the
  background (log `results/ground_truth/laptop_v1.log`), then a 10% repeat
  (`laptop_v1_repeat`) and `results/ground_truth/repeatability.txt`.
- **Phase 3 (calibration):** `tools/calibrate.cpp` (Makefile target `build/calibrate`,
  flags unchanged) -> raw JSON; `advisor/hardware.py` derives hw.json (levels, TLB, MLP, B_eff,
  over-fetch table, CPU constants, fallbacks). **Not yet built or run** (waits for the GT run).
- **Phase 4 (cost model):** `advisor/models/` (patterns: regions, basic, combine, describe,
  evaluate, extensions; ladder v0-v3; registry), `advisor/validate.py` (CP5; saves params).
  Tested only with a provisional hw.json in the scratchpad (not accuracy evidence).
- **Phase 5 (advisor):** `advisor/{workload,selectivity,report,maps,cli}.py`, `advisor/search/`,
  `configs/data/*.yaml`, `advisor/workloads/examples/*.yaml`; `python3 -m advisor
  recommend|predict|maps|validate|evaluate|calibrate|derive|ground-truth`.
- **Phase 6 (validation):** `advisor/evaluate.py` (V1 accuracy + counters + scatter, V2/V3
  random workloads vs baselines + regret CDF, V4 via `--fit-split dataset:NAME:nolookup`, V5
  measured-vs-predicted maps, V7 perturbations), `advisor/workloads/generate.py`,
  `scripts/run_validation.sh` (runs CP4-CP7 end to end).
- Tests: `python3 tests/advisor/test_patterns.py` and `tests/advisor/test_advisor.py`
  (no pytest installed; plain scripts), 7/7 each.

### Results (2026-11-14, late evening; fast mode)

- Ground truth `results/ground_truth/laptop_v1`: 36/36 runs ok, 10,368 points, 100 min. Repeat (10%):
  median deviation 4.3%, 54% within ±5% (CP3 ±5% not met; noise floor of the laptop).
  `report.md` there has winners, bytes/row, lookups, PAX page curve, execution-option effects.
- Calibration `configs/hw/laptop.json` (512 MB, 3 samples): DRAM 17.8 GB/s, 109 ns; L1/L2/L3
  82/46/40 GB/s, 1.3/4.0/14 ns; B_eff 145 B; MLP 15; walk ~100 ns; branch miss 26 cycles; 3.79 GHz.
- One model fix made from counters: page walks inside sequential streams are hidden (v2/v3,
  `page_prefetch` in evaluate.py); measured walks were 10-35x fewer than pages crossed.
- Validation `results/validation/laptop_v1/` (main, v4_generalisation, cp5, recommend, maps):
  ladder median |err| v0 59%, v1 77%, v2 26%, v3 16% (p90 47%); groups > 30%: unfiltered
  reconstruction (DSM/PAX). V2 (500 workloads): advisor top-1 73%, median regret 1.07, worst
  1.54 vs always-DSM 85% / 1.00 / 8.35 and other rules worst 6.5-11.8. Storage-only regret
  median 1.00. V5 agreement 32-92%, most SUM disagreements within a 10% margin. V7: decisions
  change 2-13% under perturbations. V4: errors similar on unseen width/distribution.
- Known gaps: CP5 tail (p90 47% vs 30%), top-1 73% vs 85% target, unfiltered materialize
  over-predicted ~54%, NSM/PAX lookups under-predict DRAM bytes, counters table in evaluate
  report.md prints empty (bug), full (non-fast) run not done.
- `presentation.md`: 22-slide outline with real numbers.

### Hardware insights (2026-11-15)

`results/insights/README.md` lists six confirmed insights (L1 associativity in DSM
reconstruction, prefetcher bounded by the 4-KB page, two lines per row → whole row,
stall-dependent mispredict cost, TLB decides lookup cost, MLP ~15) plus two ground-truth
anomalies that did NOT reproduce (noise). Probe: `tools/assoc_probe.cpp` (build:
`g++ -std=c++17 -O2 -march=native -Isrc tools/assoc_probe.cpp src/perf_counters.cpp -o build/assoc_probe`).
Deck: `results/presentation/NSM_DSM_PAX_layout_advisor.pptx` (33 slides) built by
`scripts/deck_data.py` + `scripts/build_presentation.py` (needs python-pptx; LibreOffice now
installed for render QA).

## 10. Next steps (in order)

1. When the GT run finishes: `python3 scripts/analyze_ground_truth.py results/ground_truth/laptop_v1`,
   read repeatability.txt (CP3: ±5% target), summarise hardware takeaways for the user.
2. `make` (builds build/calibrate), run `scripts/run_validation.sh` (machine idle). Check hw.json
   plausibility (CP4) and the CP5 go/no-go; iterate on the model where groups exceed 30%.
3. Lookup experiment plot (CP2 remainder), VM portability (CP8), report (Phase 8).
4. Ask the user for the deadline.

## 11. Earlier next-steps list (kept for reference)


1. **Phase 1b (CP2): point lookups** — `Layout::lookup(row_ids, proj)`; ids generated before
   timing from `LookupSpec` (seeded; `uniform`, `sorted`; independent lookups); NSM copies fields,
   DSM one value per column array, PAX page = id / T, slot = id % T; output via TupleSink;
   reference implementation in `reference.cpp`; remove the "lookup not implemented" guards in
   `main.cpp`/`reference.cpp`; tests; `scripts/run_lookups.sh` + plot (ns/lookup vs layout,
   projected cols 1/4/16/64, width, size, id distribution). Expected: NSM fastest for full tuples.
2. Phase 2 (CP3): ground-truth runner `scripts/run_ground_truth.py` + `advisor/engine.py` adapter;
   measurement store per (storage config, query, exec options); 256 MB default.
3. Phase 3 (CP4): `src/calibrate.cpp` → `hw.json` (Manegold-style per-level params + extras).
4. Phase 4 (CP5, go/no-go): `advisor/models/patterns/*`, models v0–v3, fitting, per-level miss
   validation vs counters, time error targets (median ≤15% DRAM-resident).
5. Phase 5 (CP6): advisor CLI (`python -m advisor recommend/predict/validate/maps/calibrate`).
6. Phase 6 (CP7): validation V1–V5 (accuracy, decision quality on 1,000 random workloads, baselines,
   held-out generalisation, decision maps). Phase 7 (CP8): VM. Phase 8: report.
7. Ask the user for the deadline.
