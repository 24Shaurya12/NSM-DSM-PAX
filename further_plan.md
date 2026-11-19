# Layout Advisor: Implementation Plan

**Goal.** Given (1) a machine, (2) a workload and (3) a data distribution, recommend the
storage layout (NSM, DSM or PAX) and its parameters, predict how fast it will be, and show with
measurements that the recommendation is correct.

**Scope (agreed).** Analytical queries (scans, filters, aggregates, tuple reconstruction) plus
**point lookups**. No updates or inserts for now; the design leaves room to add them later.

**Design rules.**

1. **Extensible.** New layouts, operators, execution options, cost models, search strategies,
   selectivity estimators, data distributions and workload templates are each added in one
   place, without editing the rest. Section 9 is the "how to add X" guide.
2. **Hardware-independent.** No CPU, cache size, core layout or counter name is hard-coded in
   the engine, scripts or model. Everything machine-specific is either detected at run time or
   measured by calibration and stored in `hw.json`. The model only reads `hw.json`, so the same
   code serves the laptop, the EPYC VM and any later machine (§1.1).

---

## 0. Where we are now

Already in the repository:

| Piece | State |
|---|---|
| Engine (`src/`) | NSM, DSM (fixed 1024-row batches), PAX (any page size); `sum` and `materialize` over the first k columns; conjunctive equality/range predicates; selection-vector filtering; warm/cold timing; correctness checks against the logical table |
| Counters | cycles, instructions, branch misses, LLC refs/misses, L1D misses, dTLB misses, DRAM read/write bytes |
| Experiments | working-set size, selectivity, two predicates, PAX page size, PAX-wins (about 13,000 timed samples with counters in `results/`) |
| Findings the model must capture | time ≈ DRAM bytes ÷ ~20 GB/s when the data is in DRAM; NSM reads the whole row; DSM touches every line once selectivity ≳ 1/8; branch misses ≈ 2·s·(1−s) per evaluated predicate, about 28 cycles each; PAX over-fetch shrinks as minipages grow; per-page overhead ≈ 27 instructions ÷ tuples per page; large PAX pages ≈ DSM with large batches |

Gaps the advisor needs filled first: point lookups, arbitrary column lists, DSM batch size as a
parameter, branch-free filtering, a generic query description, and a way to run "every
candidate on every query" to get ground truth.

---

## 1. Architecture

```
                  ┌──────────────────────── advisor/ (Python) ─────────────────────────┐
 hw.json ────────►│ HardwareProfile                                                    │
 (calibrate)      │                                                                    │
 data.yaml ──────►│ DataProfile ──► SelectivityEstimator (pluggable)                   │
 workload.yaml ──►│ Workload (QuerySpecs + weights)                                    │
 space.yaml ─────►│ CandidateSpace (pluggable layouts + parameter grids)               │
                  │        │                                                           │
                  │        ▼                                                           │
                  │ Search (pluggable) ──► CostModel (pluggable) ──► CostBreakdown     │
                  │        │                                                           │
                  │        ▼                                                           │
                  │ Report: choice, predicted time, margin, cost breakdown, runner-up  │
                  └────────────────────────────────────────────────────────────────────┘
                                   │ engine adapter (builds command lines, parses CSV)
                                   ▼
                  ┌──────────────────────── engine (C++) ──────────────────────────────┐
                  │ LayoutFactory ── NSM / DSM / PAX / (future layouts)                │
                  │ QuerySpec: op × projection × predicates × lookups                  │
                  │ ExecOptions: dsm_batch, filter mode, (threads)                     │
                  │ Reference executor (correctness) · timing · perf counters · CSV    │
                  └────────────────────────────────────────────────────────────────────┘
```

**Key modelling decision: storage parameters vs execution parameters.**

- *Storage* choices are made **once per table**: the layout and its physical parameters (PAX
  page size, and later column groups).
- *Execution* choices can differ **per query**: DSM batch size, branchy vs branch-free filtering,
  predicate order, and later thread count.

The advisor therefore picks one storage configuration for the whole workload and, for each
query, the best execution options for that storage. The report states both.

### 1.1 Hardware independence

Today's code still contains a few assumptions that only hold on the laptop (an Intel hybrid
CPU). Phase 1 removes them:

| Assumption today | Where | Replacement |
|---|---|---|
| Pin to CPU 2, "a P-core" | `scripts/run_*.sh` | `scripts/common.sh` picks a core automatically: on hybrid Intel the performance-core PMU's CPU list (`/sys/devices/cpu_core/cpus`), otherwise the last online CPU (away from CPU 0's interrupts); always overridable with `CPU=` |
| Core counters opened on the `cpu_core` PMU | `src/perf_counters.cpp` | already falls back to generic events when that PMU doesn't exist; keep it, and record which counters opened in `machine.txt` |
| DRAM traffic from Intel `uncore_imc_free_running_*` | `src/perf_counters.cpp` | a small table of known memory-controller PMUs (Intel IMC; AMD `amd_umc`/`amd_df` on bare metal); if none is available (e.g. inside a VM), DRAM traffic is reported as unavailable and estimated as LLC misses × line size, flagged as an estimate |
| Cache sizes from sysfs | scripts, plots | sysfs when present, otherwise `lscpu`/CPUID; calibration measures the effective capacities anyway |
| 128 B transfer unit, prefetch behaviour | (planned) model | never assumed: `B_eff` and the over-fetch table are measured by `calibrate` on each machine |
| Prefetcher-off ablation via Intel MSR `0x1A4` | (planned) | optional and Intel-only; skipped automatically elsewhere |

`hw.json` records a **capabilities** block (which counters exist, huge pages available,
hybrid or not, virtualised or not). Validation adapts to it: on a machine without counters it
validates time only, and says so.

**The second machine** is a KVM virtual machine on an AMD EPYC 9V74 (Zen 4): 5 vCPUs, one
socket, one NUMA node. That makes it a good portability test (different vendor, cache sizes,
prefetchers and memory system), with VM-specific caveats handled in CP8 (§7).

---

## 2. Phase 1: Engine refactor (C++)

### 2.1 Query specification

Replace the op ×`-- --where × --k` cross product with one explicit query description. The old
flags stay and are translated into specs, so existing scripts keep working.

```cpp
// src/query_spec.h
enum class Op { Sum, Materialize, Lookup };      // later: GroupBy, Update, ...

struct LookupSpec {
    std::size_t count = 0;                       // number of point lookups
    std::string dist = "uniform";                // uniform | sorted | zipf:<theta> (extensible)
    uint64_t seed = 1;
};

struct QuerySpec {
    std::string label;                           // for the CSV, comma-free
    Op op = Op::Sum;
    std::vector<int> project;                    // columns to aggregate or return, any order
    Conjunction where;                           // empty = no filter (scans only)
    LookupSpec lookups;                          // only for Op::Lookup
};
```

Command-line mini-language (one parser in `query_spec.cpp`, repeatable flag):

```
--query "op=sum;cols=0-3;where=c0<5,c12<50"
--query "op=materialize;cols=all;where=c0<1"
--query "op=lookup;cols=all;n=100000;dist=uniform"
--query "op=sum;cols=0,4,8,12;where=none"
```

`cols` accepts `all`, ranges `a-b`, lists `a,b,c` or a mix. Unknown keys are an error.

### 2.2 Execution options

```cpp
// src/exec_options.h
enum class FilterMode { Branchy, Branchless };

struct ExecOptions {
    std::size_t dsm_batch = 1024;                // rows per DSM batch
    FilterMode filter = FilterMode::Branchy;
    int threads = 1;                             // reserved for the multithreading extension
};
```

**Multithreading (designed in, implemented later).** `threads` is part of `ExecOptions`
from day one, with the engine rejecting values other than 1 for now. Adding it later means:
splitting rows (DSM, NSM) or pages (PAX) into per-thread ranges, opening the counters with
`inherit`, filling `bw_dram[threads]` and the per-thread CPU constants in `hw.json`
(calibration already has a slot for them), and letting the cost model divide work and share
bandwidth across threads. No interface changes.

Flags take lists so one process can sweep them without rebuilding the table:
`--dsm-batch 256,1024,16384,131072` and `--filter branchy,branchless`. The engine runs the cross
product of layouts × queries × execution options × modes.

### 2.3 Layout interface and factory

```cpp
class Layout {
public:
    // metadata as today (name, rows, cols, footprint_bytes, flush_cache)
    virtual QueryResult scan_sum(const std::vector<int>& cols) const = 0;
    virtual QueryResult filter_sum(const Conjunction&, const std::vector<int>& cols,
                                   const ExecOptions&) const = 0;
    virtual QueryResult filter_materialize(const Conjunction&, const std::vector<int>& cols,
                                           const ExecOptions&) const = 0;
    virtual QueryResult lookup(const std::vector<uint64_t>& row_ids,
                               const std::vector<int>& cols) const = 0;
    virtual std::string params() const = 0;      // e.g. "page=16384", for the CSV
};
```

- `src/layout_factory.{h,cpp}`: a registry `name → builder(Table, ParamMap)`. The CLI takes
  `--layout nsm,dsm,pax:page=262144`. A new layout registers itself here and nothing else
  changes.
- `select_positions` gains a branch-free variant
  (`sel[m] = i; m += pred.matches(v[i]);`), chosen via `ExecOptions::filter`.
- A single `run_query(layout, spec, opts)` dispatches on `Op`.

### 2.4 Point lookups

- Row ids are generated **before timing** from `LookupSpec` (seeded), stored in a vector and
  reused for every repetition, so all layouts see the same ids.
- Lookups are **independent** (the CPU may overlap their cache misses), which matches a batch of
  index probes. A dependent-chain variant (`dist=chain`) can be added later to measure pure
  latency.
- Each lookup writes the projected columns of one tuple into the `TupleSink`:
  - NSM: one contiguous copy of the projected fields
  - DSM: one value from each projected column array
  - PAX: find page `id / tuples_per_page`, slot `id % tuples_per_page`, one value per minipage
- The checksum is the same as for `materialize`, so correctness checking reuses the existing
  machinery.

### 2.5 Reference executor and tests

- Move the expected-result computation out of `main.cpp` into `src/reference.{h,cpp}`: a
  straightforward evaluation of any `QuerySpec` on the logical `Table`. Every new operator gets
  a reference implementation here first.
- `make test` builds `build/engine_tests`. It runs every layout × op × filter mode × DSM batch
  × several PAX page sizes on small tables with awkward sizes (e.g. 10,007 rows, 3 / 16 / 64
  columns), plus empty and full selections and out-of-range lookups (rejected), and compares
  each result with the reference.

### 2.6 Data distributions (pluggable)

Each column's values come from a **generator** chosen by name, so new distributions plug in
without touching the layouts or queries:

```
--dist default=uniform:0:99            # every column unless overridden
--dist c0=sorted                       # values ascending along the table
--dist c0=clustered:run=1024           # random, but in runs of 1024 equal-ish values
--dist c1=zipf:theta=1.1:ndv=1000      # (later)
--dist c2=correlated:c0:rho=0.8        # (later)
--dist c3=file:path/to/values.bin      # (later) any custom data you supply
```

- Engine: `src/distributions.{h,cpp}` holds a registry `name → generator(params, rows, seed)`.
  The first version implements `uniform`, `sorted` and `clustered`; the rest are listed as
  extension points.
- Advisor: a matching registry of **distribution models** with three methods the cost model
  needs: `pass_rate(lo, hi)` (selectivity), `run_length(lo, hi)` (how qualifying rows cluster,
  which changes the line-touch probability) and `branch_predictability()` (sorted data makes
  predicate branches predictable).
- Adding a distribution = one generator in the engine + one model class in the advisor (§9).

### 2.7 CSV schema (additions)

`query` (label), `op`, `project` (e.g. `c0..c3` or `c0|c4|c8`), `n_project`, `predicate`,
`n_items` (rows scanned, or lookups performed), `dsm_batch`, `filter`, `layout_params`. Existing
columns are kept, with `k` = `n_project` for backward compatibility. Plot scripts are updated to
read the new names.

### Checkpoint CP1: engine refactor done ✅ (2026-11-14)

- [x] `make test` passes (all layouts × ops × options against the reference): 50,914 checks in
      ~2.5 s; two injected faults were both caught.
- [x] Old scripts (`run_*.sh`) still run unchanged (all five run + plot on small settings).
- [x] **No performance regression:** 54 configurations (256 MB; NSM/DSM/PAX; sum and
      materialize; k = 1/4/16; no filter, `c0<1`, `c0<50`), old vs new binary interleaved twice:
      49 within ±5%, 4 faster by 7–12%, 1 slower by 5.1%; median ratio 1.004. The contiguous
      case needed specialised loops in NSM (reconstruction) and PAX (scan, reconstruction).
- [x] Branch-free filtering removes the 50% latency peak on a 256 MB table: at `c0<50`,
      DSM 5.56 → 2.11, PAX 5.69 → 2.58, NSM 8.58 → 6.35 ns/row.
- [ ] Portability on the second machine (auto core selection, counter fallbacks): to be checked
      when the VM is first used (CP4/CP8).

Lessons for later phases: (1) tiny changes to hot loops (a loop-invariant branch, one extra
pointer load) moved DRAM-bound timings by 10–40%; (2) NSM reconstruction timings depend on the
output buffer's address (up to ±20% across 4 KB offsets). Both set a noise floor the cost model
must tolerate, and future engine changes must be re-checked against a baseline binary.

### Checkpoint CP2: point lookups done

- [ ] Lookup tests pass.
- [ ] A small lookup experiment (`scripts/run_lookups.sh`): ns per lookup vs layout, number of
      projected columns (1, 4, 16, 64), table width, table size (in cache vs DRAM), id
      distribution (uniform, sorted). Expected: NSM fastest for full tuples, DSM slowest on wide
      tables, PAX in between; sorted ids much faster for every layout.
- [ ] Plot and short write-up of the lookup results.

---

## 3. Phase 2: Ground-truth runner and measurement store

The advisor is only as credible as its evaluation. We need cheap, repeatable "measure every
candidate on every query" runs.

- `advisor/engine.py` (adapter): turns `(Candidate, [QuerySpec], ExecOptions grid, data spec)`
  into engine command lines, runs them pinned to a P-core, parses the CSVs.
- `scripts/run_ground_truth.py`: takes a workload file (or a template set) and a candidate
  space; runs **one process per storage configuration** (the table is built once) with all
  queries × execution options inside it; appends to `results/ground_truth/<name>/measurements.csv`
  plus `machine.txt`.
- Measurements are stored **per (storage config, query, execution options)**. The time for any
  workload mix is a weighted sum, so one measurement set can evaluate thousands of random
  workloads without re-running anything.

**Budget estimate.** About 30 storage configs (NSM; DSM; PAX × 11 page sizes; × 2–3 table
widths) × about 60 query templates × 2 filter modes × up to 4 DSM batches. At 256 MB each
measurement takes ~0.3 s, so ~1–2 hours for a full laptop dataset. Default to 256 MB tables and
run 1 GB only for spot checks.

### Checkpoint CP3: ground truth available

- [ ] `run_ground_truth.py` produces a measurement store for the template set in §6.1.
- [ ] Re-running a 10% subset reproduces medians within ±5% (repeatability).
- [ ] Each measurement records machine state (power, governor, counters available).

---

## 4. Phase 3: Hardware calibration

The hardware model follows Manegold, Boncz & Kersten [1]: the memory system is a stack of
**N cache levels, with the TLB treated as one more cache** (its "line" is the 4 KB OS page and its
capacity is entries × page size). Each level i is described by the parameters of [1] Table 1,
plus a few modern additions we found necessary.

`build/calibrate` (new C++ binary, in the spirit of the paper's *Calibrator* tool) plus
`python -m advisor calibrate` writes `hw.json`. It takes ~2 minutes and uses **only calibration
kernels, never validation queries**, so validation stays honest.

**Per level (L1, L2, L3, TLB; DRAM as the last level):**

| Parameter ([1] notation) | Meaning | How it's measured |
|---|---|---|
| `C_i` capacity, `#_i` lines | size of level i | sysfs (caches); TLB entries from a page-strided pointer chase |
| `B_i` line size | transfer unit into level i | sysfs, **plus `B_eff` = effective transfer unit** (128 B on this CPU: a line and its prefetched neighbour) from a strided scan vs the DRAM byte counter |
| `b^s_{i+1}` sequential bandwidth | cost of a sequential miss (`l^s = B/b^s`) | sequential scans of regions sized for each level |
| `l^r_{i+1}` random latency | cost of a random miss | dependent pointer chase over regions sized for each level |
| `t_tlb` | cost of a TLB miss (page walk) | page-strided chase, 4 KB vs 2 MB pages (if THP available) |

**Modern additions (not in [1]):**

| Constant | Meaning | How it's measured |
|---|---|---|
| `mlp` | random misses the core overlaps (independent lookups vs a dependent chain) | independent random loads vs dependent chain, same region |
| `overfetch(run, align)` | extra bytes the prefetchers bring in after a sequential run of a given length, depending on where it sits in its 4 KB page | strided runs of length 64 B…64 KB vs the DRAM byte counter; stored as a small lookup table |
| `c_scan`, `c_pred`, `c_gather`, `c_copy` | CPU cycles per value for each engine kernel (the paper calibrates CPU cost per algorithm in cache, which is the same idea) | engine kernels on L1-resident data |
| `c_branch_miss` | cycles per branch misprediction | in-cache filter at 50% vs 0% selectivity |
| `o_batch`, `o_col` | fixed overhead per batch/page and per column per batch | PAX/DSM in-cache with 8 vs 1024 tuples per batch |
| `freq_ghz` | effective clock | cycles ÷ ns during calibration |
| `bw_dram[threads]` | bandwidth at 1, 2, 4, … threads (for the multithreading extension) | parallel sequential scans |

### Checkpoint CP4: calibration works

- [ ] `hw.json` for the laptop, documented.
- [ ] Two calibrations an hour apart agree within ±5% per constant (otherwise fix noise:
      power, governor, pinning).
- [ ] Constants are physically plausible and match earlier measurements (e.g. sequential DRAM
      bandwidth ≈ 19–21 GB/s, `c_branch_miss` ≈ 25–30 cycles, `B_eff` = 128 B, TLB covers
      the measured 0.031 misses/row at 4 KB stride).

---

## 5. Phase 4: Cost model

The model follows the structure of Manegold et al. [1], extended for modern CPUs. This gives
the project a published, principled backbone instead of ad-hoc formulas, and it makes adding a
layout or operator a matter of *describing its memory access pattern* rather than deriving new
formulas.

### 5.1 The idea of [1] in brief

1. **Data regions.** Every data structure is a region R with `R.n` items of width `R.w` bytes:
   an NSM table (items = rows), a DSM column (items = values), a PAX page sequence (items =
   pages), a minipage, the output buffer, a selection vector.
2. **Basic access patterns**, each with a closed-form *number of cache misses per level*
   (sequential and random misses counted separately):
   - `s_tra(R, u)`: single sequential traversal touching `u` of each item's `R.w` bytes
     (eq. 3/4). If the untouched gap `R.w − u` is smaller than a line, every line is loaded;
     otherwise each item costs about ⌈u/B⌉ lines.
   - `rs_tra(r, d, R, u)`: the traversal repeated r times (eq. 7). If the footprint fits in the
     level, only the first pass misses; this is exactly our warm in-cache regime.
   - `r_tra`, `rr_tra`, `r_acc(r, R, u)`: random traversal, and r random accesses with the
     expected number of distinct items/lines touched (eq. 5, 8, 9). `r_acc` models point
     lookups.
   - `nest(R, m, P, O, D)`: interleaved multi-cursor access over m sub-regions (eq. 10); extra
     misses appear once the m cursors need more lines than the level holds. This models DSM
     batching across many columns (and the TLB thrashing it can cause on wide tables).
3. **Combining patterns.** `⊕` = one after another (later patterns reuse what the earlier ones
   left in the cache, eq. 11–12); `⊙` = concurrently (the cache is shared in proportion to each
   pattern's footprint, eq. 13).
4. **Time.** `T_mem = Σ_i (M^s_i · l^s_{i+1} + M^r_i · l^r_{i+1})` (eq. 1), and
   `T = T_mem + T_cpu` with `T_cpu` calibrated in cache.

**It already fits our measurements.** Applying eq. 3/4 with the TLB as a cache level (B = 4 KB)
reproduces the measured dTLB misses of `SUM(c0)` on PAX **exactly** for every page size
(0.0312 / 0.0312 / 0.0156 / 0.0078 / 0.0020 misses per row for 1K / 4K / 8K / 16K / 64K pages).
With an effective transfer unit of 128 B it predicts NSM's 128 B/row (measured 125) and DSM's
8 B/row (measured 9.5). It **under-predicts small-page PAX** (16 vs 48 B/row at 1 KB pages,
8 vs 29 B/row at 16 KB): the 2002 model has no hardware prefetcher. That gap is the main
modern extension below.

### 5.2 Our extensions for modern hardware

| Extension | Why (evidence from our runs) |
|---|---|
| **Prefetch over-fetch:** each sequential run of length `u` adds `overfetch(u, align)` bytes, calibrated (§4) | small-page PAX reads 3–6× the bytes [1] predicts |
| **Effective transfer unit** `B_eff` = 128 B at the DRAM level | NSM reads ~125 B/row for one 8 B value |
| **Memory-level parallelism:** random misses cost `l^r / mlp`, not `l^r` | independent lookups overlap their misses; [1] only says whether the CPU can have several misses outstanding |
| **Branch mispredictions in `T_cpu`:** `n · 2f(1−f) · c_branch_miss` per branchy predicate on random data (≈ 0 on sorted data; 0 for branch-free filtering) | measured 0.50 misses/row at 50% and 0.16 at 10%, ~28 cycles each |
| **Per-batch / per-page overhead:** `(R/T) · (o_batch + o_col · cols)` | 4 + 27/T instructions per row measured for PAX |
| **Overlap of memory and CPU time:** `T = max(T_mem, T_cpu) + γ·min(T_mem, T_cpu)` as an alternative to [1]'s `T_mem + T_cpu` | out-of-order cores hide CPU work behind memory stalls; both variants are kept and compared |
| **Sparse ordered access** (new basic pattern `ss_tra(R, f, u)`): items visited in order, each with probability f; a line is touched with probability `1 − (1 − f)^{B/R.w}` | the DSM/PAX gather after a selection (not in [1]'s pattern set) |
| **Optional associativity term** | DSM rebuilding tuples from 16 columns showed 4.9 L1 misses/row vs PAX 0.8: column arrays aligned to the same offset compete for one L1 set, which [1] (capacity only) cannot see |

### 5.3 Access-pattern descriptions per layout and operator

Each `(layout, operator, exec options)` is described once as a pattern expression. The cost
engine evaluates it per cache level. Examples (16 int64 columns, w = 8; `P` = predicate
columns, `Q` = projected columns, f = pass rate):

| Query | NSM | DSM | PAX (T tuples/page) |
|---|---|---|---|
| scan, columns Q | `s_tra(Table, u = span of Q in a row)` | `⊕_{c∈Q} s_tra(Col_c)` | `s_tra(Pages, u = minipages of Q)` + over-fetch per minipage run |
| filter + sum | `s_tra(Table, u = first granule)` ⊙ `ss_tra(Table, f, u = rest of Q)` | per batch: `s_tra(Col_p1)` ⊕ `ss_tra(Col_p2, f1)` ⊕ … ⊕ `⊕_{c∈Q} ss_tra(Col_c, f)`; across batches a `nest` over the touched columns | the same per page, over minipages |
| reconstruction | as filter, plus `s_tra(Output)` | as filter, but the gathers run concurrently (`⊙`) | as filter, concurrent gathers within one page |
| point lookups (N ids) | `r_acc(N, Table, u = span of Q)` | `⊙_{c∈Q} r_acc(N, Col_c, w)` | `r_acc(N, Pages, u = |Q| minipage slots)` |

These descriptions are data, not code paths: they live in `advisor/models/patterns/describe.py`,
one function per `(layout, op)`. A new layout or operator needs only a new description.

### 5.4 Interface

```python
class CostModel(ABC):
    name: str
    def predict(self, storage: StorageConfig, exec_opts: ExecOptions, query: QuerySpec,
                ctx: ModelContext) -> CostBreakdown: ...
    def fit(self, measurements: pd.DataFrame, hw: HardwareProfile) -> None:  # optional
        ...

@dataclass
class CostBreakdown:
    total_ns: float
    mem_ns: float; cpu_ns: float; lookup_ns: float; tlb_ns: float
    misses: dict[str, tuple[float, float]]      # level -> (sequential, random), per [1]
    dram_bytes: float
    branch_misses: float; instructions: float   # predicted, comparable with counters
```

`ModelContext` carries the hardware profile, data profile and selectivity estimator. Models
register with `@register_model("name")`.

Pattern machinery (`advisor/models/patterns/`):

```
regions.py     Region(n, w), helpers for |R|_B and footprints
basic.py       STra, RSTra, RTra, RRTra, RAcc, Nest, SSTra   # each: misses(level, state) -> (Ms, Mr)
combine.py     Seq (⊕) with cache-state propagation, Conc (⊙) with footprint-proportional sharing
describe.py    describe(layout, op, exec_opts, query, data) -> pattern expression
evaluate.py    pattern expression × hw levels -> misses per level -> T_mem
extensions.py  over-fetch, B_eff, MLP, associativity hooks applied during evaluation
```

### 5.5 Model ladder (for the report's ablation)

| Version | Model | Purpose |
|---|---|---|
| v0 | time = bytes ÷ sequential bandwidth | the plain "bytes moved" story |
| v1 | Manegold et al. [1] as published: per-level sequential/random misses, `T_mem + T_cpu` | the classic model on modern hardware |
| v2 | v1 + `B_eff`, prefetch over-fetch, MLP | modern memory system |
| v3 | v2 + branch mispredictions, per-batch overhead, overlap `max + γ·min` (+ optional associativity) | modern CPU |

Each version is a registered model, so the same validation code compares them. Report the error
of each version per group. That shows *which mechanisms matter* on modern hardware and how much a
2002-era model misses, which is a strong analysis section for the report.

### 5.6 Fitting

Most constants come straight from calibration. Only γ and, where calibration is too coarse, the
over-fetch table and overhead terms are fitted (least squares on log time) on the **calibration
split** of the measurement store. At most ~8 fitted values, each with a physical meaning.
Fitting never sees validation queries.

### Checkpoint CP5: model accurate enough (go/no-go)

- [ ] **Per-level misses** (like [1] Figure 4): predicted vs measured dTLB misses, LLC misses
      and DRAM bytes across table sizes, page sizes, selectivities and lookups, within ±20%
      for DRAM-resident cases. (L1 demand misses are distorted by the L1 prefetcher; compare
      L1/L2 only qualitatively, or add L2 request counters.)
- [ ] **Time:** median absolute error ≤ 15% for DRAM-resident tables and ≤ 25% in cache; no
      layout/op group worse than 30%.
- [ ] Predicted vs measured scatter (log-log) and error tables per group.
- [ ] Model ladder table (v0 → v3).

**If CP5 fails,** look at the worst groups and their counters to find the missing mechanism,
add one term (as a pattern extension), and repeat. Don't build the advisor on an inaccurate
model.

---

## 6. Phase 5: Advisor

### 6.1 Inputs

**Hardware:** `hw.json` from calibration.

**Data profile (`data.yaml`):**

```yaml
rows: 8_000_000
columns:
  - {name: c0, width: 8, dist: uniform, min: 0, max: 99, sorted: false}
  - {name: c1, width: 8, dist: zipf, theta: 1.1, ndv: 1000}
  - ...                        # or: default: {dist: uniform, min: 0, max: 99}
correlations: []               # optional, e.g. [{cols: [c0, c1], rho: 0.8}]
```

**Workload (`workload.yaml`):**

```yaml
name: dashboard_mix
queries:
  - {weight: 0.6, op: sum,         cols: [c0, c1, c2, c3], where: "c0 < 5"}
  - {weight: 0.2, op: materialize, cols: all,              where: "c0 < 1 AND c12 < 50"}
  - {weight: 0.2, op: lookup,      cols: all,              lookups: 10000, dist: uniform}
```

Weights are relative frequencies; the objective is the expected time per query
(Σ weight × time), with total time over a run as an option.

**Candidate space (`space.yaml`, defaults built in):**

```yaml
storage:
  nsm: {}
  dsm: {}
  pax: {page: [1K, 2K, 4K, 8K, 16K, 64K, 256K, 1M, 4M, 16M]}
exec:
  dsm_batch: [256, 1024, 16384, 131072]
  filter: [branchy, branchless]
  predicate_order: [as_written, by_selectivity]
```

### 6.2 Selectivity estimation

Interface `SelectivityEstimator.estimate(conjunction, data_profile) -> per-step pass rates`.
Implementations: `uniform` (from min/max), `histogram`, and `exact` (sampled from the generated
data, used to separate model error from estimation error during validation). Conjunctions
assume independence unless the profile lists a correlation.

### 6.3 Search

Interface `Search.run(space, workload, model, ctx) -> RankedResults`.

- `exhaustive` (default): the space is small (tens of storage configs × a few execution
  options), and each prediction takes microseconds.
- Per storage config, choose the best execution options **per query**, then sum the weighted
  query costs. Rank storage configs by that total.
- Later options: pruned search, local search over continuous page sizes, robust choice that
  minimises the worst case over selectivity or bandwidth uncertainty.

### 6.4 Output

```
Machine: i7-1360P (hw.json, calibrated 2026-11-20), 1 thread, table 8M × 64 (4 GB)

Recommended storage: PAX, page = 256 KB
Per-query execution:  q1 branch-free, q2 branchy + order by selectivity, q3 —
Predicted:            41 ms per workload unit
Runner-up:            DSM (47 ms, 1.15× slower)   → close call
Worst option:         NSM (118 ms, 2.9×)
Where the time goes:  q1 DRAM traffic 64% · q3 lookups 22% · branch misses 9%
Sensitivity:          the choice holds if q1 selectivity ∈ [1%, 9%] and bandwidth ±20%
```

Also JSON output for scripting (`--json`). CLI:

```
python -m advisor calibrate           --out hw.json
python -m advisor recommend  --hw hw.json --data data.yaml --workload w.yaml [--space s.yaml]
python -m advisor predict    --hw hw.json --data data.yaml --query "op=sum;cols=0-3;where=c0<5"
python -m advisor validate   --hw hw.json --measurements results/ground_truth/<name>
python -m advisor maps       --hw hw.json --grid selectivity×projection --out plots/
```

### Checkpoint CP6: advisor works end to end

- [ ] `recommend` runs on the example workloads and gives sensible answers: scan-heavy → DSM
      or large-page PAX; lookup-heavy with full tuples → NSM; mixed wide-table workloads at
      ~1% selectivity → small-page PAX.
- [ ] Unit tests for the parsers, estimators, search and report.
- [ ] Adding a dummy model and a dummy search strategy needs only a new file each (proves the
      registries work).

---

## 7. Phase 6: Validation (the evidence)

### 7.1 Template set for ground truth

| Family | Values |
|---|---|
| Table width | 16, 64 columns (plus 32 if time allows) |
| Ops | sum, materialize, lookup |
| Projection | 1, 25%, 100% of columns; prefix and spread (`c0, c4, c8, …`) |
| Selectivity | 0.1, 0.5, 1, 2, 5, 10, 25, 50, 90, 100% (single predicate) and two-predicate variants |
| Lookups | 10³–10⁵ ids, uniform and sorted |
| Data | uniform random (main); sorted/clustered on c0 (second) |

### 7.2 Experiments and metrics

| ID | Question | Metric | Target (proposal) |
|---|---|---|---|
| V1 | Are predictions accurate? | median and 90th-percentile absolute % error per group | ≤ 15% / ≤ 30% (DRAM-resident) |
| V2 | Does it pick the right layout? | on 1,000 random workload mixes: top-1 accuracy with ties within 5% counted as correct; regret = chosen ÷ best | ≥ 85% accuracy, median regret ≤ 3%, worst ≤ 25% |
| V3 | Is it better than fixed rules? | the same workloads under baselines: always-NSM, always-DSM, always-PAX-16K, "DSM unless lookups > x%" | advisor regret lower than every baseline |
| V4 | Does it generalise? | fit on 16-column tables ≤ 256 MB, test on 64 columns / 1 GB / sorted data / lookups | V1 and V2 targets still met, or the failure is explained |
| V5 | Where are the boundaries? | decision maps (selectivity × projection, lookup ratio × selectivity, width × selectivity), predicted regions with measured winners overlaid | ≥ 90% of cells agree, disagreements only near boundaries |
| V6 | Does it transfer to other hardware? | recalibrate on a second machine, no model changes, repeat V1–V2 | the same targets; boundaries visibly move |
| V7 | How robust is the choice? | perturb selectivity estimates and calibration constants; how often does the decision change? | report the stability margins |

Random workload generator: `advisor/workloads/generate.py` draws 2–6 queries from the template
set with random weights. It is seeded and saved with the results.

### Checkpoint CP7: evidence ready

- [ ] V1–V5 run on the laptop, with tables and figures.
- [ ] The failure cases (wrong decisions) are listed with an explanation for each.

### Checkpoint CP8: portability (needs a second machine)

Target: the AMD EPYC 9V74 KVM guest (5 vCPUs, 1 socket, 1 NUMA node), plus any later machine.

- [ ] The same code runs unchanged (only `CPU=` / auto-detection differs): `make`, `make test`,
      `calibrate`, ground-truth subset.
- [ ] **VM caveats handled and documented:**
  - performance counters may be partly or entirely unavailable in the guest → validate on time
    (and on whatever counters exist), as recorded in `hw.json` capabilities;
  - other tenants share the physical L3 and memory bandwidth → repeat runs at different times,
    report spread, and treat high variance as a finding rather than hiding it;
  - vCPUs are not guaranteed to map to fixed physical cores → pin to a vCPU, check repeatability;
  - cache sizes reported inside a VM can differ from the host's → rely on calibrated capacities.
- [ ] V6 results; decision maps from both machines side by side, with the boundaries that moved
      explained by the calibrated parameters that differ.

---

## 8. Phase 7: Report integration

| Report section | Material from this plan |
|---|---|
| Introduction / research question | "Given hardware, workload and data, which layout and parameters?" |
| Design and implementation | engine (§2), calibration (§4), cost model built on [1] with modern extensions (§5), advisor (§6) |
| Methodology | ground-truth runner, template set, metrics (§3, §7) |
| Results | earlier experiments + lookups + V1–V5 figures |
| Analysis | model ladder (a 2002 model vs modern extensions: which mechanisms matter), per-level miss predictions vs counters, decision maps, boundary formulas (e.g. DSM vs NSM near s ≈ 1/v) |
| Conclusions | when each layout wins; how boundaries move with hardware |

Key figures: predicted-vs-measured scatter; model ladder bar chart; decision maps (laptop vs
server); regret distribution vs baselines; lookup latency per layout.

---

## 9. Extensibility guide (how to add X)

| To add | Do this | Files |
|---|---|---|
| A layout (e.g. column groups) | subclass `Layout`, implement the operators, register in the factory; add its parameter grid; **describe its access patterns** (one function per operator) | `src/<name>.{h,cpp}`, `src/layout_factory.cpp`, `advisor/candidates.py`, `advisor/models/patterns/describe.py` |
| An operator (e.g. GROUP BY, update) | add an `Op` value and a `--query` key; reference implementation; one virtual in `Layout`, implemented per layout; **its access-pattern description** per layout | `src/query_spec.*`, `src/reference.cpp`, `src/layout.h`, layouts, `describe.py` |
| An access pattern (e.g. strided, skewed random) | new class with a `misses(level, state)` method | `advisor/models/patterns/basic.py` |
| A hardware effect (e.g. a new prefetcher rule) | a hook applied during pattern evaluation + calibration kernel | `advisor/models/patterns/extensions.py`, `src/calibrate.cpp` |
| A cache level or TLB level | add it to `hw.json`; the pattern evaluator loops over all levels | calibration only |
| An execution option (e.g. threads) | field in `ExecOptions` + CLI list flag; use it in the layouts; add to the candidate space | `src/exec_options.h`, `src/main.cpp`, `advisor/candidates.py` |
| A cost model | new class implementing `CostModel` with `@register_model("name")` | `advisor/models/<name>.py` |
| A search strategy | new class with `@register_search("name")` | `advisor/search/<name>.py` |
| A selectivity estimator | new class with `@register_estimator("name")` | `advisor/selectivity.py` or its own file |
| A data distribution | a generator registered in the engine (`--dist`) + a distribution model class (`pass_rate`, `run_length`, `branch_predictability`) in the advisor | `src/distributions.cpp`, `advisor/data_profile.py` |
| A machine | nothing in the code: run `calibrate` there to produce its `hw.json`; add a memory-controller PMU name to the counter table if you want DRAM traffic on new hardware | `configs/hw/`, optionally `src/perf_counters.cpp` |
| A workload template | a YAML entry or a generator rule | `advisor/workloads/` |
| A calibration benchmark | new kernel in `calibrate` + field in `hw.json` | `src/calibrate.cpp`, `advisor/hardware.py` |

Conventions: every component takes its configuration from files (YAML/JSON), not constants in
code; every run writes `machine.txt` and the exact config next to its results; every new engine
feature first gets a reference implementation and a test.

---

## 10. Planned repository layout

```
src/                      engine (+ query_spec, exec_options, layout_factory, reference, calibrate)
tests/                    engine_tests.cpp
advisor/
  __init__.py  cli.py  engine.py  hardware.py  data_profile.py  workload.py
  selectivity.py  candidates.py  report.py  validate.py  maps.py
  models/   base.py  registry.py  bytes_bw.py (v0)  manegold.py (v1)  modern.py (v2, v3)
            patterns/  regions.py  basic.py  combine.py  describe.py  evaluate.py  extensions.py
  search/   base.py  exhaustive.py
  workloads/ generate.py  examples/*.yaml
configs/                  hw/*.json  data/*.yaml  spaces/*.yaml
scripts/                  existing run_*/plot_* + run_lookups.sh, run_ground_truth.py
results/                  existing + lookups/  ground_truth/  validation/
```

---

## 11. Schedule

The estimate below is in **working sessions** with Claude doing the implementation, plus the
time that has to come from you or from the machines. Calendar time depends mostly on the second
and third columns, not on writing code.

| Phase | Claude's work | Your part | Machine time | Checkpoint |
|---|---|---|---|---|
| 1 | Engine refactor, portability fixes, distributions, tests | review | minutes | CP1 |
| 1b | Point lookups + lookup experiment | review plots | ~15 min (laptop idle) | CP2 |
| 2 | Ground-truth runner | start the run, keep the laptop idle and plugged in | 1–2 h | CP3 |
| 3 | Calibration tool | run it on the VM (copy repo, `make`, one command) | ~5 min per machine | CP4 |
| 4 | Pattern machinery, models v0–v3, fitting, accuracy analysis (iterative, the riskiest part) | review the go/no-go | minutes | CP5 |
| 5 | Advisor CLI, search, report | try your own workloads | — | CP6 |
| 6 | Validation V1–V5, decision maps | review | 30–60 min | CP7 |
| 7 | Second-machine validation | run the subset on the VM | 30–60 min | CP8 |
| 8 | Figures, tables, draft text for sections | write and edit the report | — | — |

Phases 1–3 and 5–6 are typically one session each; phase 4 may need two or three, because the
model has to be checked against data and corrected. Roughly 8–11 sessions, so about a week of
calendar time if you can review checkpoints and start runs promptly.

**Minimum convincing version** (if time is short): CP1 → CP2 → CP3 → CP5 (model v1, the paper's
model) → CP6 → V1, V2, V5 on the laptop. Everything else (v2/v3 extensions, second machine,
robustness) adds strength but isn't required for a complete story.

---

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Frequency scaling and laptop noise distort ground truth | AC power, `performance` preference, pinned P-core, repeatability check (CP3); report bytes/cycle where possible |
| DRAM counters are system-wide | idle machine; repeat runs; cross-check with the LLC-miss counter |
| Prefetcher behaviour is undocumented | fit the over-fetch term from data; optional prefetcher-off ablation (MSR, needs sudo) to confirm it |
| Overfitting the model | ≤ 8 constants with physical meaning; strict calibration/validation split; held-out families (V4) |
| Many near-ties make "accuracy" meaningless | count choices within 5% of the best as correct; report regret, not only accuracy |
| Ground-truth runtime and memory | 256 MB tables by default; one process per storage config; reuse measurements across workloads |
| Machine-specific assumptions creep back in | design rule 2; CP1 and CP8 both run the code on the second machine; `hw.json` capabilities drive behaviour |
| Hybrid CPUs (P/E cores) | auto-detect and pin to a performance core; open counters on that PMU |
| VMs: missing counters, noisy neighbours, unstable vCPU mapping | time-only validation where needed; repeated runs at different times; spread reported (CP8) |
| Second machine becomes unavailable | keep V6 optional; vary the laptop instead (THP on/off, later 1 vs N threads) to show boundaries moving |

---

## 13. Decisions (settled 2026-11-14)

| # | Question | Decision | Where it shows up |
|---|---|---|---|
| 1 | Second machine | AMD EPYC 9V74 KVM guest (5 vCPUs, 1 socket, 1 NUMA node) is available, but the model must not depend on any particular machine | design rule 2, §1.1, CP8 |
| 2 | Multithreading | single-threaded advisor now; `threads` reserved in `ExecOptions` and `bw_dram[threads]` in `hw.json`, with a documented path to add it | §2.2, §4, §15 |
| 3 | Data distributions | uniform + sorted/clustered first; any other distribution pluggable later (Zipf, correlated, custom files) | §2.6, §6.2, §9 |
| 4 | Zone maps | an extension, not in the first version | §15 |
| 5 | Accuracy targets | no instructor requirement; the targets in §5 and §7 are our own and are reported as such | §5, §7 |
| 6 | Deadline | open: decides whether to stop at the minimum convincing version (§11) | §11 |

---

## 14. References

1. S. Manegold, P. Boncz, M. L. Kersten. *Generic Database Cost Models for Hierarchical Memory
   Systems.* VLDB 2002. (`1287369.1287387.pdf` in the repository.) Basis of the hardware model
   (§4) and cost model (§5).
2. A. Ailamaki, D. J. DeWitt, M. D. Hill, M. Skounakis. *Weaving Relations for Cache
   Performance.* VLDB 2001. The PAX layout.
3. K. A. Ross. *Conjunctive Selection Conditions in Main Memory.* PODS 2002. Branchy vs
   branch-free selection; the branch-misprediction term.

---

## 15. Planned extensions (designed for, not in the first version)

| Extension | Hook already in the design |
|---|---|
| Multithreaded execution | `ExecOptions::threads`, `bw_dram[threads]`, per-thread CPU constants |
| Zone maps (min/max per PAX page or DSM block) with sorted/clustered data | distribution models expose `run_length`; a `skip` access pattern in the model; page size gains a real optimum |
| Zipf, correlated and custom distributions | distribution registries in the engine and the advisor |
| Updates and inserts | a new `Op` + reference implementation + access-pattern description |
| Column groups (hybrid layouts between NSM and DSM) | layout factory + candidate space + pattern descriptions |
| Mixed column widths | `Region.w` already per region; engine table generator by column type |
| Prefetcher-off ablation (Intel MSR) | optional calibration step, skipped where unsupported |
