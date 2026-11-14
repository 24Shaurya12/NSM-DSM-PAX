"""Turn the hardware-counter columns of a scan_bench CSV row into per-row metrics."""

# Columns every scan_bench CSV has; anything after these is a perf counter.
BASE_COLUMNS = ["query", "layout", "strategy", "dist", "rows", "cols", "k", "where", "sel",
                "page_size", "batch", "knobs", "footprint_bytes", "iteration", "latency_ns",
                "rows_out",
                "result"]  # older CSVs had `result` instead of `rows_out`


def counter_columns(fieldnames):
    return [f for f in fieldnames if f not in BASE_COLUMNS]


def metrics(row, counters):
    """Metrics for one CSV row, in display order: derived ratios first, then each
    counter normalised per table row (e.g. l1d_misses/row)."""
    rows = int(row["rows"])
    c = {name: int(row[name]) for name in counters}
    out = {}
    if "cycles" in c and "instructions" in c and c["cycles"]:
        out["ipc"] = c["instructions"] / c["cycles"]
    if "l1d_loads" in c and "l1d_misses" in c and c["l1d_loads"]:
        out["l1d_miss_rate"] = c["l1d_misses"] / c["l1d_loads"]
    for name in counters:
        out[f"{name}/row"] = c[name] / rows
    return out
