"""Selectivity estimators (plan §6.2): pass rate of each predicate of a conjunction.

  profile    from the column's distribution model (data.yaml dist/min/max) - the default
  histogram  from an equi-width/any-width histogram given in data.yaml (falls back to profile)
  fixed      the rates written in the workload (`rates: [...]`), else profile
Conjunctions assume independent columns. Add one with @register_estimator("name").
"""
from .registry import Registry

ESTIMATORS = Registry("selectivity estimator")
register_estimator = ESTIMATORS.register


class Estimator:
    def __init__(self, data, hists=None):
        self.data = data
        self.hists = hists or {}

    def rate(self, pred):
        raise NotImplementedError

    def rates(self, query):
        """Pass rate of each predicate (as written), independent of evaluation order."""
        return [self.rate(p) for p in query.preds]


@register_estimator("profile")
class ProfileEstimator(Estimator):
    def rate(self, p):
        m = self.data.model(p.col)
        lo = m.lo if p.lo == float("-inf") else p.lo
        hi = m.hi if p.hi == float("inf") else p.hi
        return m.pass_rate(int(lo), int(hi))


@register_estimator("histogram")
class HistogramEstimator(ProfileEstimator):
    def rate(self, p):
        h = self.hists.get(p.col)
        if not h:
            return super().rate(p)
        edges, counts = h["edges"], h["counts"]
        total, hit = float(sum(counts)), 0.0
        for a, b, n in zip(edges[:-1], edges[1:], counts):  # bin [a, b), uniform inside
            lo, hi = max(a, p.lo), min(b, p.hi + 1)
            if hi > lo:
                hit += n * (hi - lo) / (b - a)
        return hit / total if total else 0.0


@register_estimator("fixed")
class FixedEstimator(ProfileEstimator):
    def rates(self, query):
        return list(query.rates) if query.rates is not None else super().rates(query)


def make_estimator(name, data, hists=None):
    return ESTIMATORS.get(name)(data, hists)
