#!/usr/bin/env python3
"""Compact summary of one experiment output, printed by experiments.py after each run.

scan_bench CSV: one line per configuration, one column per strategy, median latency in ms.
Only the settings that vary inside the file are shown (layout always), and the settings
shared by every line are printed once above the table.
membench CSV: one working-set size per doubling (the largest at or below each power of two),
one column per test.
batchbench text: printed as is.

Usage: brief.py FILE [FILE ...]
"""
import csv
import statistics
import sys
from collections import defaultdict

# Settings that can tell configurations apart, in display order.
KEYS = ["query", "layout", "dist", "k", "where", "sel", "page_size", "batch", "knobs"]
INDENT = "      "


def fmt_bytes(b):
    b = int(b)
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if b >= size:
            v = b / size
            return f"{v:.0f} {unit}" if v >= 100 else f"{v:.3g} {unit}"
    return f"{b} B"


def fmt_ms(x):
    return f"{x:.1f}" if x >= 100 else f"{x:.2f}" if x >= 1 else f"{x:.3g}"


def fmt_cell(key, value):
    if key == "sel" and value not in ("", "-"):
        return f"{float(value) * 100:.3g}%"
    if key == "page_size":
        return "-" if value in ("", "0") else fmt_bytes(value)
    return value


def print_table(header, rows):
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    line = lambda r: INDENT + "  ".join(str(c).rjust(w) for c, w in zip(r, widths))  # noqa: E731
    print(line(header))
    print(INDENT + "  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r))


def scan_bench(rows):
    lat = defaultdict(list)  # (settings..., strategy) -> latencies
    for r in rows:
        lat[tuple(r.get(k, "") for k in KEYS) + (r.get("strategy", "-"),)].append(
            int(r["latency_ns"]))
    varying = [i for i, k in enumerate(KEYS)
               if k == "layout" or len({key[i] for key in lat}) > 1]
    # With a swept --sel, `where` only repeats it as a constant (c0<10049); show sel alone.
    if KEYS.index("sel") in varying:
        varying = [i for i in varying if KEYS[i] != "where"]
    first = next(iter(lat))
    fixed = [f"{KEYS[i]}={fmt_cell(KEYS[i], first[i])}" for i in range(len(KEYS))
             if i not in varying and first[i] not in ("", "-", "0")
             and not (KEYS[i] == "where" and KEYS.index("sel") in varying)]
    strategies = list(dict.fromkeys(key[-1] for key in lat))
    table = defaultdict(dict)
    for key, v in lat.items():
        table[tuple(fmt_cell(KEYS[i], key[i]) for i in varying)][key[-1]] = statistics.median(v) / 1e6
    if fixed:
        print(INDENT + "  ".join(fixed))
    print_table([KEYS[i] for i in varying] + [f"{s} ms" for s in strategies],
                [list(cfg) + [fmt_ms(t[s]) if s in t else "" for s in strategies]
                 for cfg, t in table.items()])


def membench(rows):
    tests = list(dict.fromkeys(r["test"] for r in rows))
    sizes = sorted({int(r["bytes"]) for r in rows})
    keep = {max(s for s in sizes if s <= 1 << p)
            for p in range(sizes[0].bit_length() - 1, sizes[-1].bit_length())}
    table = defaultdict(dict)
    for r in rows:
        if int(r["bytes"]) in keep:  # the sweep has 8 sizes per doubling; show one
            table[int(r["bytes"])][r["test"]] = float(r["value"])
    unit = lambda t: "latency ns" if t == "latency" else f"{t} GB/s"  # noqa: E731
    print_table(["working set"] + [unit(t) for t in tests],
                [[fmt_bytes(b)] + [f"{t[x]:.2f}" if x in t else "" for x in tests]
                 for b, t in sorted(table.items())])


def brief(path):
    if not path.endswith(".csv"):
        with open(path) as f:
            for line in f:
                print(INDENT + line.rstrip())
        return
    with open(path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        print(INDENT + "(empty)")
    elif "latency_ns" in reader.fieldnames:
        scan_bench(rows)
    elif {"test", "bytes", "value"} <= set(reader.fieldnames):
        membench(rows)
    else:
        print(INDENT + f"(unrecognised CSV: {', '.join(reader.fieldnames)})")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        brief(p)
