"""Models of the engine's value distributions (src/distributions.cpp), one class per name.

The ground-truth runner uses them to turn a target selectivity into a predicate constant; the
cost model (phase 4) will also ask how qualifying rows cluster and how predictable a predicate
branch is. To add a distribution: register a generator in the engine and a class here.
"""
import math

from .registry import Registry

DISTRIBUTIONS = Registry("distribution model")


def parse_spec(spec):
    """'uniform:lo=0:hi=99' -> ('uniform', {'lo': '0', 'hi': '99'})."""
    name, *parts = spec.split(":")
    args = {}
    for p in parts:
        k, sep, v = p.partition("=")
        if not sep:
            raise ValueError(f"expected key=value in distribution spec '{spec}'")
        args[k] = v
    return name, args


def make_model(spec, rows):
    name, args = parse_spec(spec)
    return DISTRIBUTIONS.get(name)(args, rows)


class DistributionModel:
    """Integer values in [lo, hi]. Subclasses describe how they are arranged along the table."""

    def __init__(self, args, rows):
        self.lo = int(args.pop("lo", 0))
        self.hi = int(args.pop("hi", 99))
        self.rows = rows
        if args:
            raise ValueError(f"unknown parameters {sorted(args)} for {self.registered_name}")

    @property
    def ndv(self):
        return self.hi - self.lo + 1

    def pass_rate(self, lo, hi):
        """Fraction of rows with lo <= value <= hi (every built-in has a uniform marginal)."""
        lo, hi = max(lo, self.lo), min(hi, self.hi)
        return max(0, hi - lo + 1) / self.ndv

    def threshold(self, rate):
        """K such that `value < K` passes (as close as possible to) `rate` of the rows."""
        return self.lo + int(round(rate * self.ndv))

    def mean_run(self, rate):
        """Expected length of a run of consecutive qualifying rows (phase 4 uses it)."""
        raise NotImplementedError

    def branch_predictable(self):
        """Whether a predicate branch on this column follows a learnable pattern."""
        return False


@DISTRIBUTIONS.register("uniform")
class Uniform(DistributionModel):
    def mean_run(self, rate):
        return math.inf if rate >= 1 else 1.0 / (1.0 - rate)


@DISTRIBUTIONS.register("sorted")
class Sorted(DistributionModel):
    def mean_run(self, rate):
        return max(1.0, rate * self.rows)  # all qualifying rows form one run

    def branch_predictable(self):
        return True


@DISTRIBUTIONS.register("clustered")
class Clustered(DistributionModel):
    def __init__(self, args, rows):
        args = dict(args)
        self.run = int(args.pop("run", 1024))
        super().__init__(args, rows)

    def mean_run(self, rate):
        return math.inf if rate >= 1 else self.run / (1.0 - rate)

    def branch_predictable(self):
        return self.run >= 64
