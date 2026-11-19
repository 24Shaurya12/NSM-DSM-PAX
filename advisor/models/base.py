"""Cost-model interface, the model's view of the hardware, and the query description.

A model predicts the time of one (storage configuration, execution options, query, data set)
point and returns a CostBreakdown whose parts are comparable with the hardware counters.
Models register with @register_model("name") (models/registry.py).
"""
import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..data_profile import make_model


# --------------------------------------------------------------------------- hardware

@dataclass
class Level:
    """One level of the hierarchy as the patterns see it: a miss here costs seq_ns / rand_ns
    (the *additional* time to get the line from the next level, as in Manegold's l_{i+1})."""
    name: str
    kind: str              # "data" | "tlb"
    capacity: float        # bytes (TLB: entries x page)
    B: int                 # line (TLB: page)
    seq_ns: float
    rand_ns: float
    mlp: float = 1.0       # misses of this level overlapped by independent accesses


class HardwareModel:
    """Built from hw.json. Data levels L1..Ln (the last one misses to DRAM) and TLB levels."""

    def __init__(self, hw):
        self.hw = hw
        self.freq = hw.get("freq_ghz") or 3.0
        self.cpu = hw.get("cpu", {})
        lv = [x for x in hw["levels"] if x["name"] != "DRAM"]
        dram = next(x for x in hw["levels"] if x["name"] == "DRAM")
        self.dram_gb_s = dram["seq_gb_s"]
        self.dram_lat = dram["lat_ns"]
        self.B_eff = hw.get("dram", {}).get("B_eff") or lv[-1]["line"]
        self.mlp_dram = hw.get("dram", {}).get("mlp") or 1.0
        mlp_l3 = _mlp_at(hw, "2M", 4 << 20) or self.mlp_dram
        self.data_levels = []
        for i, x in enumerate(lv):
            nxt = lv[i + 1] if i + 1 < len(lv) else dram
            seq = x["line"] * max(0.0, 1.0 / nxt["seq_gb_s"] - 1.0 / x["seq_gb_s"])
            rand = max(0.0, nxt["lat_ns"] - x["lat_ns"])
            mlp = self.mlp_dram if nxt is dram else mlp_l3
            self.data_levels.append(Level(x["name"], "data", x["bytes"], x["line"], seq, rand, mlp))
        self.last_cache = self.data_levels[-1]
        self.last_cache_gb_s = lv[-1]["seq_gb_s"]
        t = hw.get("tlb") or {}
        self.tlb_levels = []
        page = t.get("page", 4096)
        if t.get("l1_entries") and t.get("l1_miss_ns") is not None:
            self.tlb_levels.append(Level("TLB1", "tlb", t["l1_entries"] * page, page,
                                         t["l1_miss_ns"], t["l1_miss_ns"], self.mlp_dram))
        if t.get("l2_entries") and t.get("walk_ns") is not None:
            pen = max(0.0, t["walk_ns"] - (t.get("l1_miss_ns") or 0.0))
            self.tlb_levels.append(Level("TLB2", "tlb", t["l2_entries"] * page, page, pen, pen,
                                         self.mlp_dram))
        self.overfetch = hw.get("overfetch", [])

    @staticmethod
    def load(path):
        return HardwareModel(json.loads(Path(path).read_text()))

    def levels(self):
        return self.data_levels + self.tlb_levels

    def cycles(self, name, default=0.0):
        v = self.cpu.get(name)
        return default if v is None or (isinstance(v, float) and math.isnan(v)) else v


def _mlp_at(hw, page, size):
    v = hw.get("mlp", {}).get(f"{page}:{size}")
    return v


# --------------------------------------------------------------------------- queries and data

@dataclass
class DataInfo:
    rows: int
    cols: int
    dist: dict = field(default_factory=dict)   # {'default': spec, 'c0': spec}

    def model(self, c):
        spec = self.dist.get(f"c{c}", self.dist.get("default", "uniform:lo=0:hi=99"))
        return make_model(spec, self.rows)


@dataclass
class QueryInfo:
    """What the model needs to know about a query (independent of the engine's text form)."""
    op: str                       # sum | materialize | lookup
    cols: list                    # projected columns
    preds: list = field(default_factory=list)  # [(col, pass rate)] in evaluation order
    lookups: int = 0
    id_dist: str = "uniform"

    @property
    def k(self):
        return len(self.cols)

    def cum_rates(self):
        """F_0 = 1, F_j = product of the first j pass rates (independent predicates)."""
        out, f = [1.0], 1.0
        for _, r in self.preds:
            f *= r
            out.append(f)
        return out

    @property
    def selectivity(self):
        return self.cum_rates()[-1]


def parse_cols(text, ncols):
    if text == "all":
        return list(range(ncols))
    out = []
    for part in str(text).split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


_PRED = re.compile(r"c(\d+)<(-?\d+)")


def query_from_template(t, rates=None):
    """QueryInfo from a templates.csv row. `rates`: per-predicate pass rates (default: the
    template's target rates)."""
    ncols = int(t["n_cols"]) if "n_cols" in t else None
    cols = parse_cols(t["cols"], ncols or 0)
    preds = []
    where = t.get("where")
    if isinstance(where, str) and where:
        pcols = [int(m.group(1)) for m in _PRED.finditer(where)]
        prs = rates or [float(x) for x in str(t["pred_rates"]).split(";")]
        preds = list(zip(pcols, prs))
    return QueryInfo(t["op"], cols, preds, int(t.get("lookups") or 0),
                     t.get("id_dist") if isinstance(t.get("id_dist"), str) else "uniform")


@dataclass
class ExecInfo:
    dsm_batch: int = 1024
    filter: str = "branchy"       # branchy | branchless


@dataclass
class StorageInfo:
    layout: str                   # nsm | dsm | pax
    page: int = 0


# --------------------------------------------------------------------------- results

@dataclass
class CostBreakdown:
    total_ns: float
    mem_ns: float = 0.0
    cpu_ns: float = 0.0
    seq_ns: float = 0.0           # part of mem_ns from sequential misses
    rand_ns: float = 0.0          # part of mem_ns from random misses
    tlb_ns: float = 0.0           # part of mem_ns from TLB misses
    dram_bytes: float = 0.0
    misses: dict = field(default_factory=dict)   # level -> (sequential, random)
    branch_misses: float = 0.0
    cpu_terms: dict = field(default_factory=dict)  # term -> ns


class CostModel(ABC):
    name = "?"
    #: fitted parameters with defaults (subclasses extend); fit() changes them
    params = {}

    def __init__(self, hw, **overrides):
        self.hw = hw if isinstance(hw, HardwareModel) else HardwareModel(hw)
        self.params = dict(type(self).params, **overrides)

    @abstractmethod
    def predict(self, storage, exec_opts, query, data):
        """-> CostBreakdown for one execution of `query`."""
