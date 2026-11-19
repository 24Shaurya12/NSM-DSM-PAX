# COL868 Project 1 — Presentation outline (NSM vs DSM vs PAX, encodings, machine tuning)

## Instructions for building the deck

- **Audience:** a database systems course (COL868). They know NSM/DSM/PAX, caches and
  compression, but not this project.
- **Length:** 17 main slides for a **20-minute talk** (about 1 minute per slide, a little more
  for slides 4, 9 and 14), plus backup slides for questions.
- **Style:** one message per slide, stated in the title. Few words on the slide, with the
  detail in speaker notes. Prefer one chart or table per slide. Charts should use the data
  tables given here. No dual-axis charts.
- **Story:** one idea runs through the talk: *performance is decided by the cache lines a query
  moves and by what the CPU does with each one.*
  1. **Encodings** (slides 3–7): fewer bytes alone don't help; working on compressed data
     does, and decoding must fit the time memory takes.
  2. **Layouts and materialization** (8–11): count the lines each layout and strategy moves;
     then see why a scattered line costs more than a streamed one.
  3. **Tuning for this machine** (12–15): each hardware parameter (cache sizes, cache sets,
     write-allocate, pages) decides one code choice.
  4. **Result and conclusions** (16–17).
- **Figures:** the project report already has PNG figures in `report/figures/`. Where a slide
  says "Figure: report/figures/X.png", the presenter can upload that image instead of
  redrawing the chart.
- **Numbers:** all come from one core of an AMD Ryzen 5 5600H laptop under WSL2, 4M rows,
  8-byte integer attributes, unless a slide says otherwise. They match the report
  (`report/report.pdf`) and `results/`; the encoding numbers come from the 2026-11-15 re-run.
  Don't round them further or invent new ones.

---

## Slide 1 — Title

**Title:** Comparing NSM, DSM and PAX — and tuning a scan engine for one machine
**Subtitle:** COL868 Project 1 · in-memory C++ scan engine · AMD Ryzen 5 5600H
**Speaker notes:** We built an in-memory scan engine with three layouts and several column
encodings, measured how layout, materialization and compression affect latency and cache
behaviour, and then tuned the engine using what we know about this specific CPU.

## Slide 2 — Setup

**Title:** One engine, three layouts, one laptop
**Content:**
- Layouts: NSM (row store), DSM (column store), PAX (columns inside pages)
- Queries: scans with equality/range predicates, projection, SUM, full-tuple reconstruction
  (`SELECT *`); early and late materialization
- Varied: projectivity, selectivity, table width, working-set size, PAX page size, encoding
- Measured: latency plus hardware counters (cache misses, TLB misses)
- Default table: 4M rows × 16 attributes = 512 MB, 32× the L3, so scans come from DRAM

**Visual:** the machine as a small table

| L1D | L2 | L3 | Line | DRAM (one core) | SIMD |
|---|---|---|---|---|---|
| 32 KB, 8-way, 64 sets | 512 KB | 16 MB | 64 B | read 27–28 GB/s, write 11–12 GB/s | AVX2, no AVX-512 |

**Speaker notes:** Keep these numbers in mind: every tuning decision later maps to one of them.
All parameters are read from sysfs at startup (`scan_bench --machine`), not hard-coded.

---

## Part 1: Encodings

## Slide 3 — Fewer bytes alone don't make a scan faster

**Title:** Decode first, and every encoding is slower than plain, even at 32× compression
**Visual:** bar chart of the speedup, with the compression ratio printed next to each bar.
Horizontal line at 1× = plain uncompressed DSM.

SUM over 8 columns, 4M rows, scalar code, decode each vector first, then add:

| Encoding | Idea | Data it suits | Compression | Speedup vs plain |
|---|---|---|---|---|
| rle | one (value, run end) per run | runs of ~64 rows | 32× | 0.69× |
| delta | difference from the previous value, bit-packed | sorted | 15.6× | 0.30× |
| bitpack | each value in the minimum bits (7 bits for 0–99) | uniform 0–99 | 9.1× | 0.80× |
| for | per block: min + bit-packed offsets | uniform / sorted | 8.9× / 5.3× | 0.77× |
| dict | sorted dictionary + bit-packed codes | Zipf, 10,000 distinct | 4.5× | 0.30× |

**Key message:** the speedup doesn't follow the compression ratio. "Less data, faster scan"
is wrong as stated.
**Speaker notes:** Each data set is designed so every encoding has the data it suits.
Compression = uncompressed bytes / encoded bytes. A sixth encoding, `dictb`, comes on slide 7.

## Slide 4 — Encodings win by working on the compressed data

**Title:** Compute on the compressed form, and every encoding beats plain
**Visual:** grouped bars per encoding: "decode first" vs "on compressed data", line at plain.
Or the table:

| Encoding | Operation on the compressed form | Decode first | On compressed | Plain DSM |
|---|---|---|---|---|
| **rle** | SUM = Σ value × run length | 11.5 ms | **0.10 ms** | 8.4 ms |
| **delta** | SUM = n·first + Σ dₖ·(n−k), additions only | 24.5 ms | **6.4 ms** | 8.0 ms |
| **bitpack** | SUM = n·min + Σ offsets, added as they're unpacked | 7.7 ms | **4.9 ms** | 7.9 ms |
| **for** | zone maps: skip blocks whose [min, max] misses the predicate | 4.0 ms | **0.09–0.36 ms** | 1.3–1.4 ms |
| **dictb** | WHERE on codes: value range → code range, compared in place | 2.6 ms | **0.57 ms** | 1.3 ms |

**Key message:** the work becomes proportional to the compressed size (runs, blocks, deltas,
codes), not to the number of values. rle 87×, dictb 2.2×, bitpack 1.6×, delta 1.25× faster
than plain.
**Visual idea:** rle values [7, 2, 9] with ends [4, 6, 10] → `7×4 + 2×2 + 9×4`. Delta: values
10, 12, 13, 17 = deltas 2, 1, 4 weighted 3, 2, 1, so the sum = 4·10 + 2·3 + 1·2 + 4·1 = 52.

**Speaker notes:**
- All with AVX2 kernels, 4M rows. SUM rows: 8 columns; for: SUM(c0..c3) at 0.1–1% selected;
  dictb: SUM(c0) at 1% selected, Zipf data. "Plain" = the better of early and late on the
  uncompressed column, same run.
- Making rle cheap took more than the formula: the first version spent 60% of its time in a
  binary search for the start run in every vector (1.9 ms, 4.3× plain). Remembering the last
  run, one call per column, and a loop over whole runs that gcc vectorizes brought it to
  0.10 ms (backup E). Its 7.6 MB of runs stay in L3; from DRAM it would be ~0.3 ms, ~28×.
- Delta decoding is a running sum (each value waits for the previous one), so it can't use
  SIMD. The SUM formula has no such chain.

## Slide 5 — Otherwise, decoding must fit in the time memory takes

**Title:** From DRAM the CPU waits ~2/3 of the time; decoding only pays if it fits in that wait
**Figure:** report/figures/enc_workingset.png
**Visual:** a time budget per value: memory time vs decode time, and the table

SUM of 8 columns, bit-packed with AVX2 vs plain:

| Where the data lives | Plain, cycles/value | Packed, cycles/value | Packed speedup |
|---|---|---|---|
| L2 / L3 | 0.26–0.30 | 0.61–0.68 | 0.40–0.49× (loses) |
| 16 MB (L3 edge) | 0.46 | 0.61–0.64 | 0.72–0.76× (loses) |
| DRAM (≥ 64 MB) | 0.90–0.98 | 0.61–0.65 | **1.42–1.60× (wins)** |

**Content:**
- The same plain loop is 3× faster in cache, so from DRAM it is waiting on memory
- **Rule:** compression wins ⇔ decode cost per value < the memory time per value: ~1 cycle
  from DRAM (8 bytes at ~28 GB/s), ~0.35 from L3. In cache there is no wait to hide decoding in
- Scalar decoders miss the budget: bitpack ~1.2 cycles/value (0.8×), dict ~2 (0.3–0.5×),
  delta 3–4, a serial chain (0.3×)

**Speaker notes:** Compression turns a memory-bound scan into a compute-bound one. That pays
only if the new compute is cheaper than the memory time removed. The budget is DRAM
*bandwidth* (~8 bytes per cycle), not latency: the prefetcher keeps many lines in flight.
Cycles assume ~4 GHz; the scalar decode costs are back-computed from the measured speedups.

## Slide 6 — AVX2 makes decoding fit the budget, and that's machine-specific

**Title:** With AVX2, bit-unpacking costs < 1 cycle per value, and compression wins
**Visual:** left, the unpacking diagram; right, scalar vs AVX2 bars with a line at 1×
```
bit:   0      7      14     21     28     35     42     49     56
       |  v0  |  v1  |  v2  |  v3  |  v4  |  v5  |  v6  |  v7  |
```
**Content:**
- **AVX2** = 256-bit SIMD registers: one instruction on 8 × 32-bit values. Our Zen 3 has AVX2
  but no AVX-512. It must be enabled explicitly (`-march=native` / `target("avx2")`)
- Unpacking 8 values of 7 bits in ~5 instructions:
  1. **shuffle** (`vpshufb`): bring each value's bytes into its own 32-bit lane
  2. **per-lane shift** (`vpsrlvd`, AVX2 only): shift each lane by its own amount
  3. **mask** with `0x7F`

| SUM, 8 columns, from DRAM | Scalar | AVX2 |
|---|---|---|
| bitpack (uniform) | 0.80× | **1.60×** |
| for (uniform) | 0.77× | **1.56×** |
| delta (SUM from the deltas) | 0.52× | **1.25×** |

- **Why machine-specific:** the budget comes from this machine's DRAM bandwidth; whether to
  compress at all depends on the 16 MB L3; the kernel needs AVX2's per-lane shift

**Speaker notes:** SSE2 can only shift all lanes by the same amount, and the compiler can't
auto-vectorize this loop (the shift changes value by value), so the kernels are hand-written,
one per bit width (backup A). Lane width is 32 bits, not 8: the variable shift exists only for
32- and 64-bit lanes, and a 7-bit value can straddle a byte boundary. On runs data bitpack
and for reach 1.51× / 1.49×. With faster memory or a bigger cache the break-even point moves.

## Slide 7 — Shape the data for the register: byte-aligned dictionary codes

**Title:** Dictionary codes of 1, 2 or 4 whole bytes made dictionary predicates 2–5× faster than plain
**Visual:** left, two register diagrams; right, the table
```
14-bit packed codes:  unpack first     → 8 codes per 256-bit register
2-byte codes:         load and compare → 16 codes per register   (1-byte: 32)
```
`SUM(c0) WHERE c0 < x`, Zipf (10,000 distinct values), AVX2:

| Selected | Plain DSM | dict (14-bit packed) | dictb (2-byte codes) | dictb vs plain |
|---|---|---|---|---|
| 0.05% | 1.29 ms | 1.50 ms | **0.24 ms** | 5.4× |
| 1% | 1.28 ms | 1.63 ms | **0.57 ms** | 2.2× |
| 5% | 1.27 ms | 1.96 ms | **0.66 ms** | 1.9× |
| 41% | 1.28 ms | 2.57 ms | 1.44 ms | 0.9× |

**Two machine-specific choices:**
1. **Code width = a lane width AVX2 can compare** (8, 16, 32 bits). It gives up 12% of the
   compression (4.0× instead of 4.5×) and removes all unpacking. Range test:
   `min(code − lo, span) == code − lo`, 3 instructions per 16 or 32 codes
2. **Branch or no branch, by selectivity.** Skipping 32-code groups with no match is fastest
   when matches are rare, but at 1% 27% of groups have a match and the branch mispredicts
   constantly (0.90 vs 0.55 ms). The column skips only when the previous vector was below 0.2%

**Speaker notes:** What it doesn't fix: SUM over all rows still needs one value per row (SUM
of 8 columns: plain 8.2 ms, dict 18.4, dictb 9.3). Rejected on purpose: SUM by counting codes
(a counter per code, look up the dictionary once at the end) was 10–25% slower, because a
counter update is a load + add + store and equal codes wait on each other through memory.
Counting is right for GROUP BY, not SUM. With AVX-512 the trade-off changes (`vpcompressd`,
byte permutes). The 0.90 vs 0.55 ms was measured during development.

---

## Part 2: Layouts and materialization

## Slide 8 — Count the cache lines, and you know which layout wins

**Title:** Every layout difference is the number of cache lines a query moves
**Figure:** report/figures/projectivity.png
**Content:**

| Layout | Lines read per row (16 attributes, k read, selectivity s) |
|---|---|
| NSM | whole rows: **2**, whatever k is |
| DSM / PAX, early | **k/8** |
| DSM / PAX, late | 1/8 + (k−1)/8 · f(s), with f(s) = 1 − (1 − s)⁸ |

- **Reading few attributes:** SUM of 1 of 16 attributes: DSM 1.0 ms, NSM ~15 ms, **15×**
  (the bytes ratio is 16×). NSM is flat in k (13–22 ms); DSM grows with k
- **Returning few rows:** `SELECT *` at 0.01%: DSM late 2.3 ms vs NSM 15.1 ms
- **Rebuilding whole rows:** projection of all 16 attributes: **NSM wins** (61 vs 72 ms), and
  for `SELECT *` from 5% selectivity up NSM is 0–9% ahead of DSM. DSM must stitch every
  tuple from 16 places; NSM copies one contiguous range
- **Lines predict time:** a read line costs ~2.3 ns (64 B at 28 GB/s), a written one ~5.3 ns
  (write-allocate, slide 14). NSM `SELECT *`: 4M × (2 × 2.3 + 2 × 5.3) ns = **61 ms predicted,
  61.2 ms measured**

**Speaker notes:** PAX lies between: it reads only the needed minipages but pays per minipage
(SUM(c0) 3.9 ms at 16 KB pages, slide 15). Where the data lives changes the answer too: the
NSM/DSM gap for SUM(c0) shrinks from 15× in DRAM to ~5× in L2, where lines are cheap and CPU
work is what's left. Width and working-set sweeps: backup F. `SELECT *` by selectivity per
layout: backup G.

## Slide 9 — Late reads fewer lines, but each one costs more

**Title:** Late materialization wins only below 2.4–7.4% selectivity, for two reasons
**Visual:** two diagrams (early streams all k columns; late builds a selection vector, then
gathers), then the table
**Content:**
- **Early:** read all k columns, test the predicate per row, write passing rows.
  **Late:** scan the WHERE column into a *selection vector* (positions of passing rows), then
  fetch only those positions from each other column
- **Reason 1, lines not rows:** a line holds 8 values, so late fetches it if any of its 8 rows
  passes: at 1% of rows, 8% of lines; at 5%, 34%; **at 10%, 57%**
- **Reason 2, a gathered line costs 2.2–3.2× a streamed one:** the addresses jump
  irregularly, so the prefetcher can't run ahead. At 5%, gathering a third of a column's lines
  (1.28 ms) is slower than reading all of it (1.21 ms)
- **Plus a fixed first pass:** building the selection vector (scalar, ~2 cycles/row) costs
  2.05 ms against 1.21 ms for just reading the column

| DSM from DRAM | 1% | 2.4% | 5% | 7.4% | 10% |
|---|---|---|---|---|---|
| k = 4: early / late (ms) | 4.28 / **3.38** | 4.18 / 4.19 | **4.46** / 5.84 | — | — |
| k = 16: early / late (ms) | 20.3 / **7.2** | — | 22.1 / **19.7** | **23.6** / 25.2 | **26.1** / 31.8 |

- **Crossovers: 2.4% for 4 attributes, 7.4% for 16.** The fixed pass is paid once, the savings
  once per skipped column, so more columns let late win longer

**Speaker notes:** The cost of one gathered column (k = 16): 0.34 ms at 1%, 0.66 at 2.4%,
1.17 at 5%, 1.97 at 10%, against 1.27–1.63 ms for a whole column in early. Measured by timing
k = 1..16 at several selectivities (`results/late_breakdown/`); the WSL2 counters were too
unreliable to confirm the prefetcher directly. Building the selection vector and reading the
column overlap, so the first pass costs the max of the two, not the sum. One gather pass for
all columns didn't help below 5% (backup D).

## Slide 10 — In cache, CPU work decides instead

**Title:** In cache, skipping lines saves nothing; instructions per row decide
**Visual:** table (SUM of 4 DSM columns, table in L3, ns per row)

| | 0.01% | 1% | 2% | 5% | 10% |
|---|---|---|---|---|---|
| early | 0.30 | 0.30 | 0.30 | 0.30 | 0.30 |
| late, scalar select | 0.46 | 0.50 | 0.52 | 0.61 | 0.73 |
| late, AVX2 select | **0.20** | **0.24** | **0.26** | 0.34 | 0.46 |

**Content:**
- Early SUM: `total += row_sum & mask` (a failing row adds 0). No branch, no store, the same
  work every row, so the compiler vectorizes it: **~1.2 cycles/row**
- Scalar late spends ~2 cycles/row on the selection vector alone, so it **never wins SUM in
  cache**
- AVX2 selection vector: compare 8 values → 8-bit mask → a 256-entry table gives the passing
  positions → **~0.9 cycles/row**. Now late wins up to **3.4% (L3)** and **5.8% (L2)**
- For `SELECT *`, the in-cache crossovers are much higher (19% L3, 24% L2, vs ~2% from DRAM):
  gathered lines are cheap there, and early pays CPU time to stitch every row

**Speaker notes:** The compiler can't vectorize the selection vector (or early `SELECT`)
like early SUM: their output is a packed list, where row i goes depends on how many earlier
rows passed (stream compaction). AVX2 has no "compress" instruction; AVX-512's `vpcompressd`
would do it in one, but Zen 3 has no AVX-512, hence the lookup table (backup B).

## Slide 11 — Choosing early or late while the query runs

**Title:** A threshold from the line model works where it was fitted; measuring makes it robust
**Figure:** report/figures/auto.png
**Content:**
- **Threshold mode:** late while f(s) < φ(1 − σ/g) (g = gathered columns), using the previous
  span's selectivity. Fitted on DSM from DRAM: φ = 0.55, σ = 2 → predicts 2.5% / 7.8%,
  measured 2.4% / 7.4%. Within 1% of the better fixed strategy there
- **Sorted data:** a span is ~0% or ~100% selected, so auto switches inside one query (late
  outside the matching stretch, early inside) and beats **both** fixed strategies by up to 24%
- **But the constants are a fit:** measured one by one, the selection vector costs ~0.7
  scans (not 2) and a gathered line 2.2–3.2× (not 1.8). They don't carry over to PAX or to
  data in L3: up to **2.5× too slow**
- **Measuring mode** (`--auto-measure`): times each span of ≥ 1024 rows with `rdtsc`, keeps a
  cost per strategy per selectivity range, runs the cheaper one. Worst case **2.5× → 1.5×**,
  for 2–5% overhead where the model was already right

**Speaker notes:** About 1 vector in 250 is interrupted (3–30× its cost), so samples are
clipped at 2× the running estimate; otherwise one interrupt flips the choice for hundreds of
spans. The overhead is the timing itself plus occasionally re-running the slower strategy on
purpose. Spans are at least 1024 rows because a 128-row PAX page at 1% has 0–4 matching rows,
too few to estimate a selectivity from.

---

## Part 3: Tuning for this machine

## Slide 12 — Vector size: match it to the work, not to one cache level

**Title:** Work per value and a fixed cost per vector decide whether L1, L2 or L3 is the target
**Visual:** the pipeline line, then the table
```
per vector:  [fixed cost O]  →  step 1 on B values  →  step 2 on B values  → …
             paid once          paid per value
```
**Content:**
- **Fixed cost per vector** (call setup, a UDF call, handing a batch to a thread): bigger
  vectors amortize it. It decides how **big** the vector should be
- **Work per value:** the more cycles per value, the fewer bytes per cycle the loop needs, so
  a slower level can keep up. It decides how **slow a level** the buffers may live in

ns per value, 32M values; each cell is the best vector size whose buffers fit that level:

| Workload | Vector in L1 | Vector in L2 | Vector in L3 |
|---|---|---|---|
| Random reads inside the vector (like probing a per-vector hash table) | **1.34** | 1.69 | 2.55 |
| Same random reads + 2 µs fixed cost per vector | 2.40 | **1.90** | 2.60 |
| Light work + 20 µs fixed cost per vector | 12.05 | 2.26 | **1.69** |

- Our scans need little bandwidth per value and have a tiny fixed cost, so the engine sizes
  vectors to **¼ of L2**: decoding SUM of 8 bit-packed columns 1.2× faster than L1-sized
  vectors (11.9 → 9.9 ms)

**Speaker notes:** In hardware, L1 is faster than L2 (4 vs 12 cycles latency, 64 vs 32 bytes
per cycle peak), but a sequential scan loop uses ~30 bytes per cycle or less and the
prefetcher hides L2's latency, so for our scans L2 is as fast as L1 (L1-sized vectors cut L1
misses from 19 to 5 per row with no change in time). Random reads can't be prefetched, so they
pay L1's 4 cycles or L2's 12 (row 1). A fixed cost pushes the best size up (rows 2 and 3);
the L2 rule would pick 128 KB in row 3, 2.5× slower. The fixed costs are a busy-wait standing
in for real per-batch costs; real systems size batches this way (Spark's Python UDFs: 10,000
rows per batch; HyPer's morsels: ~100,000 rows). Data: `build/batchbench`,
`results/batch_demo/`; the scan numbers: `results/batch/`. Details: backup H.

## Slide 13 — L1 aliasing: where PAX minipages start made early 2.7× slower

**Title:** 16 minipages exactly 4 KB apart fight over one 8-slot cache set
**Visual:** one L1 set with 16 lines trying to fit into 8 slots, then the table
**Content:**
- The L1 is 64 sets × 8 slots × 64 B lines. An address picks its set, and the pattern repeats
  every **4 KB**: addresses 4 KB apart always share a set, which holds 8 lines
- A PAX page of P bytes holds 16 minipages, **P/16** bytes apart. From **64 KB pages** on, P/16
  is a multiple of 4 KB, so value r of all 16 minipages lands in **one set**
- Early reads a row from all 16 at once: 16 lines for 8 slots, constant eviction, **8.6 L1
  misses per row**
- **Fix** (`--pax-pad auto`): an **odd number of lines** per minipage (64 → 63), so the 16
  start in 16 different sets. Costs 1 line per minipage (1.6% at 64 KB)

| PAX page size | Early `SELECT *` at 1%, plain | Odd-line minipages |
|---|---|---|
| 32 KB (stride 2 KB: 8 per set, fits) | 37.8 ms | not needed |
| **64 KB** (stride 4 KB) | **65.6 ms**, 8.6 misses/row | **27.8 ms**, 3.3 |
| 256 KB | 63.0 ms | 24.2 ms |
| 4 MB | 62.7 ms | 23.3 ms |

- **2.3–2.7× faster, same code.** From 256 KB pages, PAX early is within 7% of DSM (22.7 ms).
  Late is unaffected (6.2–8.5 ms): it reads one minipage at a time

**Speaker notes:** 64 bytes of padding per minipage doesn't help (62.1–64.9 ms): with the page
size fixed, the stride stays P/16. The same happens in DSM: every column is its own
page-aligned array, so row r of all 16 columns maps to one set. `SELECT *` at 1%: 62.6 ms with
page-aligned columns, 22.9 ms when column c starts 17·c lines in (17 shares no factor with
64, so up to 64 columns get distinct sets). We found it by accident: with `malloc`, glibc
sometimes placed the columns back to back, which happened to spread them over different sets,
so the same query took 21 ms in one experiment and 64 ms in another. The rule follows this
L1's geometry and is computed from sysfs.

## Slide 14 — Writing output: skip the read that every normal write does

**Title:** A normal write first reads the line; streaming stores through a cached stage: 1.2–1.9× faster
**Visual:** two flows, then the table
```
normal store:     DRAM ──read line──▶ cache ──modify──▶ evict ──▶ DRAM     (2 transfers)
streaming store:  core ──▶ write-combining buffer (fills 64 B) ──▶ DRAM    (1 transfer)
```
**Content:**
- A store to an uncached line first **reads the whole line** (write-allocate): reads
  27–28 GB/s, normal writes 11–12 GB/s, **streaming writes 32–43 GB/s**
- A streaming store bypasses the cache: it fills a **write-combining buffer** (hardware, one
  64-byte line) and sends it to DRAM with no read. But only a handful exist, so scattered
  writes send them out half-full, which is slow
- Our writes are scattered: late fills each output row in 16 column passes; early writes every
  row and overwrites the rejected ones. Hence the **stage**: one vector's output (128 KB),
  reused every vector, so it stays in cache

| | Messy writes absorbed? | Write-allocate read? |
|---|---|---|
| Normal stores | ✓ by the cache | ✗ paid on every output line |
| Streaming stores directly | ✗ no cache, so partial lines | ✓ avoided |
| **Stage (normal stores) + streaming copy** | ✓ by the cache | ✓ avoided |

- `SELECT *` of every row: DSM early 71.2 → **37.9 ms**, NSM early 67.1 → **41.5**, PAX late
  86.5 → **45.3** (1.9×). The line model predicts ~61 → ~31 ms

**Speaker notes:** The stage alone with a normal `memcpy` is up to 36% *slower* (an extra copy,
still paying write-allocate): the gain is all from the streaming stores; the stage only makes
them usable. All 12 configurations gain 1.2–1.9× at 100%; the gain grows with the output (DSM
early: 21.8 → 20.5 ms at 1%, 71.2 → 37.9 at 100%). Streaming stores are right for a result
written once and read later; a result the next operator reads at once should stay in cache.
Intel uses its ~10–12 line fill buffers as write-combining buffers; AMD doesn't publish Zen 3's
count. Early at 100% could stream without a stage (not measured).

## Slide 15 — Pages: the prefetcher and the TLB

**Title:** Small pages cost twice: PAX minipages too short to prefetch, 4 KB pages too small for the TLB
**Visual:** two halves

**Left — PAX page size and the prefetcher.** The prefetcher needs a few misses to detect a
stream and doesn't follow it into another page. With 16 KB pages, each PAX column is a 1 KB
minipage (16 lines) then a 16 KB jump: a new stream every page, its first lines fetched at full
DRAM latency.

| `SUM(c0)` | PAX 1 KB | PAX 16 KB (default) | PAX 64 KB | PAX 4 MB | DSM |
|---|---|---|---|---|---|
| ms | 20.1 | **4.9** | 2.8 | **1.2** | 1.2 |

At 16 KB: ~120 ns extra per page, about one DRAM latency. Early `SELECT *` at 1%: 53.8 ms vs
DSM's 22.7.

**Right — huge pages and the TLB.** Every load translates a virtual address; the TLB caches
~2048 translations. With 4 KB pages that covers 8 MB, so every 512 values of a column is a new
page and a page walk. `--hugepages`: buffers ≥ 2 MB are allocated at a 2 MB boundary and
`madvise(MADV_HUGEPAGE)`d before first touch, so Linux backs them with 2 MB pages (16 TLB
entries per 32 MB column instead of 8192). No query code changes.

| DSM, k = 16 | 4 KB pages | 2 MB pages |
|---|---|---|
| late SUM | 23.3 ms | **18.7 ms** (20%; 24% at k = 8) |
| early SUM | 19.3 ms | 18.8 ms (3%) |

**Content:** early and late miss the TLB equally often (0.031 → 0.008 per row), but early
reads 16 columns interleaved, so other loads continue during a page walk; late reads one
column at a time and every next load waits. **Stagger (slide 13) helps early; huge pages help
late.**

**Speaker notes:** 1 KB PAX pages fail differently: 8 rows per page, so per-page CPU work
dominates. The default 16 KB page comes from disk pages; in memory the right size is set by the
prefetcher and the L1 sets: from 256 KB with odd-line minipages, PAX reads like DSM. THP here
is in `madvise` mode, so Linux gives 2 MB pages only to memory that asks; `madvise` is a
request, not a guarantee. Page walks are costlier under WSL2 (nested page tables). The TLB
sizes are AMD's spec; the overlap explanation fits the equal miss counts but page-walk cycles
weren't measured directly.

---

## Part 4: Result

## Slide 16 — Everything together: 1.46× faster

**Title:** The machine-tuned engine is 1.46× faster on average, up to 3.8×
**Figure:** report/figures/tuned.png
**Content:**
- 6 queries × 3 layouts = 18 cells, from DRAM
- **Untuned:** defaults (1024-row vectors, page-aligned DSM columns, 4 KB pages, normal stores,
  scalar code, 16 KB PAX pages), given the **best of the four fixed strategies** per cell
- **Tuned:** everything from slides 6 and 10–15; strategy chosen by measured auto

| NSM | DSM | PAX | **All 18 cells** |
|---|---|---|---|
| 1.11× | 1.41× | **1.96×** | **1.46×** (1.50× with the best tuned strategy) |

- **Biggest wins:** PAX `SUM(c0)` 4.5 → 1.2 ms (**3.8×**); `SELECT *` of every row 1.7× (NSM),
  2.3× (DSM), 2.5× (PAX)
- **Little to gain:** queries already one sequential stream (DSM `SUM(c0)`: 1.03×)
- **Exceptions:** NSM `SELECT *` at 10%: 0.95× (its best untuned strategy, early-pipe, isn't in
  the tuned set); PAX at 10%: 1.01×

**Speaker notes:** Geometric mean, because speedups are ratios. The baseline is generous: it
is given the best fixed strategy, which a real engine can't know. After tuning, NSM no longer
wins `SELECT *`: 36.9 ms on NSM, 34.6 on DSM, 31.7 on PAX. One cell (NSM `SELECT c0..c3` at
1%, auto 0.76×) hit a slow period on the WSL2 host; repeated three times, auto takes 15.2–16.1
ms against late's 14.5–15.4. The 1.46× includes the bad measurement.

## Slide 17 — Conclusions

**Title:** Count the lines, then make each line cheap on this machine
- **Encodings:** compression wins by computing on compressed data (rle 87×, dictb 2.2–5.4×);
  otherwise only if decoding fits DRAM's time per value, which here takes AVX2
- **Layouts:** cache lines moved explain the differences: DSM for few attributes or few rows
  (up to 15×), NSM for rebuilding whole rows, PAX only with large, unaliased pages
- **Materialization:** late wins below 2.4–7.4% from DRAM, because a gathered line costs 2–3×
  a streamed one; in cache, CPU work per row decides. Measuring beats a fitted threshold
- **Tuning:** each hardware parameter maps to one code choice (vector size ↔ L2 and work per
  value, column/minipage placement ↔ L1 sets, streaming stores ↔ write-allocate, huge pages ↔
  TLB). Together: **1.46×**, no change to the queries
- **Limitations:** one core, one laptop, WSL2 noise, 8-byte integers. The fitted constants must
  be re-measured on another machine (`make experiments`)

---

## Backup slides

### Backup A — Why the compiler doesn't do this for you
- A default x86-64 build may use only SSE2 (128-bit). AVX2 must be allowed explicitly.
- gcc auto-vectorizes simple loops (`sum += a[i]`). It even vectorized our new RLE loop,
  building the missing 64-bit multiply from 3 × `vpmuludq`.
- It can't vectorize bit-unpacking (the shift changes per value) or the selection vector
  (mask → positions), so those are hand-written.

### Backup B — The AVX2 selection vector
- `WHERE c0 < x`: compare 8 values at once → 8-bit mask → 256-entry table lookup gives the
  passing offsets → store all 8, advance by popcount(mask)
- Scalar ~2 cycles/row → AVX2 ~0.9 cycles/row in cache. From DRAM it helps mainly at the
  lowest selectivities (the late first pass drops from 2.05 to 1.21 ms, memory speed)
- Figure: report/figures/simd.png

### Backup C — Dictionary codes: what byte codes don't fix
- SUM of all rows still needs one value per row. SUM of 8 columns: plain 8.2 ms, dict 18.4 ms,
  dictb 9.3 ms.
- SUM by counting codes (Σ dict[c] × count[c]) was 10–25% *slower* than one lookup per row
  (tried with 32-bit counters and with 16-bit L1-resident counters).
- Not done: GROUP BY on codes (dense codes index an array, no hash table) and string columns.

### Backup D — Tried and dropped: one gather pass for all columns
- Idea: one pass over the selection vector fetching all k attributes of each selected row,
  instead of one pass per column (more independent misses per row, output written in order)
- Below 5%: 0.97–1.11× the time, no gain. The per-column loop already keeps many misses in
  flight (each address comes from the cached selection vector)
- Above 7%: 10–30% faster, but there late already loses. The k=16 crossover moved only from
  ~7% to 8–9%
- Lesson: the expensive part is the touched line itself, not how the loop is organised

### Backup E — Making RLE cheap, step by step

| Step | RLE SUM time |
|---|---|
| First version (binary search for the start run in every 1024-row vector) | 1.9 ms (4.3× plain) |
| Remember the last run instead of searching | 0.86 ms |
| One call per column when there's no WHERE (no vectors needed) | 0.38 ms |
| Loop over whole runs without a `min` per run (gcc auto-vectorizes it) | **0.10 ms (87× plain)** |

`perf` showed 60% of the time in the binary search: each vector covers only ~16 runs, so a
16-step search per vector cost more than the summing. The middle two steps were measured
during development; the first and last match the report.

### Backup F — Table width and working-set size
- **Width** (table fixed at 512 MB, N = 2..256 attributes): `SUM(c0)` on DSM falls as 1/N.
  `SELECT *` of every row: DSM grows from 63 ms (N=4) to 106 ms (N=256), NSM stays at 66–69 ms.
  `SELECT *` at 1%: NSM fastest from N=128 (4.8 vs 5.2 ms), 3.1 vs 4.9 at N=256
- **Working set** (16 KB to 512 MB): NSM/DSM gap for `SUM(c0)` from ~5× in L2 to 15× in DRAM.
  `SELECT *`: DSM fastest while the table fits in L2 (2.2–3.1 vs 3.1–3.5 ns per row), NSM once
  it fills L3 (13.0 vs 13.6 ns at 16 MB, 16.1 vs 18.1 from DRAM). From L3 to DRAM costs
  2.3–5.8× for every query and layout
- Figures: report/figures/width.png, report/figures/workingset.png

### Backup G — `SELECT *` by selectivity, best strategy per layout (ms)

| Selected | NSM | DSM | PAX (16 KB pages) |
|---|---|---|---|
| 0.01% | 15.1 | **2.3** (late) | 8.4 |
| 1% | 16.2 | **6.9** (late) | 13.8 |
| 5% | **19.5** | 20.3 | 23.0 |
| 20% | **28.9** | 30.0 | 30.6 |
| 50% | 44.6 | **44.2** | 53.1 |
| 100% | **64.9** | 68.4 | 83.8 |

Figure: report/figures/selectivity.png

### Backup H — Cache speeds on this CPU and the vector-size measurements
- Measured (one core): latency L1 0.96 ns (~4 cycles), L2 2.9 ns (~12), L3 11–13 ns, DRAM
  105–135 ns; read bandwidth L1 110–129 GB/s, L2 127–131, L3 88–95, DRAM 27–28
  (`results/machine/membench.csv`)
- More `batchbench` results: 8 light passes over one buffer, 0.91 ns per value in L1 vs
  1.09–1.18 in L2 (needs more than L2's 32 B/cycle). Heavy work (~27 cycles/value) + 20 µs per
  vector: best in L3 (6.63 ns), and buffers in DRAM only 7.5% slower
- Our engine's scan (decode 8 bit-packed columns + SUM): 0.37 ns per value with L1-sized
  vectors, 0.31 with L2-sized, 0.32 with L3-sized (within noise of L2)
