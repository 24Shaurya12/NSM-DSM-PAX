"""Candidate space: which storage configurations and execution options to consider.

Read from a space file (configs/spaces/*.yaml):

    storage:                    # layout -> {param: [values]} (cross product per layout)
      nsm: {}
      pax: {page: [1K, 4K, 16K]}
    exec:
      dsm_batch: [1024, 16384]
      filter: [branchy, branchless]
      predicate_order: [selective_first, selective_last]

A layout that cannot hold the data set (e.g. a PAX page smaller than one row) is skipped; add
such rules for new layouts in VALIDITY.
"""
import itertools

import yaml

from .engine import ExecGrid, StorageConfig, parse_size

# layout -> f(StorageConfig, DataSpec) -> bool
VALIDITY = {
    "pax": lambda s, d: parse_size(s.param("page")) >= d.row_bytes
    and parse_size(s.param("page")) % 64 == 0,
}


def load_space(path):
    with open(path) as f:
        return yaml.safe_load(f)


def storage_configs(space, data=None):
    out = []
    for layout, grid in space["storage"].items():
        grid = grid or {}
        names = sorted(grid)
        for values in itertools.product(*[grid[n] for n in names]):
            params = {n: parse_size(v) if n in ("page",) else v for n, v in zip(names, values)}
            s = StorageConfig.make(layout, **params)
            if data is None or VALIDITY.get(layout, lambda *_: True)(s, data):
                out.append(s)
    return out


def exec_grid(space):
    e = space.get("exec", {})
    return ExecGrid(dsm_batch=e.get("dsm_batch", [1024]), filter=e.get("filter", ["branchy"]))


def predicate_orders(space):
    return space.get("exec", {}).get("predicate_order", ["as_written"])
