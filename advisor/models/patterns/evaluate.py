"""Evaluate a pattern on the hardware levels: misses per level (Manegold eq. 1) -> memory time.

T_mem = sum over levels i of  Ms_i * l^s_{i+1} + Mr_i * l^r_{i+1} [/ mlp]
with the TLB levels contributing their walk penalties. Options switch on the modern
extensions (B_eff at the last cache level, prefetch over-fetch, memory-level parallelism).
"""
import math
from dataclasses import dataclass, field, replace

from .extensions import Overfetch


@dataclass
class MemCost:
    seq_ns: float = 0.0
    rand_ns: float = 0.0
    tlb_ns: float = 0.0
    dram_bytes: float = 0.0
    overfetch_bytes: float = 0.0
    misses: dict = field(default_factory=dict)

    @property
    def total_ns(self):
        return self.seq_ns + self.rand_ns + self.tlb_ns


def memory(pattern, hwm, b_eff=False, overfetch=False, mlp=False, page_prefetch=False):
    out = MemCost()
    last = hwm.last_cache
    dram_unit = hwm.B_eff if b_eff else last.B
    per_byte_dram = max(0.0, 1.0 / hwm.dram_gb_s - 1.0 / hwm.last_cache_gb_s)
    for lv in hwm.levels():
        level = lv
        if lv is last and b_eff:
            level = replace(lv, B=int(hwm.B_eff), seq_ns=hwm.B_eff * per_byte_dram)
        s, r = pattern.misses(level)
        out.misses[lv.name] = (s, r)
        rand_cost = level.rand_ns / (level.mlp if mlp else 1.0)
        if lv.kind == "tlb":
            # page_prefetch: page walks of a sequential stream are issued ahead of the stream
            # (next-page prefetch) and hidden; measured walks are 10-35x fewer than pages
            # crossed by DSM/NSM scans, while random lookups match the model exactly.
            out.tlb_ns += (0.0 if page_prefetch else s * level.seq_ns) + r * rand_cost
        else:
            out.seq_ns += s * level.seq_ns
            out.rand_ns += r * rand_cost
        if lv is last:
            out.dram_bytes = (s + r) * dram_unit
    if overfetch:
        table = Overfetch(hwm.overfetch)
        if table:
            for run, gap, period, count in pattern.dense_runs():
                measured = table.dram_bytes(run, period)
                own = math.ceil(run / dram_unit) * dram_unit
                extra = min(max(0.0, measured - own), max(0.0, gap - (own - run))) * count
                out.overfetch_bytes += extra
                out.dram_bytes += extra
                out.seq_ns += extra * per_byte_dram
    return out
