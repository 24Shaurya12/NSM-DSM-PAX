"""Geometry of a pass over a region, at the granularity of one cache level (line size B).

A region is `n_periods` repetitions of a `period`-byte block (an NSM row, a PAX page, or one
8-byte DSM value). An access *component* reads some byte runs of every block: the runs are
candidates, and each *unit* in them is accessed with probability f. A unit is either the whole
block (NSM: a row qualifies or not) or one 8-byte value (DSM/PAX: every value is its own row).
Accessed units come in runs: `gap` is the mean length of a run of skipped units (1/f for
independent rows; larger for sorted or clustered data).

For a line holding k consecutive candidate units, P(touched) = 1 - (1-f) (1 - 1/gap)^(k-1)
(a two-state Markov chain; for independent rows this is 1 - (1-f)^k, the line-touch
probability of DSM after a selection). Manegold's s_tra is the case f = 1.

Components of one region must have disjoint runs (describe.py gives each column to exactly one
component, the one with the largest f). A line shared by several components is touched with the
largest of their probabilities, which is exact for nested selections over the same rows.
"""
import math
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Component:
    runs: tuple            # ((offset, length), ...) within one block, bytes
    f: float = 1.0         # probability that a unit is accessed
    gap: float = 1.0       # mean run of skipped units (only used when f < 1)
    unit_is_block: bool = False  # True: the block is one unit (NSM row); False: 8-byte values


def p_touch(k, f, gap):
    if f >= 1.0 or k <= 0:
        return 1.0 if k > 0 else 0.0
    if f <= 0.0:
        return 0.0
    stay = max(0.0, 1.0 - 1.0 / max(gap, 1.0))
    return 1.0 - (1.0 - f) * stay ** (k - 1)


def merge_runs(runs):
    """Sorted, non-overlapping, adjacent runs merged."""
    out = []
    for o, ln in sorted(runs):
        if out and o <= out[-1][0] + out[-1][1]:
            prev_o, prev_l = out[-1]
            out[-1] = (prev_o, max(prev_l, o + ln - prev_o))
        else:
            out.append((o, ln))
    return tuple(out)


def column_runs(cols, width=8, stride=8, base=0):
    """Byte runs of columns `cols` when column c starts at base + c * stride (stride = value
    width for NSM, minipage size for PAX), each `width` bytes long, merged where adjacent."""
    return merge_runs((base + c * stride, width) for c in cols)


@dataclass
class LineStats:
    touched: float      # expected lines touched per block (averaged over the window)
    seq: float          # of which counted as sequential misses
    candidates: float   # lines holding candidate bytes per block
    dense_runs: tuple   # lengths (bytes) of contiguous dense runs per block (for over-fetch)


@lru_cache(maxsize=200_000)
def line_stats(period, comps, B):
    """Per-block line statistics of a pass with components `comps` over blocks of `period`
    bytes, at line size B. Exact over one window of lcm(period, B) bytes."""
    window = period * B // math.gcd(period, B)
    items = window // period
    if items > 4096:  # tiny blocks against huge lines: a long enough sample of the window
        items = 4096
    interior_t = interior_seq = interior_c = 0.0
    edge = {}  # line -> (probability, owner density) of partially covered lines
    for comp in comps:
        partial = {}
        t_c = c_c = 0.0
        for i in range(items):
            for o, ln in comp.runs:
                a, b = i * period + o, i * period + o + ln
                la, lb = a // B, (b - 1) // B
                for line in ((la, lb) if lb > la else (la,)):
                    lo, hi = max(a, line * B), min(b, (line + 1) * B)
                    if comp.unit_is_block:
                        partial.setdefault(line, set()).add(i)
                    else:
                        partial[line] = partial.get(line, 0) + (hi - lo) // 8
                inner = lb - la - 1
                if inner > 0:
                    k = 1 if comp.unit_is_block else B // 8
                    t_c += inner * p_touch(k, comp.f, comp.gap)
                    c_c += inner
        probs = {line: p_touch(len(k) if comp.unit_is_block else k, comp.f, comp.gap)
                 for line, k in partial.items()}
        all_c = c_c + len(probs)
        density = 1.0 if comp.f >= 1.0 else (t_c + sum(probs.values())) / all_c if all_c else 0
        interior_t += t_c
        interior_c += c_c
        interior_seq += t_c * density
        for line, p in probs.items():
            if p > edge.get(line, (-1.0, 0.0))[0]:
                edge[line] = (p, density)
    touched = interior_t + sum(p for p, _ in edge.values())
    # Sequential share: dense components stream; a touched line of a sparse component counts
    # as sequential with the probability that its neighbour is touched too (its line density).
    seq = interior_seq + sum(p * d for p, d in edge.values())
    candidates = interior_c + len(edge)
    dense = merge_runs([r for c in comps if c.f >= 1.0 for r in c.runs])
    return LineStats(touched / items, seq / items, candidates / items, _wrap_runs(dense, period))


def _wrap_runs(runs, period):
    """(run length, gap to the next run) per block, cyclically: a run ending at the block end
    joins one starting at 0 (consecutive blocks then form one longer run, e.g. a DSM column or
    a full NSM row); a single run covering the whole block is endless (inf)."""
    if not runs:
        return ()
    runs = list(runs)
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][0] + runs[-1][1] == period:
        o, ln = runs.pop()
        runs[0] = (o - period, ln + runs[0][1])
    if len(runs) == 1 and runs[0][1] >= period:
        return ((math.inf, 0),)
    out = []
    for i, (o, ln) in enumerate(runs):
        nxt = runs[(i + 1) % len(runs)][0] + (period if i + 1 == len(runs) else 0)
        out.append((ln, nxt - (o + ln)))
    return tuple(out)


@lru_cache(maxsize=100_000)
def lines_per_access(offsets, B, period=0, samples=64):
    """Expected distinct lines touched by one random access that reads 8 bytes at each of
    `offsets` (relative to the accessed item), averaged over the item's alignment to B. Items
    start at multiples of `period` from a B-aligned base (0 = uniformly random alignment)."""
    if period:
        cycle = B // math.gcd(period, B)
        step = max(1, cycle // samples)
        aligns = [(i * period) % B for i in range(0, cycle, step)]
    else:
        aligns = [B * s // samples // 8 * 8 for s in range(samples)]
    total = 0
    for a in aligns:
        lines = set()
        for off in offsets:
            lines.add((a + off) // B)
            lines.add((a + off + 7) // B)
        total += len(lines)
    return total / len(aligns)
