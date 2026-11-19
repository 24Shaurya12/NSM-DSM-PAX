// L1 associativity probe for DSM-style tuple reconstruction.
//
// k column arrays are read "tuple at a time" (value i of every column, then value i+1), as DSM
// reconstruction does. If every array starts at the same offset within a 4 KB page, value i of
// every column falls into the SAME L1 set (set index = address bits 6..11 on a 64-set L1). With
// more columns than L1 ways, the lines evict each other before their next 7 uses.
// Variant "staggered" shifts array c by c * 64 bytes, spreading the columns over different sets.
//
//   build/assoc_probe [rows=8192] [kmax=32] > out.csv
#include <sys/mman.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "perf_counters.h"

namespace {
volatile int64_t g_sink;

double now_ns() {
    return std::chrono::duration<double, std::nano>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
}
}  // namespace

int main(int argc, char** argv) {
    const std::size_t rows = argc > 1 ? std::strtoul(argv[1], nullptr, 10) : 8192;
    const int kmax = argc > 2 ? std::atoi(argv[2]) : 32;
    const std::size_t col_bytes = rows * 8;
    const std::size_t slot = (col_bytes + 4096 + 4095) / 4096 * 4096;  // room for a 64 B shift
    char* base = static_cast<char*>(mmap(nullptr, slot * kmax, PROT_READ | PROT_WRITE,
                                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
    madvise(base, slot * kmax, MADV_NOHUGEPAGE);
    PerfCounters perf;
    std::size_t l1 = 0, cyc = 0;
    for (std::size_t i = 0; i < perf.size(); ++i) {
        if (perf.name(i) == "l1d_misses") l1 = i;
        if (perf.name(i) == "cycles") cyc = i;
    }
    std::printf("variant,k,ns_per_row,l1d_misses_per_row,cycles_per_row\n");
    for (int staggered = 0; staggered <= 1; ++staggered) {
        std::vector<const int64_t*> col(kmax);
        for (int c = 0; c < kmax; ++c) {
            int64_t* a = reinterpret_cast<int64_t*>(base + c * slot + (staggered ? c * 64 : 0));
            for (std::size_t r = 0; r < rows; ++r) a[r] = static_cast<int64_t>(r * 7 + c);
            col[c] = a;
        }
        for (int k = 1; k <= kmax; ++k) {
            auto run = [&] {
                int64_t s = 0;
                for (std::size_t r = 0; r < rows; ++r)
                    for (int c = 0; c < k; ++c) s += col[c][r] * (c + 1);
                return s;
            };
            g_sink = g_sink + run();
            double best = 1e30, l1v = 0, cyv = 0;
            for (int rep = 0; rep < 7; ++rep) {
                int reps = 0;
                perf.start();
                double t0 = now_ns();
                while (now_ns() - t0 < 5e6) {
                    g_sink = g_sink + run();
                    ++reps;
                }
                double el = now_ns() - t0;
                perf.stop();
                double per = el / (static_cast<double>(reps) * rows);
                if (per < best) {
                    best = per;
                    l1v = perf.value(l1) / (static_cast<double>(reps) * rows);
                    cyv = perf.value(cyc) / (static_cast<double>(reps) * rows);
                }
            }
            std::printf("%s,%d,%.3f,%.3f,%.2f\n", staggered ? "staggered" : "aligned", k, best, l1v,
                        cyv);
        }
    }
    return 0;
}
