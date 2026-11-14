#pragma once

#include <cstddef>
#include <string>

// The memory hierarchy of the machine the benchmark runs on, read from Linux sysfs
// (/sys/devices/system/cpu/cpu0/cache). Falls back to common x86 values if sysfs is missing.
struct CacheInfo {
    std::size_t size = 0;  // bytes
    std::size_t line = 64;
    std::size_t ways = 0;
    std::size_t sets = 0;
    // Bytes between two addresses that map to the same set (size / ways). Arrays whose
    // starts differ by a multiple of this compete for the same `ways` lines.
    std::size_t set_stride() const { return ways ? size / ways : 0; }
};

struct MachineInfo {
    std::string cpu;
    CacheInfo l1d, l2, l3;
    std::size_t page_size = 4096;
    std::string thp;  // transparent huge pages mode: always / madvise / never

    static const MachineInfo& get();
    // Multi-line human-readable summary.
    std::string describe() const;
};

// Machine-derived defaults for the tuning knobs (see README, "Tuning for the machine").

// Vector size for late materialization and decoding: the largest power of two (256 to
// 16384) such that one vector's worth of per-row state fills at most a quarter of L2.
// `bytes_per_row` is what a vector keeps live per row: 8 bytes per decoded or gathered
// value plus 4 bytes of selection vector.
//
// The first version sized vectors to half of L1D. On the test machine that was the worst
// choice for decoding (64-row vectors of 8 columns: 14 ms against 10 ms at 512-4096 rows):
// per-vector overheads (a virtual decode call per column) dominate small vectors, while L2
// is fast enough (~3 ns, ~130 GB/s) to hold the vectors between the phases of a query.
std::size_t auto_batch(const MachineInfo& m, std::size_t bytes_per_row);

// Per-column start offset for DSM arrays: consecutive columns start 17 cache lines apart
// (mod the L2 set stride), so up to 64 columns read in lockstep land in distinct L1 sets
// and distinct L2 sets instead of all in one.
std::size_t auto_dsm_offset(const MachineInfo& m);

// Selectivity below which the auto strategy uses late materialization.
//
// Memory moves in cache lines. With v values per line and selectivity s, a fraction
// f(s) = 1 - (1 - s)^v of a column's lines holds at least one selected value, so late
// materialization still reads that fraction of every gathered column. Per row, in units of
// "one sequential scan of one column":
//     early  ~  1 + g                       (the predicate column and the g gathered ones)
//     late   ~  1 + sigma + g * f(s) / phi
// where sigma is the fixed cost of late (building the selection vector, which is CPU-bound
// at about 2 cycles per row, against ~1.2 cycles per 8-byte value from DRAM on the test
// machine), and 1/phi is how much more a sparsely gathered line costs than a streamed one
// (no prefetching, latency-bound). Late wins while
//     f(s) < phi * (1 - sigma / g),
// so
//     threshold = 1 - (1 - phi * (1 - sigma / g))^(1 / v),   v = line size / value_bytes,
// and late never wins when g <= sigma. Fitted on this machine (DSM, from DRAM, see the
// report): phi ~ 0.55, sigma ~ 2. For 8-byte values that gives 2.1% for g = 3 and 7.7% for
// g = 15 (measured crossovers: 2.1% and 7.4%); for 7-bit packed values (73 per line) the
// threshold is 9x lower. sigma = 0 recovers the pure cache-line rule f(s) < phi.
//   value_bytes  bytes one value takes in the layout (a whole tuple for NSM)
//   gathered     columns late reads only at selected positions (all referenced columns
//                but the first predicate's)
double late_threshold(const MachineInfo& m, double value_bytes, double phi, double sigma,
                      std::size_t gathered);
