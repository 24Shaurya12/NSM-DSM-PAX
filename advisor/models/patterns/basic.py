"""Basic access patterns (Manegold et al. §3, plus ss_tra). Each pattern answers, for one cache
level (capacity C, line B) and the share of C it may use, how many lines miss sequentially and
how many randomly: misses(level, share) -> (Ms, Mr). Patterns start from a cold cache, as the
ground truth does (the table is flushed before every query).

  Stream  s_tra / ss_tra: one ordered pass over a region of blocks (dense or sparse components)
  RAcc    r_acc: N independent random accesses, each touching a few lines of one item
  Reuse   rs_tra: a small buffer passed over repeatedly (selection vector, output buffer); it
          misses on every pass only where it does not fit in its share of the level
"""
import math
from dataclasses import dataclass, field

from .regions import line_stats, lines_per_access


@dataclass
class Stream:
    name: str
    period: int            # block bytes (NSM row, PAX page, DSM value = 8)
    n_blocks: float
    comps: tuple           # regions.Component, disjoint runs
    overfetch_ok: bool = True  # dense runs feed the prefetch over-fetch extension
    live_bytes: float = 0.0    # bytes streamed between reuses of a concurrent buffer (⊙
                               # share); 0 = the whole pass

    def misses(self, level, share=1.0):
        s = line_stats(self.period, self.comps, level.B)
        return self.n_blocks * s.seq, self.n_blocks * (s.touched - s.seq)

    def footprint(self):
        if self.live_bytes:  # only the lines actually touched stream through the cache
            s = line_stats(self.period, self.comps, 64)
            return self.live_bytes * (s.touched / s.candidates if s.candidates else 1.0)
        return self.n_blocks * line_stats(self.period, self.comps, 64).touched * 64

    def dense_runs(self):
        """[(run bytes, gap after it, block period, number of runs)] for the over-fetch
        extension; the prefetchers can only over-fetch into the gap before the next run."""
        if not self.overfetch_ok:
            return []
        s = line_stats(self.period, self.comps, 64)
        return [(r, g, self.period, self.n_blocks) for r, g in s.dense_runs if math.isfinite(r)]


@dataclass
class RAcc:
    name: str
    n: float               # accesses
    offsets: tuple         # bytes read (8 each) relative to the accessed item
    item_period: int       # alignment stride of items
    region_bytes: float    # bytes the accesses are spread over

    def misses(self, level, share=1.0):
        lpa = lines_per_access(self.offsets, level.B, self.item_period)
        lines = max(1.0, self.region_bytes / level.B)
        touches = self.n * lpa
        distinct = lines * (1.0 - math.exp(-touches / lines))
        cap = level.capacity * share / level.B
        if distinct <= cap:
            return 0.0, distinct
        hit = min(1.0, cap / lines)
        return 0.0, cap + (touches - cap) * (1.0 - hit)

    def footprint(self):
        return min(self.region_bytes, self.n * len(self.offsets) * 64)

    def dense_runs(self):
        return []


@dataclass
class Reuse:
    name: str
    bytes: float           # buffer size
    passes: float          # total passes over it during the query

    def misses(self, level, share=1.0):
        lines = math.ceil(self.bytes / level.B)
        if self.bytes <= level.capacity * share:
            return float(lines), 0.0  # only the first pass
        return self.passes * lines, 0.0

    def footprint(self):
        return self.bytes

    def dense_runs(self):
        return []


@dataclass
class Misses:
    """Per-level result of evaluating a pattern."""
    seq: dict = field(default_factory=dict)    # level name -> sequential misses
    rand: dict = field(default_factory=dict)   # level name -> random misses
