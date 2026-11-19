"""Random workload mixes for decision-quality validation (plan §7.2, V2/V3).

Each workload is drawn from the logical queries of ONE data set of a ground-truth store: 2-6
distinct queries with weights drawn log-uniformly over [1, 1000] (query frequencies, so a cheap
query can matter when it is frequent). Seeded; the list is saved with the results so every
number can be reproduced.
"""
import json
import math
import random


def generate(logical_ids_by_dataset, n=1000, seed=1, min_q=2, max_q=6, weight_range=(1, 1000)):
    rng = random.Random(seed)
    datasets = sorted(logical_ids_by_dataset)
    lo, hi = math.log(weight_range[0]), math.log(weight_range[1])
    out = []
    for i in range(n):
        ds = datasets[i % len(datasets)]
        pool = sorted(logical_ids_by_dataset[ds])
        k = rng.randint(min_q, min(max_q, len(pool)))
        qs = rng.sample(pool, k)
        out.append({"id": i, "dataset": ds,
                    "queries": [{"logical_id": q, "weight": round(math.exp(rng.uniform(lo, hi)), 3)}
                                for q in qs]})
    return out


def save(workloads, path):
    with open(path, "w") as f:
        json.dump(workloads, f, indent=1)


def load(path):
    with open(path) as f:
        return json.load(f)
