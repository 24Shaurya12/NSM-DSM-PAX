# NSM / DSM / PAX: scan engine and layout advisor

How to set up and run the scan engine (`build/scan_bench`, C++) and the layout advisor
(`python3 -m advisor`, Python), and how to vary every parameter.

Contents:
[Setup](#1-setup) ·
[Quick start](#2-quick-start) ·
[Engine](#3-the-engine-buildscan_bench) ·
[Sweeps](#4-experiment-sweeps-scriptsrun_sh) ·
[Advisor](#5-the-layout-advisor) ·
[Ground truth and validation](#6-ground-truth-and-validation) ·
[Modes](#7-summary-of-run-modes) ·
[Reproducing the results](#8-reproducing-the-report-results)

---

## 1. Setup

**Requirements**

| | Needed for | Notes |
|---|---|---|
| Linux, x86-64 | everything | uses `perf_event_open`, sysfs, `taskset`, `mmap` |
| `g++` with C++17 (GCC ≥ 9) and `make` | engine, tests, calibration | built with `-std=c++17 -O2 -march=native` |
| Python ≥ 3.8 with `numpy pandas scipy matplotlib pyyaml` | advisor, ground truth, plots | `pip install numpy pandas scipy matplotlib pyyaml` |
| `pdflatex` with the ACM class | building the report (optional) | `report/acmart.cls` is included |

**Build and test**

```bash
make                                   # build/scan_bench (engine) and build/calibrate
make test                              # engine correctness tests (~53k checks)
python3 tests/advisor/test_patterns.py # cost-model geometry tests
python3 tests/advisor/test_advisor.py  # advisor tests (parsers, estimators, search)
```

**Hardware counters (optional but recommended).** Latency is always measured. Cache, TLB and
branch counters need `perf_event_paranoid ≤ 2`. DRAM traffic (memory-controller counters, read
system-wide) needs `≤ 0`:

```bash
cat /proc/sys/kernel/perf_event_paranoid
sudo sysctl kernel.perf_event_paranoid=0        # until reboot
```

Counters that cannot be opened are left empty in the CSV; nothing fails.

**Measurement hygiene.** Keep the machine idle and on AC power. Every script pins the benchmark to
one core, chosen automatically: a performance core on hybrid Intel CPUs, otherwise the last
online CPU. Override with `CPU=<id>`.

---

## 2. Quick start

```bash
make
# SUM of 4 columns with a 1% filter, all three layouts, 256 MB table, from DRAM
./build/scan_bench --size 256M --cols 16 --query "op=sum;cols=0-3;where=c0<1" --modes cold

# Ask the advisor what to use for an example workload on a 4 GB, 64-column table
python3 -m advisor recommend --hw configs/hw/laptop_v2.json \
    --data configs/data/w64_8m.yaml --workload advisor/workloads/examples/wide_mixed.yaml
```

`configs/hw/laptop_v2.json` is the calibration of the machine we measured. On another machine,
calibrate first (§5.1); it takes under a minute.

---

## 3. The engine (`build/scan_bench`)

One run builds the table and the requested layouts, checks every result against a reference,
then times each layout × query × option and writes one CSV row per sample. Run
`./build/scan_bench --help` for the full list.

### 3.1 What you can vary

| Option | Values | Default |
|---|---|---|
| `--rows R` or `--size BYTES` | table size; `--size` accepts `K/M/G` (rows = bytes ÷ (cols × 8)) | 1,000,000 rows |
| `--cols N` | number of `int64` columns (table width) | 16 |
| `--dist T=SPEC` | value distribution, repeatable; `T` is `default` or a column `cN` | `uniform:lo=0:hi=99` |
| `--layout L1,L2,...` | `nsm`, `dsm`, `pax[:page=BYTES]` | all three |
| `--page-size B` | default PAX page size (a multiple of 64) | 16384 |
| `--query SPEC` | one query, repeatable (§3.2) | — |
| `--dsm-batch B1,...` | DSM rows per batch (selection-vector size) | 1024 |
| `--filter F1,F2` | `branchy` and/or `branchless` predicate evaluation | branchy |
| `--modes M1,M2` | `warm` and/or `cold` (§3.3) | warm |
| `--warmup W`, `--iterations I` | untimed runs, timed samples per configuration | 2, 10 |
| `--min-sample-ms T` | warm mode repeats the query until one sample lasts ≥ T ms | 20 |
| `--output FILE` | CSV destination | stdout |

Distributions:
- `uniform:lo=..:hi=..`: independent uniform integers.
- `sorted:lo=..:hi=..`: ascending along the table.
- `clustered:run=N:lo=..:hi=..`: runs of N equal values.

Example: `--dist default=uniform:lo=0:hi=99 --dist c0=sorted:lo=0:hi=999`.

Options that do not apply to a query are not swept. For example, `--dsm-batch` only affects DSM
filters and materialization, and `--filter` only affects queries with predicates. Lists produce
the cross product: layouts × queries × execution options × modes.

### 3.2 Query mini-language

```text
op=sum;cols=0-3;where=c0<5,c12<50          SELECT SUM(c0..c3) WHERE c0<5 AND c12<50
op=materialize;cols=all;where=c0<1         SELECT * WHERE c0<1 (tuple reconstruction)
op=sum;cols=0,4,8,12;where=none            SUM of any column subset, no filter
op=lookup;cols=all;n=10000;dist=uniform    10,000 point lookups (dist: uniform | sorted; seed=S)
```

- **`cols`:** `all`, a range `a-b`, a list `a,b,c`, or a mix.
- **`where`:** a comma-separated conjunction of `c3=42`, `c0<10`, `c0<=10`, `c0>90`, `c0>=90`,
  `c2=10..19`, or `none`. Predicates are evaluated in the order written.
- **`label=NAME`:** names the query in the CSV.
- **Legacy flags.** `--op sum,materialize --where "c0<10" --k 1,4,16` gives the cross product of
  ops × predicates × the first k columns. The sweep scripts use this form.

### 3.3 Measurement modes

| Mode | What is timed | Use it for |
|---|---|---|
| `warm` | the query repeated until a sample lasts ≥ `--min-sample-ms`; time per execution | cache-resident tables, CPU cost |
| `cold` | one execution right after the table is flushed from every cache level (`clflush`) | DRAM-resident behaviour, the ground truth |

The output CSV includes the layout, its parameters, rows, columns, query, op, projection,
predicate, execution options and mode. It also has the latency (`latency_ns`, per execution),
the measured selectivity, the result checksum, and nine counters: `cycles`, `instructions`,
`branch_misses`, `llc_refs`, `llc_misses`, `l1d_misses`, `dtlb_misses`, `dram_read_bytes`,
`dram_write_bytes`.

### 3.4 Examples

```bash
# Projectivity: SUM of k of 16 columns, 1 GB table, every layout, warm and cold
./build/scan_bench --size 1G --cols 16 --k 1,2,4,8,16 --modes warm,cold --output proj.csv

# Selectivity sweep on DSM with both filter modes and four batch sizes
./build/scan_bench --size 256M --layout dsm --op sum --k 4 \
    --where "c0<1" --where "c0<10" --where "c0<50" \
    --filter branchy,branchless --dsm-batch 256,1024,16K,128K

# PAX page sizes on a 64-column table, SELECT * at 1%
./build/scan_bench --size 1G --cols 64 --layout pax:page=4K,pax:page=64K,pax:page=1M,nsm,dsm \
    --query "op=materialize;cols=all;where=c0<1"

# Sorted data (predictable branches, contiguous qualifying rows)
./build/scan_bench --size 256M --dist c0=sorted:lo=0:hi=999 --query "op=sum;cols=0;where=c0<10"
```

---

## 4. Experiment sweeps (`scripts/run_*.sh`)

Each script runs one experiment, writes `raw.csv` and `machine.txt` (machine state, counters,
cache sizes) into its output folder, and has a matching `plot_*.py`. Every parameter is an
environment variable, so you change a sweep without editing the script:

```bash
SIZES="1M 64M 1G" LAYOUTS="dsm pax" scripts/run_working_set.sh
python3 scripts/plot_working_set.py results/working_set
```

| Script (plot) | Question | Variables (default) |
|---|---|---|
| `run_working_set.sh` (`plot_working_set.py`) | how do layouts behave as the table grows through L1/L2/L3/DRAM? | `SIZES` (16K … 1G), `KS` (1,2,4,8,16), `COLS` (16), `MODES` (warm,cold), `LAYOUTS`, `PAGE_SIZE` (16384), `ITERATIONS` (10) |
| `run_selectivity.sh` (`plot_selectivity.py`) | how does the pass rate change the winner? | `PERCENTS` (0 … 100), `SIZES` (1M 256M), `KS` (1,4,16), `COLS`, `MODES` (warm), `LAYOUTS`, `PAGE_SIZE` |
| `run_conjunction.sh` (`plot_conjunction.py`) | does predicate order matter? (`c0<S AND c12<50`, both orders) | `SELECTIVE_PERCENTS` (0 … 100), `OTHER` (c12<50), `SIZES` (1G), `KS`, `COLS`, `LAYOUTS` |
| `run_pagesize.sh` (`plot_pagesize.py`) | which PAX page size is best? | `PAGE_SIZES` (1K … 16M), `PREDICATES`, `OPS` (sum,materialize), `KS`, `SIZE` (1G), `COLS` |
| `run_pax_wins.sh` (`plot_pax_wins.py`) | when does PAX beat both NSM and DSM? (wide `SELECT *`) | `COLS` (64), `PAGE_SIZES` (4K … 1M), `PREDICATES`, `SIZE` (1G) |

All scripts also take `OUT` (output folder) and `CPU` (pinned core). Each takes 1–5 minutes with
the defaults.

---

## 5. The layout advisor

The advisor reads four inputs:

| Input | File | Made by |
|---|---|---|
| the machine | `hw.json` | `python3 -m advisor calibrate` (§5.1) |
| the data | `data.yaml` | you (§5.2) |
| the workload | `workload.yaml` | you (§5.2) |
| the candidate space | `space.yaml` | default `configs/spaces/default.yaml` (§5.3) |

### 5.1 Calibrate a machine

```bash
python3 -m advisor calibrate --out configs/hw/mymachine.json            # full (1 GB regions)
python3 -m advisor calibrate --out configs/hw/quick.json --max-size 512M --samples 3   # faster
```

The calibration runs on the pinned core and writes everything the advisor needs about the
machine into the JSON file. `--raw FILE` keeps the raw measurements, and
`python3 -m advisor derive RAW --out hw.json` re-derives `hw.json` from them.

### 5.2 Describe the data and the workload

```yaml
# data.yaml
rows: 8_000_000              # or: size: 4G
cols: 64
default: {dist: uniform, min: 0, max: 99}
columns:                     # per-column overrides
  c0: {dist: uniform, min: 0, max: 999}
  c1: {dist: clustered, run: 1024}
  c2: {dist: uniform, hist: {edges: [0, 10, 100], counts: [900, 100]}}   # optional histogram
```

```yaml
# workload.yaml
name: dashboard_mix
queries:
  - {name: q1, weight: 0.6, op: sum, cols: [c0, c1, c2, c3], where: "c0 < 5"}
  - {weight: 0.2, op: materialize, cols: all, where: "c0 < 1 AND c12 < 50"}
  - {weight: 0.2, op: lookup, cols: all, lookups: 10000, dist: uniform}
  - {weight: 0.1, op: sum, cols: "0-3", where: "c0 < 5", rates: [0.04]}   # known pass rate
```

- **`op`:** `sum`, `materialize` or `lookup`.
- **`cols`:** `all`, `[c0, c5]`, `"0-3"` or `"0,4,8"`.
- **`where`:** a conjunction joined by `AND` of `cN < v`, `<=`, `>`, `>=`, `= v`, `= a..b` or
  `BETWEEN a AND b`.
- **`weight`:** relative frequencies.

Examples are in `configs/data/` and `advisor/workloads/examples/` (`scan_heavy`, `lookup_heavy`,
`wide_mixed`).

### 5.3 Commands

```bash
H=configs/hw/laptop_v2.json; D=configs/data/w64_8m.yaml

# Recommend a storage configuration for a workload (text report; --json for scripts)
python3 -m advisor recommend --hw $H --data $D --workload advisor/workloads/examples/scan_heavy.yaml

# Predict one query on every storage configuration, with the best options for each
python3 -m advisor predict --hw $H --data $D --query "op=materialize;cols=all;where=c0<10"

# Decision maps: which layout wins over a grid (selectivity x projection, or lookups x selectivity)
python3 -m advisor maps --hw $H --data $D --grid selectivity-projection --op sum --out results/maps
```

`recommend` prints the chosen storage, the predicted time, the runner-up, the execution options of
each query and a sensitivity check (`--no-sensitivity` skips it).

Options common to the three commands:

| Option | Values | Default |
|---|---|---|
| `--model` | `v0`, `v1`, `v2`, `v3` (from the simplest to the full cost model) | `v3` |
| `--params FILE` | fitted model parameters (`params_v3.json`, written by validation, §6.2) | unfitted defaults |
| `--estimator` | `profile` (from data.yaml distributions), `histogram` (from `hist:`), `fixed` (the workload's `rates:`) | `profile` |
| `--objective` | `mean` (expected time per query) or `total` (sum of weighted times) | `mean` |
| `--space FILE` | candidate space (below) | `configs/spaces/default.yaml` |
| `--search` | search strategy | `exhaustive` |

**Candidate space.** Storage is chosen once per table, execution options once per query:

```yaml
# configs/spaces/default.yaml
storage:
  nsm: {}
  dsm: {}
  pax: {page: [1K, 2K, 4K, 8K, 16K, 64K, 256K, 1M, 4M, 16M]}
exec:
  dsm_batch: [256, 1024, 16384, 131072]
  filter: [branchy, branchless]
  predicate_order: [selective_first, selective_last]
```

Edit a copy and pass it with `--space` to add or remove page sizes, batch sizes or options.

---

## 6. Ground truth and validation

### 6.1 Ground truth: measure every candidate on every query

```bash
python3 scripts/run_ground_truth.py --dry-run              # list the jobs, nothing runs
python3 scripts/run_ground_truth.py                        # full run (~100 min; machine idle)
python3 scripts/run_ground_truth.py --sample 0.1 --name laptop_v1_repeat   # 10% repeat
python3 scripts/analyze_ground_truth.py results/ground_truth/laptop_v1     # winners, report.md
```

`configs/ground_truth/default.yaml` defines the run. Copy it to change what is measured:

| Key | What it controls | Default |
|---|---|---|
| `datasets` | name, `cols`, `size`, per-column `dist`; optional `families` | 16 and 64 columns uniform, 16 columns with sorted `c0`; 256 MB each |
| `templates` | query templates (`configs/templates/default.yaml`): projections and families (scans; one predicate at 10 pass rates; two predicates in both orders; lookups) | 134 per width |
| `space` | storage configurations and execution options | `configs/spaces/default.yaml` |
| `measure` | `modes`, `iterations`, `warmup` | cold, 5, 1 |

Options:
- `--datasets` and `--storage` restrict the run, for example
  `--datasets w16_uniform --storage nsm,dsm`.
- `--iterations` overrides the number of samples.
- `--out` and `--name` choose the store.

The run is **resumable**: an interrupted run continues where it stopped when you start the same
command again. The default space gives 36 engine runs and 10,368 points.

### 6.2 Validation: accuracy and decision quality

```bash
STORE=results/ground_truth/laptop_v1 HW=configs/hw/laptop_v2.json scripts/run_validation.sh
FAST=1 STORE=... HW=... scripts/run_validation.sh          # faster: 500 workloads, 4 perturbations
```

`run_validation.sh` does the following:
1. calibrates the machine, only if `HW` does not exist or `RECALIBRATE=1` is set;
2. runs the tests;
3. runs the accuracy check (`validate`) and the full evaluation (`evaluate`, below);
4. repeats the evaluation with a generalisation split;
5. produces the three example recommendations and the decision maps.

Outputs go to `results/validation/<store name>/`, or to `OUT`. Other variables: `FIT_DATASET`
(the data set used for the generalisation split, default `w16_uniform`).

The individual steps can also be run directly:

```bash
python3 -m advisor validate --store STORE --hw HW --out DIR      # accuracy per model rung
python3 -m advisor evaluate --store STORE --hw HW --out DIR \
    [--models v0,v1,v2,v3] [--fit v3] [--fit-split random:0.3 | dataset:w16_uniform:nolookup] \
    [--workloads 1000] [--seed 1] [--no-robustness | --quick]
```

`evaluate` writes `report.md` (accuracy, decisions against fixed rules, decision maps, robustness),
`params_v3.json` (pass it to `--params`), CSVs and plots.

---

## 7. Summary of run modes

| Choice | Options | Where |
|---|---|---|
| Cache state | `warm` (repeated, in cache if it fits) / `cold` (flushed before each run) | `scan_bench --modes` |
| Predicate evaluation | `branchy` / `branchless` | `scan_bench --filter`, space `exec.filter` |
| DSM batch | any row count | `scan_bench --dsm-batch`, space `exec.dsm_batch` |
| Predicate order | as written / `selective_first` / `selective_last` | `--query`, space `exec.predicate_order` |
| PAX page | any multiple of 64 B that holds one row | `--layout pax:page=…`, space `storage.pax.page` |
| Data distribution | `uniform`, `sorted`, `clustered` per column | `scan_bench --dist`, `data.yaml`, ground-truth `datasets` |
| Cost model | `v0` … `v3` | `advisor --model`, `evaluate --models` |
| Selectivity estimate | `profile`, `histogram`, `fixed` | `advisor --estimator` |
| Objective | `mean` / `total` | `advisor --objective` |
| Calibration | full (1 GB, 5 samples) / quick (`--max-size 512M --samples 3`) | `advisor calibrate` |
| Validation | full / `FAST=1` / `--quick` / `--no-robustness` | `run_validation.sh`, `evaluate` |
| Ground truth | full / `--sample F` / `--datasets` / `--storage` / `--dry-run` | `run_ground_truth.py` |

---

## 8. Reproducing the report results

Run these in order, with the machine idle and on AC power. Times are for our laptop (Intel
i7-1360P).

```bash
make && make test
python3 scripts/run_ground_truth.py                                  # 1. ground truth, ~100 min
python3 scripts/run_ground_truth.py --sample 0.1 --name laptop_v1_repeat   # 2. noise floor, ~10 min
python3 -m advisor ground-truth compare results/ground_truth/laptop_v1 results/ground_truth/laptop_v1_repeat
scripts/run_report.sh                                                # 3. sweeps + calibration + validation, ~15 min
```

`scripts/run_report.sh` runs, logging to `results/report/run.log`:
- build and tests;
- the five sweeps and their plots, into `results/report/<sweep>/`;
- a full calibration, into `configs/hw/laptop_v2.json`;
- the ground-truth analysis;
- full validation, into `results/report/validation/`.

Each stage's outcome is listed in `results/report/status.txt`.

**Building the report.** `cd report && pdflatex report.tex && pdflatex report.tex`. Figures are
in `report/figures/` and generated tables in `report/generated/`.
