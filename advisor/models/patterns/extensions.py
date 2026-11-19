"""Modern-hardware hooks applied while evaluating patterns (plan §5.2). All constants come from
hw.json (calibration), never from code:

  B_eff       effective DRAM transfer unit: misses of the last cache level are counted in units
              of B_eff bytes (a line plus its adjacent-line prefetch on many CPUs)
  overfetch   DRAM bytes actually moved per sequential run of `run` bytes repeated every
              `period` bytes (prefetchers run past the end of short runs); interpolated from the
              calibrated table
  mlp         random misses overlap: each costs rand_ns / mlp
  page_prefetch  page walks inside sequential streams are hidden (next-page prefetch); only
              random/sparse page misses cost a walk
"""
import math

import numpy as np


class Overfetch:
    def __init__(self, table):
        self.rows = [(math.log2(r["period"]), math.log2(r["run"]), r["dram_bytes"])
                     for r in table if r.get("offset", 0) == 0 and r.get("dram_bytes")]

    def __bool__(self):
        return bool(self.rows)

    def dram_bytes(self, run, period):
        """Measured DRAM bytes per run, interpolated (inverse distance in log space; exact on
        calibrated points). Runs longer than calibrated scale linearly with the run."""
        if not self.rows:
            return None
        lp, lr = math.log2(period), math.log2(max(run, 8))
        d = np.array([(p - lp) ** 2 + (r - lr) ** 2 for p, r, _ in self.rows])
        i = int(np.argmin(d))
        if d[i] < 1e-9:
            return self.rows[i][2]
        w = 1.0 / (d + 1e-9)
        # interpolate the *ratio* bytes/run so that sizes scale sensibly
        ratios = np.array([b / 2 ** r for _, r, b in self.rows])
        return float(np.sum(w * ratios) / np.sum(w)) * run


def units(bytes_, B):
    return math.ceil(bytes_ / B)
