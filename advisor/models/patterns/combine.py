"""Combining patterns (Manegold et al. §3.3).

  Seq (⊕)   patterns one after another; each may use the whole level. Because the ground truth
            starts cold and our streams never revisit data, the cache-state carry-over of
            eq. 11-12 only matters for re-used buffers, which are modelled explicitly (Reuse).
  Conc (⊙)  patterns interleaved; level capacity is shared in proportion to their footprints
            (eq. 13). This decides, e.g., whether a DSM selection vector survives next to the
            column data streaming through the same cache.
  Rounds    nest(): the body runs in `rounds` batches with `cursors` interleaved streams (DSM
            columns, one batch at a time). Where a level cannot hold one line (or TLB page) per
            cursor, every round re-fetches each cursor's boundary line.
"""
from dataclasses import dataclass


@dataclass
class Seq:
    parts: list

    def misses(self, level, share=1.0):
        s = r = 0.0
        for p in self.parts:
            a, b = p.misses(level, share)
            s, r = s + a, r + b
        return s, r

    def footprint(self):
        return max((p.footprint() for p in self.parts), default=0.0)

    def dense_runs(self):
        return [x for p in self.parts for x in p.dense_runs()]


@dataclass
class Conc:
    parts: list

    def misses(self, level, share=1.0):
        fps = [max(p.footprint(), 1.0) for p in self.parts]
        total = sum(fps)
        s = r = 0.0
        for p, fp in zip(self.parts, fps):
            a, b = p.misses(level, share * fp / total)
            s, r = s + a, r + b
        return s, r

    def footprint(self):
        return sum(p.footprint() for p in self.parts)

    def dense_runs(self):
        return [x for p in self.parts for x in p.dense_runs()]


@dataclass
class Rounds:
    body: object
    rounds: float
    cursors: int
    touch: float = 1.0     # probability a cursor touches anything in a round

    def misses(self, level, share=1.0):
        s, r = self.body.misses(level, share)
        if self.cursors * level.B > level.capacity * share:
            r += self.rounds * self.cursors * self.touch
        return s, r

    def footprint(self):
        return self.body.footprint()

    def dense_runs(self):
        return self.body.dense_runs()
