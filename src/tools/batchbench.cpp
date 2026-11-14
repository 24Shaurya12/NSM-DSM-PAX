// How the best vector size depends on the work per value and the fixed cost per vector.
// Input: 32M int64 values (256 MB, from DRAM), processed one vector of B rows at a time.
// Every vector first pays a fixed overhead O (busy-wait), modelling a per-batch cost such as
// a UDF call into another runtime or handing a batch to another thread. Then, by mode:
//   pipe R     buf1 = f(in); buf2 = f(buf1); sum += buf2      f = R rounds of mul/xor-shift
//              (heavy work per value, buffers read once)
//   passes P   buf = in; then P light passes buf[i] += (buf[i] >> 3) ^ c; sum += buf
//              (almost no work per byte, the buffer is re-read P times)
//   probe P    buf = in; then P passes sum += buf[idx[i]] with idx a random permutation
//              of 0..B-1 (random reads inside the vector, like probing a per-vector table)
// Usage: batchbench MODE PARAM OVERHEAD_US   (pin it: taskset -c 2 build/batchbench ...)
#include <x86intrin.h>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <vector>

static inline uint64_t f(uint64_t x, int rounds) {
    for (int r = 0; r < rounds; ++r) x = (x * 0x9E3779B97F4A7C15ull) ^ (x >> 29);
    return x;
}

int main(int argc, char** argv) {
    if (argc < 4) {
        std::fprintf(stderr, "usage: %s pipe|passes|probe PARAM OVERHEAD_US\n", argv[0]);
        return 2;
    }
    const std::size_t n = 32u << 20;
    const char* mode = argv[1];
    const int param = std::atoi(argv[2]);
    const double overhead_us = std::atof(argv[3]);
    const uint64_t overhead_ticks = static_cast<uint64_t>(overhead_us * 3300);  // TSC ~3.3 GHz
    const bool pipe = !std::strcmp(mode, "pipe"), passes = !std::strcmp(mode, "passes");
    const std::size_t max_b = pipe ? (16u << 20) : (4u << 20);
    std::vector<uint64_t> in(n);
    for (std::size_t i = 0; i < n; ++i) in[i] = i * 2654435761u;
    std::vector<uint64_t> buf1(max_b, 0), buf2(max_b, 0);
    std::vector<uint32_t> idx(max_b);
    std::printf("mode=%s param=%d overhead=%.1fus\n%10s %12s %10s %10s\n", mode, param,
                overhead_us, "B", "buffers", "ms", "ns/value");
    uint64_t sink = 0;
    std::mt19937 rng(42);
    for (std::size_t B = 256; B <= max_b; B *= 2) {
        if (!pipe && !passes) {  // a fresh random permutation of 0..B-1
            for (std::size_t i = 0; i < B; ++i) idx[i] = static_cast<uint32_t>(i);
            std::shuffle(idx.begin(), idx.begin() + B, rng);
        }
        double best = 1e30;
        for (int rep = 0; rep < 3; ++rep) {
            const auto t0 = std::chrono::steady_clock::now();
            uint64_t sum = 0;
            for (std::size_t s = 0; s < n; s += B) {
                const uint64_t w = __rdtsc() + overhead_ticks;
                while (__rdtsc() < w) {}
                const uint64_t* src = in.data() + s;
                uint64_t* b1 = buf1.data();
                if (pipe) {
                    uint64_t* b2 = buf2.data();
                    for (std::size_t i = 0; i < B; ++i) b1[i] = f(src[i], param);
                    for (std::size_t i = 0; i < B; ++i) b2[i] = f(b1[i], param);
                    for (std::size_t i = 0; i < B; ++i) sum += b2[i];
                } else if (passes) {
                    std::memcpy(b1, src, B * 8);
                    for (int p = 0; p < param; ++p) {
                        const uint64_t c = 0x9E37u + p;
                        for (std::size_t i = 0; i < B; ++i) b1[i] += (b1[i] >> 3) ^ c;
                    }
                    for (std::size_t i = 0; i < B; ++i) sum += b1[i];
                } else {
                    std::memcpy(b1, src, B * 8);
                    const uint32_t* ix = idx.data();
                    for (int p = 0; p < param; ++p)
                        for (std::size_t i = 0; i < B; ++i) sum += b1[ix[i]];
                }
            }
            sink += sum;
            const double ms = std::chrono::duration<double, std::milli>(
                                  std::chrono::steady_clock::now() - t0).count();
            best = std::min(best, ms);
        }
        // Bytes the vector keeps live: pipe 2 buffers; passes 1; probe 1 buffer + 4 B index.
        const double kb = (pipe ? 16.0 : passes ? 8.0 : 12.0) * B / 1024;
        char size[32];
        if (kb < 1024) std::snprintf(size, sizeof size, "%.0f KB", kb);
        else std::snprintf(size, sizeof size, "%.0f MB", kb / 1024);
        std::printf("%10zu %12s %10.1f %10.3f\n", B, size, best, best * 1e6 / n);
    }
    std::fprintf(stderr, "(checksum %llu)\n", static_cast<unsigned long long>(sink));
    return 0;
}
