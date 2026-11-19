"""Exhaustive search: every storage configuration x every execution option per query. The
space is small (tens of storage configurations, a few options) and a prediction takes well under
a millisecond, so this is the default."""
from .base import RankedResults, Search, StorageResult, best_choice, register_search


@register_search("exhaustive")
class Exhaustive(Search):
    def run(self, space, workload, model, ctx):
        results = []
        for s in self.candidates(space, ctx):
            choices = [best_choice(model, s, q, ctx, space) for q in workload.queries]
            results.append(StorageResult(s, self.score(choices, ctx), choices))
        results.sort(key=lambda r: r.cost_ns)
        return RankedResults(results, ctx.objective)
