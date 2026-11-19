"""Adapter between the advisor and the C++ engine (build/scan_bench).

It turns (storage configuration, data set, queries, execution-option grid, measurement
settings) into an engine command line, runs it pinned to one core, and returns the CSV as a
pandas DataFrame. Nothing here knows about a particular machine: the core is picked from sysfs
(same rule as scripts/common.sh) and every machine fact is read at run time.
"""
import io
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DEFAULT_BINARY = REPO / "build" / "scan_bench"

_SUFFIX = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}


def parse_size(text):
    """'256M' -> 268435456 (K/M/G suffixes, like the engine)."""
    text = str(text).strip()
    if text and text[-1].upper() in _SUFFIX:
        return int(float(text[:-1]) * _SUFFIX[text[-1].upper()])
    return int(text)


def format_size(n):
    for s in ("G", "M", "K"):
        if n >= _SUFFIX[s] and n % _SUFFIX[s] == 0:
            return f"{n // _SUFFIX[s]}{s}"
    return str(n)


# --------------------------------------------------------------------------- descriptions

@dataclass(frozen=True)
class StorageConfig:
    """A physical layout and its storage parameters, e.g. PAX with page=4096."""
    layout: str
    params: tuple = ()  # sorted ((name, value), ...) so the object is hashable

    @staticmethod
    def make(layout, **params):
        return StorageConfig(layout, tuple(sorted((k, str(v)) for k, v in params.items())))

    def spec(self):
        """Engine --layout text, e.g. 'pax:page=4096'."""
        return ":".join([self.layout] + [f"{k}={v}" for k, v in self.params])

    def key(self):
        """Short human-readable id, e.g. 'pax:page=4K'."""
        parts = [self.layout]
        for k, v in self.params:
            parts.append(f"{k}={format_size(int(v)) if v.isdigit() else v}")
        return ":".join(parts)

    def param(self, name, default=None):
        return dict(self.params).get(name, default)


@dataclass
class DataSpec:
    """A generated table: width, size and per-column value distributions."""
    name: str
    cols: int
    size: int = 0          # bytes (rows = size / (cols * 8)); or give rows
    rows: int = 0
    dist: dict = field(default_factory=dict)  # {'default': spec, 'c0': spec, ...}

    def __post_init__(self):
        if bool(self.size) == bool(self.rows):
            raise ValueError(f"data set {self.name}: give exactly one of size and rows")
        if self.size:
            self.rows = self.size // (self.cols * 8)
        else:
            self.size = self.rows * self.cols * 8

    @property
    def row_bytes(self):
        return self.cols * 8

    def column_spec(self, c):
        return self.dist.get(f"c{c}", self.dist.get("default", "uniform:lo=0:hi=99"))

    def engine_args(self):
        args = ["--rows", str(self.rows), "--cols", str(self.cols)]
        for target, spec in self.dist.items():
            args += ["--dist", f"{target}={spec}"]
        return args


@dataclass
class ExecGrid:
    """Execution options to sweep; the engine applies each only where it matters."""
    dsm_batch: list = field(default_factory=lambda: [1024])
    filter: list = field(default_factory=lambda: ["branchy"])

    def engine_args(self):
        return ["--dsm-batch", ",".join(str(parse_size(b)) for b in self.dsm_batch),
                "--filter", ",".join(self.filter)]


@dataclass
class Measurement:
    """How each (storage, query, options) point is timed.

    cold: the table is flushed from every cache before each timed execution (one execution per
    sample); warm: the query is repeated until a sample lasts min_sample_ms.
    """
    modes: list = field(default_factory=lambda: ["cold"])
    iterations: int = 5
    warmup: int = 1
    min_sample_ms: float = 20.0

    def engine_args(self):
        return ["--modes", ",".join(self.modes), "--iterations", str(self.iterations),
                "--warmup", str(self.warmup), "--min-sample-ms", str(self.min_sample_ms)]


# --------------------------------------------------------------------------- machine facts

def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def expand_cpu_list(text):
    cpus = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            cpus.extend(range(int(a), int(b) + 1))
        elif part:
            cpus.append(int(part))
    return cpus


def pick_cpu():
    """Same rule as scripts/common.sh: on hybrid CPUs the third performance core, otherwise
    the last online CPU. Overridable with the CPU environment variable."""
    if os.environ.get("CPU"):
        return int(os.environ["CPU"])
    p_cores = _read("/sys/devices/cpu_core/cpus")
    if p_cores:
        cpus = expand_cpu_list(p_cores)
        return cpus[2] if len(cpus) > 2 else cpus[-1]
    return expand_cpu_list(_read("/sys/devices/system/cpu/online", "0"))[-1]


def machine_state(cpu):
    """Conditions that can change timings, recorded with every engine run."""
    cpufreq = f"/sys/devices/system/cpu/cpu{cpu}/cpufreq"
    ac = [_read(p / "online") for p in Path("/sys/class/power_supply").glob("*")
          if _read(p / "type") == "Mains"]
    temps = []
    for z in Path("/sys/class/thermal").glob("thermal_zone*"):
        if _read(z / "type") in ("x86_pkg_temp", "k10temp", "cpu-thermal", "acpitz"):
            t = _read(z / "temp")
            if t.lstrip("-").isdigit():
                temps.append(int(t) / 1000)
    try:
        virt = subprocess.run(["systemd-detect-virt"], capture_output=True, text=True,
                              timeout=5).stdout.strip() or "none"
    except (OSError, subprocess.SubprocessError):
        virt = "unknown"
    return {
        "cpu": cpu,
        "ac_power": ac[0] if ac else "unknown",
        "governor": _read(f"{cpufreq}/scaling_governor", "unknown"),
        "epp": _read(f"{cpufreq}/energy_performance_preference", "unknown"),
        "cur_mhz": int(_read(f"{cpufreq}/scaling_cur_freq", "0") or 0) // 1000,
        "no_turbo": _read("/sys/devices/system/cpu/intel_pstate/no_turbo", "unknown"),
        "loadavg1": float(_read("/proc/loadavg", "0").split()[0]),
        "temp_c": max(temps) if temps else float("nan"),
        "perf_event_paranoid": _read("/proc/sys/kernel/perf_event_paranoid", "unknown"),
        "virt": virt,
    }


COUNTERS = ["cycles", "instructions", "branch_misses", "llc_refs", "llc_misses", "l1d_misses",
            "dtlb_misses", "dram_read_bytes", "dram_write_bytes"]


# --------------------------------------------------------------------------- running

class EngineError(RuntimeError):
    pass


class Engine:
    def __init__(self, binary=DEFAULT_BINARY, cpu=None):
        self.binary = Path(binary)
        self.cpu = pick_cpu() if cpu is None else cpu
        if not self.binary.exists():
            raise EngineError(f"{self.binary} not found: run `make` first")

    def command(self, storage, data, queries, grid, measure):
        """queries: engine --query strings (each should carry a comma-free label=)."""
        cmd = ["taskset", "-c", str(self.cpu), str(self.binary)]
        cmd += data.engine_args()
        cmd += ["--layout", storage.spec()]
        for q in queries:
            cmd += ["--query", q]
        cmd += grid.engine_args() + measure.engine_args()
        return cmd

    def run(self, storage, data, queries, grid, measure, log=None):
        """Runs the engine and returns (DataFrame, seconds). Raises EngineError on failure,
        with the engine's error message (e.g. a correctness mismatch)."""
        cmd = self.command(storage, data, queries, grid, measure)
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        seconds = time.time() - t0
        if log is not None:
            with open(log, "a") as f:
                f.write(f"$ {' '.join(cmd[:12])} ... ({len(queries)} queries)\n{proc.stderr}\n")
        if proc.returncode != 0:
            raise EngineError(f"engine failed for {storage.key()} on {data.name}: "
                              f"{proc.stderr.strip().splitlines()[-1:]}")
        df = pd.read_csv(io.StringIO(proc.stdout))
        return df, seconds
