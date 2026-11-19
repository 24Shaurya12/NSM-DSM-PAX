"""Access-pattern machinery after Manegold, Boncz & Kersten (VLDB 2002), extended.

regions.py     geometry: which lines/pages a pass over a region touches (dense or sparse)
basic.py       basic patterns: STra/SSTra (one Stream class), RAcc, Reuse (RSTra)
combine.py     Seq (⊕), Conc (⊙), Rounds (nest: interleaved cursors over batches/pages)
describe.py    (layout, op, exec options, query, data) -> pattern + CPU work
evaluate.py    pattern x hardware levels -> misses per level, DRAM bytes, memory time
extensions.py  modern-hardware hooks: B_eff, prefetch over-fetch, MLP
"""
