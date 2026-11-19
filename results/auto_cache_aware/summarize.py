# Each auto mode's latency relative to the better of early/late at that selectivity, taking
# early/late from the same run. Geomean and worst over the 10 selectivities.
import csv, collections as c, statistics as st, math, sys, os
here = os.path.dirname(os.path.abspath(__file__))
def gm(x): return math.exp(sum(map(math.log, x)) / len(x))
rows = []
allr = c.defaultdict(list)
for q, k in (("sum", 4), ("projection", 4), ("projection", 16)):
    for lay in ("NSM", "DSM", "PAX"):
        line = [f"{q} k={k}", lay]
        for mode in ("threshold", "cache", "measure"):
            d = c.defaultdict(list)
            for r in csv.DictReader(open(f"{here}/{q}_k{k}_{mode}.csv")):
                if r["layout"] == lay:
                    d[(float(r["sel"]), r["strategy"])].append(int(r["latency_ns"]))
            sels = sorted({s for s, _ in d})
            rel = [st.median(d[(s, "auto")]) / min(st.median(d[(s, "early")]), st.median(d[(s, "late")]))
                   for s in sels]
            allr[mode] += rel
            line.append(f"{gm(rel):.2f} / {max(rel):.2f}")
        rows.append(line)
print("| query | layout | threshold (DRAM consts) | threshold, in-cache consts | measure |")
print("|---|---|---|---|---|")
for l in rows: print("| " + " | ".join(l) + " |")
print("| **all** | | " + " | ".join(f"**{gm(allr[m]):.2f} / {max(allr[m]):.2f}**" for m in ("threshold","cache","measure")) + " |")
