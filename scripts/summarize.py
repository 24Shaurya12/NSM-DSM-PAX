#!/usr/bin/env python3
"""Read scan_bench CSV from stdin and print per-(query, k, layout) summary tables:
latency, and (if the CSV has perf counter columns) median hardware-counter metrics."""
import csv
import statistics
import sys
from collections import defaultdict

from perf_metrics import counter_columns, metrics


def print_table(header, table):
    widths = [max(len(r[i]) for r in [header] + table) for i in range(len(header))]
    fmt = lambda r: "  ".join(c.rjust(w) for c, w in zip(r, widths))
    print(fmt(header))
    print("  ".join("-" * w for w in widths))
    prev_group = None
    for r in table:
        if prev_group is not None and r[:3:2] != prev_group:
            print()
        print(fmt(r))
        prev_group = r[:3:2]  # (query, k)


def main():
    reader = csv.DictReader(sys.stdin)
    counters = counter_columns(reader.fieldnames or [])
    groups = defaultdict(list)  # (query, k, layout) -> CSV rows
    for row in reader:
        groups[(row["query"], int(row["k"]), row["layout"])].append(row)
    if not groups:
        sys.exit("summarize.py: no benchmark rows on stdin")

    header = ["query", "layout", "k", "p=k/N", "page_size", "median_ms", "min_ms", "max_ms",
              "read_GB/s"]
    table = []
    for (query, k, layout), runs in sorted(groups.items()):
        lat = [int(r["latency_ns"]) for r in runs]
        rows, cols, page_size = (int(runs[0][f]) for f in ("rows", "cols", "page_size"))
        median = statistics.median(lat)
        table.append([
            query,
            layout,
            str(k),
            f"{k / cols:.3f}",
            str(page_size) if page_size else "-",
            f"{median / 1e6:.3f}",
            f"{min(lat) / 1e6:.3f}",
            f"{max(lat) / 1e6:.3f}",
            f"{rows * k * 8 / median:.2f}",  # input bytes read per ns == GB/s
        ])
    print_table(header, table)

    if not counters:
        return
    print("\nHardware counters (median over timed runs; /row = per table row)")
    names = list(metrics(next(iter(groups.values()))[0], counters))
    table = []
    for (query, k, layout), runs in sorted(groups.items()):
        per_run = [metrics(r, counters) for r in runs]
        table.append([query, layout, str(k)] +
                     [f"{statistics.median(m[n] for m in per_run):.3f}" for n in names])
    print_table(["query", "layout", "k"] + names, table)


if __name__ == "__main__":
    main()
