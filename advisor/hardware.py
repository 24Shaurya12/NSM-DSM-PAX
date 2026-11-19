"""Hardware profile (hw.json) from calibration measurements (plan §4).

The memory system is modelled as in Manegold, Boncz & Kersten (VLDB 2002): a stack of cache
levels, with the TLB as one more "cache" whose line is the OS page. For every level we store
capacity C, line size B, sequential bandwidth b^s and random access latency of data served by
that level; the miss penalties l^s_{i+1} = B_i / b^s_{i+1} and l^r_{i+1} = lat_{i+1} - lat_i
follow. Modern additions: effective DRAM transfer unit B_eff, prefetch over-fetch table,
memory-level parallelism, CPU cycles per engine kernel, branch-miss cost, batch/page overheads.

Nothing is assumed about the machine: nominal cache sizes come from sysfs (when present), and
every bandwidth, latency, TLB reach and constant is derived from the curves measured by
build/calibrate. Missing counters (e.g. in a VM) degrade to documented fallbacks, flagged in
hw.json["fallbacks"].

  python3 -m advisor.hardware calibrate --out configs/hw/laptop.json   # measure + derive (~2-3 min)
  python3 -m advisor.hardware derive RAW.json --out hw.json             # re-derive only
"""
import argparse
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from .engine import REPO, _read, machine_state, pick_cpu

CALIBRATE = REPO / "build" / "calibrate"


# --------------------------------------------------------------------------- sysfs facts

def _size(text):
    text = text.strip()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    return int(text[:-1]) * mult[text[-1]] if text and text[-1] in mult else int(text or 0)


def sysfs_caches(cpu):
    """Data/unified caches of `cpu`, smallest first: [{level, bytes, line, ways, sets}]."""
    out = []
    for idx in sorted(Path(f"/sys/devices/system/cpu/cpu{cpu}/cache").glob("index*")):
        if _read(idx / "type") not in ("Data", "Unified"):
            continue
        out.append({"level": int(_read(idx / "level", "0")),
                    "bytes": _size(_read(idx / "size", "0")),
                    "line": int(_read(idx / "coherency_line_size", "64") or 64),
                    "ways": int(_read(idx / "ways_of_associativity", "0") or 0),
                    "sets": int(_read(idx / "number_of_sets", "0") or 0)})
    return sorted(out, key=lambda c: c["level"])


def cpu_model():
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "unknown"


# --------------------------------------------------------------------------- curve helpers

def _curve(records, xkey, ykey):
    pts = sorted((r[xkey], r[ykey]) for r in records if r.get(ykey) is not None)
    return np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float)


def _interp_log(x, xs, ys):
    return float(np.interp(math.log2(x), np.log2(xs), ys))


def _plateau(xs, ys, lo, hi, stat=np.median):
    sel = (xs >= lo) & (xs <= hi)
    return float(stat(ys[sel])) if sel.any() else float("nan")


def _knee(xs, ys, lo_val, hi_val, frac=0.5):
    """First x where y crosses lo_val + frac * (hi_val - lo_val) (curves rise with x)."""
    thr = lo_val + frac * (hi_val - lo_val)
    for x, y in zip(xs, ys):
        if y >= thr:
            return float(x)
    return float("nan")


# --------------------------------------------------------------------------- derivation

def derive(raw, cpu, caches=None, state=None):
    caches = sysfs_caches(cpu) if caches is None else caches
    fallbacks = []
    counters = set(raw.get("counters", []))
    have_dram = "dram_read_bytes" in counters

    # --- cache levels: plateaus between nominal capacities, knees from the curves
    seq_x, seq_bw = _curve(raw["seq_read"], "bytes", "gb_s")
    lat_src = raw.get("chase_huge") or raw["chase"]
    if not raw.get("chase_huge"):
        fallbacks.append("no huge-page chase: cache latencies include TLB misses")
    lat_x, lat_y = _curve(lat_src, "bytes", "ns")
    if not caches:
        fallbacks.append("no sysfs cache info: levels found from latency knees only")
        caches = _levels_from_knees(lat_x, lat_y)
    levels, prev = [], 0
    for i, c in enumerate(caches):
        lo = max(4096, prev * 2)
        hi = c["bytes"] / 2
        levels.append({"name": f"L{c['level']}", "bytes": c["bytes"], "line": c["line"],
                       "ways": c["ways"], "sets": c["sets"],
                       "seq_gb_s": _plateau(seq_x, seq_bw, lo, hi),
                       "lat_ns": _plateau(lat_x, lat_y, lo, hi, np.min)})
        prev = c["bytes"]
    dram_lo = 4 * prev
    levels.append({"name": "DRAM", "bytes": None, "line": caches[-1]["line"] if caches else 64,
                   "seq_gb_s": _plateau(seq_x, seq_bw, dram_lo, math.inf),
                   "lat_ns": _plateau(lat_x, lat_y, dram_lo, math.inf)})
    for i, lv in enumerate(levels):
        if 0 < i:
            below = levels[i - 1]
            lv["miss_lat_ns"] = lv["lat_ns"] - below["lat_ns"]          # Manegold l^r of level i-1
            lv["seq_miss_ns"] = below["line"] / lv["seq_gb_s"]           # Manegold l^s of level i-1
        if lv["bytes"]:  # effective capacity: where latency is halfway to the next plateau
            nxt = levels[i + 1]["lat_ns"]
            lv["effective_bytes"] = _knee(lat_x, lat_y, lv["lat_ns"], nxt)

    # --- TLB as a cache: one line per page vs the dense chase over as many lines
    tlb = {}
    if raw.get("tlb"):
        dense_x, dense_y = _curve(raw["chase"], "bytes", "ns")
        pages = np.array([r["pages"] for r in raw["tlb"]], float)
        extra = np.array([r["ns"] - _interp_log(max(r["lines_bytes"], dense_x[0]), dense_x, dense_y)
                          for r in raw["tlb"]])
        walks = np.array([r.get("dtlb_misses", np.nan) for r in raw["tlb"]])
        page_size = int(raw.get("os_page_size", 4096))
        first_lvl_reach = _knee(pages, extra, 0.0, float(np.max(extra)), 0.1)
        if not np.isnan(walks).all():
            stlb_reach = float(pages[np.argmax(walks > 0.5)]) if (walks > 0.5).any() else math.nan
        else:
            fallbacks.append("no dTLB counter: last-level TLB reach from the latency knee")
            stlb_reach = _knee(pages, extra, 0.0, float(np.max(extra)), 0.5)
        big = pages >= 4 * stlb_reach if not math.isnan(stlb_reach) else pages >= pages.max() / 2
        tlb = {"page": page_size,
               # the previous measured count is the last one that still fits
               "l1_entries": float(pages[max(0, np.searchsorted(pages, first_lvl_reach) - 1)])
               if not math.isnan(first_lvl_reach) else None,
               "l2_entries": float(pages[max(0, np.searchsorted(pages, stlb_reach) - 1)])
               if not math.isnan(stlb_reach) else None,
               "l1_miss_ns": _plateau(pages, extra, first_lvl_reach, stlb_reach / 2)
               if not math.isnan(stlb_reach) else None,
               "walk_ns": float(np.median(extra[big])) if big.any() else None,
               "curve": [{"pages": int(p), "extra_ns": round(float(e), 3),
                          "walks_per_load": None if np.isnan(w) else round(float(w), 4)}
                         for p, e, w in zip(pages, extra, walks)]}

    # --- memory-level parallelism
    mlp = {f"{r['page']}:{r['bytes']}": round(r["mlp"], 3) for r in raw.get("mlp", [])}
    dram_mlp = [r["mlp"] for r in raw.get("mlp", []) if r["bytes"] == max(
        x["bytes"] for x in raw["mlp"]) and r["page"] == "2M"] or \
        [r["mlp"] for r in raw.get("mlp", []) if r["bytes"] == max(x["bytes"] for x in raw["mlp"])]

    # --- effective transfer unit and over-fetch (need the DRAM byte counter)
    line = levels[0]["line"]
    stride = sorted(raw.get("stride", []), key=lambda r: r["stride"])
    if have_dram and stride:
        bpa = {int(r["stride"]): r["dram_read_bytes"] for r in stride}
        sparse = [v for s, v in bpa.items() if 256 <= s <= 2048]
        b_eff = float(np.median(sparse)) if sparse else float(line)
        overfetch = [{"period": int(r["period"]), "run": int(r["run"]), "offset": int(r["offset"]),
                      "dram_bytes": round(r["dram_read_bytes"], 1),
                      "extra_bytes": round(r["dram_read_bytes"] - r["run"], 1),
                      "ns": round(r["ns_per_run"], 2)} for r in raw.get("overfetch", [])]
    else:
        fallbacks.append("no DRAM byte counter: B_eff = cache line, over-fetch table empty")
        bpa, b_eff, overfetch = {}, float(line), []

    # --- CPU constants from the engine kernels (cycles per row / per value)
    k = {r["name"]: r for r in raw.get("kernels", [])}
    cpu_consts, freq = {}, float("nan")
    if k:
        have_cycles = all("cycles" in r for r in k.values())
        if have_cycles:
            freq = float(np.median([r["cycles"] / r["ns_per_row"] for r in k.values()]))
        else:
            fallbacks.append("no cycle counter: CPU constants are in ns, not cycles")

        def cyc(name):
            r = k[name]
            return r["cycles"] if have_cycles else r["ns_per_row"]

        def br(name):
            return k[name].get("branch_misses")

        c = cpu_consts
        c["c_scan"] = cyc("dsm_scan_l1")
        c["c_scan_l2"] = cyc("dsm_scan_l2")
        c["c_pred"] = cyc("dsm_pred0_branchy")
        c["c_pred_branchless"] = cyc("dsm_pred0_branchless")
        c["c_select_sum"] = cyc("dsm_pred100_branchy") - cyc("dsm_pred0_branchy")
        c["c_select_sum_branchless"] = cyc("dsm_pred100_branchless") - cyc("dsm_pred0_branchless")
        c["o_batch"] = (cyc("dsm_pred0_batch8") - cyc("dsm_pred0_branchy")) / (1 / 8 - 1 / 1024)
        c["c_gather_k1"] = cyc("dsm_mat100_k1") - cyc("dsm_pred0_branchy")
        c["c_gather_value"] = (cyc("dsm_mat100_k16") - cyc("dsm_mat100_k1")) / 15
        c["c_sum_value"] = (cyc("dsm_sum100_k16") - cyc("dsm_pred100_branchy")) / 15
        c["c_nsm_row"] = cyc("nsm_scan_k1")
        c["c_nsm_value"] = (cyc("nsm_scan_k16") - cyc("nsm_scan_k1")) / 15
        c["c_nsm_pred"] = cyc("nsm_pred0")
        c["c_copy_tuple_k16"] = cyc("nsm_mat100_k16")
        c["c_copy_tuple_k1"] = cyc("nsm_mat100_k1")
        # branch misses: 50% vs 0% on 8 MB of random data, minus the extra selection work
        d_br = (br("dsm_pred50_big") or 0) - (br("dsm_pred0_big") or 0)
        if br("dsm_pred50_big") is None:
            fallbacks.append("no branch-miss counter: assumed 0.5 misses/row at 50%")
            d_br = 0.5
        work = cyc("dsm_pred50_big") - cyc("dsm_pred0_big") - 0.5 * c["c_select_sum"]
        c["c_branch_miss"] = work / d_br if d_br > 0.05 else float("nan")
        c["branch_misses_per_row_50"] = br("dsm_pred50_big")
        c["branch_misses_per_row_10"] = br("dsm_pred10_big")
        # PAX: cycles per row = a + o_page / T, T = tuples per page (16 columns of 8 bytes)
        for prefix, key in (("pax_scan_k1_", "pax_scan"), ("pax_pred0_", "pax_pred"),
                            ("pax_mat100_k16_", "pax_mat_k16")):
            pts = [(int(n[len(prefix):]) / 128, cyc(n)) for n in k if n.startswith(prefix)]
            if len(pts) >= 2:
                T = np.array([p[0] for p in pts])
                y = np.array([p[1] for p in pts])
                slope, icpt = np.polyfit(1 / T, y, 1)
                c[f"{key}_per_row"] = float(icpt)
                c[f"{key}_o_page"] = float(slope)
        cpu_consts = {kk: (None if v is None or (isinstance(v, float) and math.isnan(v))
                           else round(float(v), 3)) for kk, v in c.items()}

    dram = levels[-1]
    return {
        "schema": 1,
        "created": datetime.now().isoformat(timespec="seconds"),
        "machine": {"cpu_model": cpu_model(), "pinned_cpu": cpu, **(state or {})},
        "capabilities": {
            "counters": sorted(counters),
            "dram_counters": have_dram,
            "hybrid": Path("/sys/devices/cpu_core").exists(),
            "thp": _read("/sys/kernel/mm/transparent_hugepage/enabled", "unknown"),
            "huge_pages_obtained": int(raw.get("anon_huge_kb", -1)) > 0,
            "os_page_size": int(raw.get("os_page_size", 4096)),
        },
        "freq_ghz": round(freq, 3) if not math.isnan(freq) else None,
        "levels": [{kk: (round(v, 4) if isinstance(v, float) and not math.isnan(v) else
                         (None if isinstance(v, float) else v)) for kk, v in lv.items()}
                   for lv in levels],
        "tlb": tlb,
        "dram": {"seq_gb_s": round(dram["seq_gb_s"], 3), "lat_ns": round(dram["lat_ns"], 2),
                 "B_eff": round(b_eff, 1), "mlp": round(float(np.median(dram_mlp)), 2)
                 if dram_mlp else None,
                 "bw_by_threads": {"1": round(dram["seq_gb_s"], 3)}},
        "mlp": mlp,
        "dram_bytes_per_strided_access": {str(s): round(v, 1) for s, v in bpa.items()},
        "overfetch": overfetch,
        "cpu": cpu_consts,
        "fallbacks": fallbacks,
    }


def _levels_from_knees(xs, ys):
    """Without sysfs: a level ends wherever latency jumps by >= 1.6x between neighbours."""
    levels, lvl = [], 1
    for i in range(1, len(xs)):
        if ys[i] >= 1.6 * ys[i - 1]:
            levels.append({"level": lvl, "bytes": int(xs[i - 1]), "line": 64, "ways": 0,
                           "sets": 0})
            lvl += 1
    return levels


# --------------------------------------------------------------------------- CLI

def calibrate(out, raw_out=None, max_size="1G", cpu=None, samples=None):
    cpu = pick_cpu() if cpu is None else cpu
    subprocess.run(["make", "-s", "build/calibrate"], cwd=REPO, check=True)
    state = machine_state(cpu)
    cmd = ["taskset", "-c", str(cpu), str(CALIBRATE), "--max-size", max_size]
    if samples:
        cmd += ["--samples", str(samples)]
    proc = subprocess.run(cmd,
                          capture_output=True, text=True, check=True)
    raw = json.loads(proc.stdout)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if raw_out:
        Path(raw_out).write_text(proc.stdout)
    hw = derive(raw, cpu, state=state)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(hw, indent=2))
    return hw


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m advisor.hardware")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("calibrate", help="run build/calibrate and write hw.json")
    p.add_argument("--out", required=True)
    p.add_argument("--raw", help="also keep the raw calibration JSON here")
    p.add_argument("--max-size", default="1G")
    p.add_argument("--samples", type=int, help="samples per kernel (default 5)")
    p = sub.add_parser("derive", help="re-derive hw.json from a raw calibration JSON")
    p.add_argument("raw")
    p.add_argument("--out", required=True)
    p.add_argument("--cpu", type=int)
    args = ap.parse_args(argv)
    if args.cmd == "calibrate":
        hw = calibrate(args.out, args.raw or str(Path(args.out).with_suffix(".raw.json")),
                       args.max_size, samples=args.samples)
    else:
        raw = json.loads(Path(args.raw).read_text())
        cpu = pick_cpu() if args.cpu is None else args.cpu
        hw = derive(raw, cpu)
        Path(args.out).write_text(json.dumps(hw, indent=2))
    print(json.dumps({k: hw[k] for k in ("freq_ghz", "dram", "fallbacks")}, indent=2))
    for lv in hw["levels"]:
        print(f"  {lv['name']:5s} {lv['bytes'] or '-':>10} B  seq {lv['seq_gb_s']} GB/s  "
              f"lat {lv['lat_ns']} ns")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
