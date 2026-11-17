#!/usr/bin/env python3
"""Runs the benchmark suite behind the report: every experiment is a list of scan_bench
(or membench) invocations, each writing one CSV under results/<experiment>/.

Usage:
  experiments.py [--list] [--names] [--only E1,E2] [--force] [--cpu C] [--quick]

  --list    print the experiments and their number of runs, and exit
  --names   print only the experiment names, one line (used by the Makefile)
  --only    run only these experiments (default: all, in order)
  --force   re-run runs whose CSV already exists (default: skip them, so an interrupted
            suite resumes where it stopped)
  --cpu     core to pin every run to with taskset (default 2)
  --quick   1/8 of the rows and fewer repetitions, to check that everything runs
  --no-summary  don't print each run's results (by default every run, also a skipped one,
            is followed by a table of median latency per configuration and strategy)
  --detail  print each scan_bench run with scripts/summarize.py instead: median, min and max
            latency, and the hardware counters per row (instructions/row, L1D misses/row, ...)
            for the runs that recorded them (--perf)

Every run's stderr (table sizes, footprints, correctness check) is kept next to its CSV as
<name>.log. A run spec is (name, args[, executable[, extension]]); batchbench prints a text
table, so its runs are saved as <name>.txt. Figures are drawn separately by scripts/figures.py.
"""
import argparse
import os
import subprocess
import sys
import time
import functools

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brief import brief  # noqa: E402

print = functools.partial(print, flush=True)  # progress shows up in redirected logs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "build", "scan_bench")
MEMBENCH = os.path.join(ROOT, "build", "membench")
BATCHBENCH = os.path.join(ROOT, "build", "batchbench")
RESULTS = os.path.join(ROOT, "results")

# Base table: 4M rows x 16 int64 columns = 512 MB per layout, 32x the 16 MB L3. Uniform
# values in [0, 10^6), so a predicate's selectivity can be set in steps of 10^-6.
ROWS = 4_000_000
COLS = 16
DOMAIN = 1_000_000
REPS = ["--iters", "5", "--rounds", "3", "--warmup", "1"]
SELS = "0.0001,0.001,0.005,0.01,0.02,0.05,0.1,0.2,0.5,1"


def base(rows=ROWS, cols=COLS, domain=DOMAIN):
    # DSM columns are staggered across L1 sets in every experiment (--dsm-offset auto), except
    # where the placement is what is measured (dsm_knobs, aliasing, untuned_*), which pass
    # --dsm-offset explicitly (the last value wins). Page-aligned columns make row-at-a-time
    # reconstruction of more than 8 attributes thrash one L1 set, 3x slower: an allocation
    # artifact, not a property of the layout.
    return (["--rows", str(rows), "--cols", str(cols), "--domain", str(domain)] + REPS
            + ["--dsm-offset", "auto"])


def experiments(quick):
    rows = ROWS // 8 if quick else ROWS
    b = lambda **kw: base(rows=kw.pop("rows", rows), **kw)  # noqa: E731
    exps = {}

    # 0. The machine: latency and bandwidth per working-set size.
    exps["machine"] = [("membench", ["--max-mb", "64" if quick else "1024"], MEMBENCH)]

    # 1. Projectivity: no WHERE clause, k of N columns.
    exps["projectivity"] = [
        ("sum", b() + ["--query", "sum", "--strategy", "early,late", "--perf", "core"]),
        ("projection", b() + ["--query", "projection", "--strategy", "early,late",
                              "--perf", "core"]),
        ("projection_mem", b() + ["--query", "projection", "--strategy", "early,late",
                                  "--perf", "mem"]),
    ]

    # 2. Selectivity x materialization strategy, for three projectivities.
    exps["selectivity"] = [
        (f"projection_k{k}", b() + ["--query", "projection", "--k", str(k), "--sel", SELS,
                                    "--perf", "core"])
        for k in (2, 4, 16)
    ] + [
        ("sum_k4", b() + ["--query", "sum", "--k", "4", "--sel", SELS, "--perf", "core"]),
        # Two predicates: c0 < x (swept) AND c1 < 500000 (50%).
        ("projection_k8_2pred", b() + ["--query", "projection", "--k", "8", "--sel", SELS,
                                       "--where", "c1<500000"]),
    ]

    # 3. Table width at a fixed table size (512 MB): N = 2 .. 256 columns.
    width = []
    for n in (2, 4, 8, 16, 32, 64, 128, 256):
        r = rows * 16 // n
        width.append((f"width_n{n}", b(rows=r, cols=n) + [
            "--query", "sum,projection", "--k", f"1,{n}", "--sel", "0.01,1",
            "--strategy", "early,late", "--page-size", str(max(16384, n * 8 * 64))]))
    exps["width"] = width

    # 4. Working-set size: 16 KB .. 512 MB tables (N = 16), cache-resident ones repeated so
    #    each sample lasts >= 20 ms.
    ws = []
    for kb in (16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536,
               131072, 262144, 524288):
        r = kb * 1024 // (COLS * 8)
        if quick and kb > 65536:
            continue
        ws.append((f"ws_{kb}k", base(rows=r) + [
            "--query", "sum,projection", "--k", "1,4,16", "--strategy", "early,late",
            "--min-time-ms", "20", "--page-size", "4096" if kb <= 16 else "16384"]))
    exps["workingset"] = ws

    # 5. PAX page size, one process per size (each PAX copy is a full table).
    pax = []
    for ps in (1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144, 1048576, 4194304):
        # pad "auto": minipages of an odd number of cache lines (distinct L1 sets).
        for pad in (0, 64, "auto"):
            if pad != "auto" and ps < COLS * (pad + 8):  # the padding alone would fill the page
                continue
            pax.append((f"pax_{ps}_pad{pad}", b() + [
                "--layout", "pax", "--page-size", str(ps), "--pax-pad", str(pad),
                "--query", "sum,projection", "--k", "1,16", "--sel", "0.01,1",
                "--strategy", "early,late", "--perf", "core"]))
    exps["pax"] = pax

    # 6. Encodings: compression and scan speed per distribution (k = N), N = 8.
    # Each also with --simd: AVX2 bit-unpacking (and fused unpack-and-add for SUM).
    enc = []
    for dist in ("uniform", "wide", "sorted", "runs", "zipf"):
        for simd in (False, True):
            enc.append((f"enc_{dist}" + ("_simd" if simd else ""), base(rows=rows, cols=8, domain=100) + [
                "--dist", dist, "--layout", "dsm", "--encoding", "all", "--query", "sum,projection",
                "--k", "8", "--strategy", "early,late"] + (["--simd"] if simd else [])))
    exps["encodings"] = enc

    # 7. Encodings x late materialization: predicates on compressed columns.
    exps["enc_selectivity"] = [
        (f"encsel_{dist}" + ("_simd" if simd else ""), base(rows=rows, cols=8, domain=1000) + [
            "--dist", dist, "--layout", "dsm", "--encoding", "plain,bitpack,for,dict,dictb,rle",
            "--mode", "vector", "--query", "sum,projection", "--k", "4", "--sel",
            "0.001,0.01,0.05,0.1,0.2,0.5,1", "--strategy", "early,late"]
         + (["--simd"] if simd else []))
        for dist in ("uniform", "sorted", "runs") for simd in (False, True)
    ]

    # 8. Encodings x working-set size: does compression pay off in cache, or only from DRAM?
    encws = []
    for kb in (64, 256, 1024, 4096, 16384, 65536, 262144):
        r = kb * 1024 // (8 * 8)
        for simd in (False, True):
            encws.append((f"encws_{kb}k" + ("_simd" if simd else ""), base(rows=r, cols=8, domain=100) + [
                "--layout", "dsm", "--encoding", "bitpack,for,dict,dictb", "--mode", "vector",
                "--query", "sum", "--k", "8", "--strategy", "early,late", "--min-time-ms", "20"]
                + (["--simd"] if simd else [])))
    exps["enc_workingset"] = encws

    # 8b. Dictionary codes, bit-packed (dict) or byte-aligned (dictb): a predicate on the codes,
    # SUM(c0) and SUM(c0..c3) WHERE c0 < x, on Zipf data (10,000 distinct values, 2-byte codes).
    exps["enc_dict"] = [
        ("encdict_zipf" + ("_simd" if simd else ""), base(rows=rows, cols=8) + [
            "--dist", "zipf", "--layout", "dsm", "--encoding", "dict,dictb", "--mode", "vector",
            "--query", "sum", "--k", "1,4", "--sel", "0.0005,0.001,0.005,0.01,0.05,0.1,0.5",
            "--strategy", "early,late"] + (["--simd"] if simd else []))
        for simd in (False, True)
    ]

    # 9. Vector (batch) size: late materialization and vectorized decoding.
    batches = "64,128,256,512,1024,2048,4096,8192,16384,65536"
    exps["batch"] = [
        ("batch_proj", b() + ["--layout", "dsm,pax", "--query", "projection", "--k", "4,16",
                              "--sel", "0.1,1", "--strategy", "late", "--batch", batches,
                              "--page-size", "1048576", "--perf", "core"]),
        ("batch_enc", base(rows=rows, cols=8, domain=100) + [
            "--layout", "dsm", "--encoding", "bitpack,dict", "--mode", "vector",
            "--query", "sum,projection", "--k", "8", "--strategy", "early",
            "--batch", batches, "--perf", "core"]),
    ]

    # 10. Tuning knobs for tuple reconstruction: DSM column offsets x huge pages.
    knobs = []
    for off in ("0", "auto"):
        for hp in (False, True):
            name = f"dsm_off{off}_{'huge' if hp else '4k'}"
            knobs.append((name, b() + ["--layout", "dsm", "--dsm-offset", off,
                                       "--query", "sum,projection", "--k", "1,2,4,8,12,16",
                                       "--strategy", "early,late", "--perf", "core"]
                          + (["--hugepages"] if hp else [])))
    exps["dsm_knobs"] = knobs

    # 10b. Cache-set conflicts between DSM columns: column c starts c * B bytes into its page
    #      (columns are page-aligned mmaps, so B = 0 puts value r of every column in one L1
    #      set). Early SELECT * reads 16 columns in lockstep. 1088 = 17 lines is what
    #      --dsm-offset auto picks. One process per B (the _r0 suffix is kept so older results
    #      with a second process, _r1, still plot as extra dots).
    exps["aliasing"] = [
        (f"alias_{st}_r0", b() + ["--layout", "dsm", "--query", "projection", "--k", "16",
                                  "--sel", "0.01", "--strategy", "early,late", "--perf", "core",
                                  "--dsm-offset", st])
        for st in ("0", "64", "512", "1024", "1088", "2048", "3072", "4096", "8192", "16384",
                   "32768")
    ]

    # 11. Materializing the output: column-at-a-time writes vs a cache-resident stage copied
    #     out with normal or non-temporal stores.
    exps["output_knobs"] = [
        (f"out_{name}", b() + ["--query", "projection", "--k", "4,16", "--sel", "0.01,0.1,0.5,1",
                               "--strategy", "early,late", "--perf", "mem"] + flags)
        for name, flags in (("direct", []), ("stage", ["--stage"]), ("nt", ["--nt-stores"]))
    ]

    # 12. The selection vector with AVX2, from DRAM and from cache.
    simd = []
    for tag, r, extra in (("dram", rows, []), ("l2", 2048, ["--min-time-ms", "10"]),
                          ("l3", 65536, ["--min-time-ms", "10"])):
        for on in (False, True):
            simd.append((f"simd_{tag}_{'on' if on else 'off'}", base(rows=r) + [
                "--layout", "dsm,pax", "--query", "sum,projection", "--k", "4", "--sel", SELS,
                "--strategy", "late,late-branch,early,early-pipe"] + extra + (["--simd"] if on else [])))
    exps["simd"] = simd

    # 13. Selectivity crossovers when the table fits in the L3 cache (4 MB) instead of DRAM:
    #     the same four queries as `selectivity`, then on DSM again with the AVX2 selection
    #     vector (cache_simd_*), so every cell of the crossover table comes from one table size.
    cache = lambda: base(rows=32768) + ["--sel", SELS, "--min-time-ms", "10"]  # noqa: E731
    queries = [(f"projection_k{k}", ["--query", "projection", "--k", str(k)]) for k in (2, 4, 16)]
    queries.append(("sum_k4", ["--query", "sum", "--k", "4"]))
    exps["selectivity_cache"] = [
        (f"cache_{name}", cache() + q + (["--page-size", "16384"] if "projection" in name else []))
        for name, q in queries
    ] + [
        (f"cache_simd_{name}", cache() + q + ["--layout", "dsm", "--strategy", "early,late",
                                              "--simd"])
        for name, q in queries
    ]

    # 14. The auto strategy against the fixed ones, on uniform data and on sorted data (where
    #     the qualifying rows are clustered, so the selectivity differs from vector to vector).
    #     "_measure": auto times every vector and keeps the cheaper strategy per selectivity
    #     range (--auto-measure). "cache_": the same with the table in L3 (32K rows, 4 MB).
    exps["auto"] = [
        (f"auto_{q}_k{k}_{dist}{suffix}", b() + ["--dist", dist, "--query", q, "--k", str(k),
                                                 "--sel", SELS, "--strategy", "early,late,auto"]
         + flags)
        for suffix, flags in (("", []), ("_measure", ["--auto-measure"]))
        for dist in ("uniform", "sorted") for q, k in (("projection", 4), ("projection", 16),
                                                       ("sum", 4))
    ] + [
        (f"auto_cache_{q}_k{k}{suffix}", base(rows=32768) + [
            "--query", q, "--k", str(k), "--sel", SELS, "--strategy", "early,late,auto",
            "--min-time-ms", "10"] + flags)
        for suffix, flags in (("", []), ("_measure", ["--auto-measure"]))
        for q, k in (("projection", 4), ("projection", 16), ("sum", 4))
    ]

    # 15. Predicate shapes: equality and ranges on one or more attributes (domain 100, so
    #     c=v selects 1%), for every layout and strategy.
    preds = [("eq1", "c0=42"), ("eq2", "c0=42&c1=7"), ("range1", "c2=10..29"),
             ("range2", "c2=10..29&c5<50"), ("eq_range", "c3=5&c6=0..49")]
    exps["predicates"] = [
        (f"pred_{name}", base(rows=rows, domain=100) + [
            "--query", "sum,projection", "--k", "4,16", "--where", w, "--perf", "core"])
        for name, w in preds
    ]

    # 16. Untuned vs tuned engine on representative queries.
    queries = [("sum_k1", ["--query", "sum", "--k", "1"]),
               ("sum_k16", ["--query", "sum", "--k", "16"]),
               ("proj_k16", ["--query", "projection", "--k", "16"]),
               ("proj_k4_s1", ["--query", "projection", "--k", "4", "--sel", "0.01"]),
               ("proj_k16_s10", ["--query", "projection", "--k", "16", "--sel", "0.1"]),
               ("proj_k16_s50", ["--query", "projection", "--k", "16", "--sel", "0.5"])]
    tuned_flags = ["--batch", "auto", "--dsm-offset", "auto", "--hugepages", "--nt-stores",
                   "--simd", "--auto-measure"]
    tuned = []
    for qn, qargs in queries:
        tuned.append((f"untuned_{qn}", b() + qargs + ["--strategy", "early,early-pipe,late,late-branch",
                                                      "--dsm-offset", "0"]))
        # PAX: 256 KB pages (16 KB minipages) were the best all-round size in the pax
        # experiment, with minipages of an odd number of lines (explicit padding never helped).
        tuned.append((f"tuned_{qn}", b() + qargs + tuned_flags + ["--strategy", "early,late,auto",
                                                                  "--page-size", "262144",
                                                                  "--pax-pad", "auto"]))
    exps["tuned"] = tuned

    # 17. Where late materialization's time goes (DSM projection from DRAM): the strategies
    #     for k = 1..16 around the crossovers, the first pass alone (k = 1 at 0.01%) with the
    #     scalar and the AVX2 selection vector, and one gathered column per touched line.
    lb_sels = "0.0001,0.01,0.024,0.05,0.074,0.1"
    exps["late_breakdown"] = [
        ("sweep", b() + ["--layout", "dsm", "--query", "projection", "--k", "1,2,4,8,16",
                         "--sel", lb_sels, "--strategy", "early,late"]),
        ("first_pass_scalar", b() + ["--layout", "dsm", "--query", "projection", "--k", "1,16",
                                     "--sel", "0.0001,0.05", "--strategy", "early,late",
                                     "--no-check"]),
        ("first_pass_avx2", b() + ["--layout", "dsm", "--query", "projection", "--k", "1,16",
                                   "--sel", "0.0001,0.05", "--strategy", "early,late", "--simd",
                                   "--no-check"]),
    ] + [
        (f"gather_per_column_{rep}", b() + ["--layout", "dsm", "--query", "projection",
                                            "--k", "2,4,8,16",
                                            "--sel", "0.001,0.01,0.024,0.05,0.074,0.1,0.2",
                                            "--strategy", "early,late"])
        for rep in (1, 2)
    ]

    # 18. Vector size vs work per value and fixed cost per vector (build/batchbench, a
    #     synthetic benchmark): which cache level a vector should fit in. MODE PARAM OVERHEAD_US.
    exps["batch_demo"] = [
        (f"{mode}_{param}_{over}", [mode, str(param), str(over)], BATCHBENCH, ".txt")
        for mode, param, over in (("probe", 4, 0), ("probe", 4, 2), ("pipe", 1, 0),
                                  ("pipe", 1, 20), ("pipe", 6, 20), ("passes", 8, 0))
    ]

    # 19. The auto threshold with constants for the data's location (--auto-cache), against
    #     the DRAM constants and against measuring (--auto-measure), with the table in L3.
    #     results/auto_cache_aware/summarize.py prints the comparison.
    exps["auto_cache_aware"] = [
        (f"{q}_k{k}_{mode}", base(rows=32768) + [
            "--query", q, "--k", str(k), "--sel", SELS, "--strategy", "early,late,auto",
            "--min-time-ms", "10"] + flags)
        for q, k in (("sum", 4), ("projection", 4), ("projection", 16))
        for mode, flags in (("threshold", []), ("cache", ["--auto-cache"]),
                            ("measure", ["--auto-measure"]))
    ]
    return exps


# How `--list` (`make experiments list`) groups and describes the experiments. Every
# experiment must appear here; the order inside a group is the order they run in.
SECTIONS = [
    ("Machine", [
        ("machine", "latency and read/write/streaming bandwidth vs working-set size (membench)"),
    ]),
    ("Layouts", [
        ("projectivity", "SUM and projection of k = 1..16 attributes, no predicate"),
        ("selectivity", "0.01%-100% selectivity, all four fixed strategies"),
        ("predicates", "equality, range and two-attribute predicates"),
        ("width", "N = 2..256 attributes at a fixed 512 MB"),
        ("workingset", "table size 16 KB - 512 MB, L1 to DRAM"),
        ("pax", "PAX page size 1 KB - 4 MB x minipage padding"),
    ]),
    ("Materialization", [
        ("late_breakdown", "late's first pass (scalar vs AVX2) and cost per gathered column"),
        ("selectivity_cache", "the selectivity sweep with the table in L3"),
        ("simd", "scalar vs AVX2 selection vector, from DRAM, L3 and L2"),
        ("auto", "auto (threshold, measured) vs early and late; uniform, sorted, in L3"),
        ("auto_cache_aware", "auto with DRAM vs in-cache constants vs measuring, in L3"),
    ]),
    ("Encodings", [
        ("encodings", "every encoding x 5 distributions, scalar and AVX2"),
        ("enc_selectivity", "predicates on compressed columns, early vs late"),
        ("enc_dict", "dict (bit-packed codes) vs dictb (byte codes) vs plain, Zipf data"),
        ("enc_workingset", "compression vs table size: does it pay in cache?"),
    ]),
    ("Tuning", [
        ("batch", "vector size 64 - 65536 rows: late projection, decoding"),
        ("batch_demo", "best vector size vs work and fixed cost per vector (batchbench)"),
        ("aliasing", "DSM column-start stagger 0 - 32 KB, incl. the auto 1088 B (L1 sets)"),
        ("dsm_knobs", "DSM column offsets x 4 KB / 2 MB pages"),
        ("output_knobs", "output writes: direct vs stage + memcpy vs stage + streaming"),
        ("tuned", "untuned defaults vs every machine-derived knob (the 1.46x)"),
    ]),
]


def print_list(exps):
    described = {name: (sec, text) for sec, items in SECTIONS for name, text in items}
    missing = [n for n in exps if n not in described]
    sections = SECTIONS + ([("Other", [(n, "") for n in missing])] if missing else [])
    total = 0
    for sec, items in sections:
        print(sec)
        for name, text in items:
            n = len(exps[name])
            total += n
            print(f"  {name:<18} {n:>3} run{'s' if n != 1 else ' '}   {text}")
        print()
    print(f"{len(exps)} experiments, {total} runs. Run some with: make experiments <name> ...")
    print("A run is one benchmark call with one set of flags; it writes one file under")
    print("results/<experiment>/ and sweeps many configurations inside it.")


def run(name, args, exe, outdir, cpu, force, ext=".csv"):
    csv_path = os.path.join(outdir, name + ext)
    log_path = os.path.join(outdir, name + ".log")
    if os.path.exists(csv_path) and not force:
        print(f"  {name} (already measured)")
        return "skipped"
    cmd = ["taskset", "-c", str(cpu), exe] + args
    if exe == BENCH:
        cmd += ["--output", csv_path + ".tmp"]
    t0 = time.time()
    with open(log_path, "w") as log:
        log.write(" ".join(cmd) + "\n")
        log.flush()
        if exe == BENCH:
            rc = subprocess.call(cmd, stdout=log, stderr=log)
        else:
            with open(csv_path + ".tmp", "w") as out:
                rc = subprocess.call(cmd, stdout=out, stderr=log)
    dt = time.time() - t0
    if rc != 0:
        print(f"  FAIL {name} (exit {rc}, {dt:.0f}s); see {log_path}")
        return False
    os.replace(csv_path + ".tmp", csv_path)
    print(f"  done {name} ({dt:.0f}s)")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--names", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--cpu", type=int, default=2)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-summary", action="store_true")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()

    exps = experiments(a.quick)
    if a.names:
        print(" ".join(exps))
        return
    if a.list:
        print_list(exps)
        return
    names = [n for n in a.only.split(",") if n] or list(exps)
    unknown = [n for n in names if n not in exps]
    if unknown:
        sys.exit(f"unknown experiments: {unknown}; see --list")
    ok = True
    skipped = 0
    for e in names:
        outdir = os.path.join(RESULTS + ("_quick" if a.quick else ""), e)
        os.makedirs(outdir, exist_ok=True)
        print(f"[{time.strftime('%H:%M:%S')}] {e}")
        for run_spec in exps[e]:
            name, args = run_spec[0], run_spec[1]
            exe = run_spec[2] if len(run_spec) > 2 else BENCH
            ext = run_spec[3] if len(run_spec) > 3 else ".csv"
            done = run(name, args, exe, outdir, a.cpu, a.force, ext)
            skipped += done == "skipped"
            done = bool(done)
            ok &= done
            if done and not a.no_summary:
                try:
                    out = os.path.join(outdir, name + ext)
                    if a.detail and exe == BENCH:
                        with open(out) as f:
                            text = subprocess.run(
                                [sys.executable, os.path.join(ROOT, "scripts", "summarize.py")],
                                stdin=f, capture_output=True, text=True, check=True).stdout
                        print("\n".join("      " + l for l in text.rstrip().splitlines()))
                    else:
                        brief(out)
                except Exception as e:  # a summary must never stop the suite
                    print(f"      (no summary: {e})")
                print()
    if skipped:
        print(f"{skipped} run{'s were' if skipped != 1 else ' was'} already measured and not "
              "re-run; add force=1 (--force) to re-run.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
