// membench: measures the memory hierarchy the scan engine runs on, as a function of the
// working-set size:
//   latency    dependent loads (pointer chasing through a random cycle of cache lines),
//              so no two loads overlap and the hardware prefetcher cannot help: ns per load
//   read       sequential read bandwidth (sum of an int64 array, vectorized): GB/s
//   write      sequential write bandwidth (fill an int64 array): GB/s
//   write_nt   sequential write bandwidth with non-temporal (streaming) stores: GB/s
//
// Each measurement repeats over the buffer until at least 50 ms have passed and reports
// the best of 5 such samples. Output is CSV on stdout:
//   test,bytes,value
// Usage: membench [--max-mb M] [--hugepages]
#include <immintrin.h>
#include <sys/mman.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <numeric>
#include <random>
#include <string>
#include <vector>

namespace {

volatile uint64_t g_sink;

double now_ns() {
    return std::chrono::duration<double, std::nano>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

// Calls f(reps) with growing reps until one call lasts >= 50 ms; returns the best ns per
// rep over 5 calls at that count.
template <class F>
double measure(F&& f) {
    long reps = 1;
    for (;;) {
        double t0 = now_ns();
        f(reps);
        double dt = now_ns() - t0;
        if (dt >= 50e6) break;
        reps = std::max<long>(reps * 2, static_cast<long>(reps * 60e6 / std::max(dt, 1.0)));
    }
    double best = 1e300;
    for (int i = 0; i < 5; ++i) {
        double t0 = now_ns();
        f(reps);
        best = std::min(best, (now_ns() - t0) / reps);
    }
    return best;
}

struct Buffer {
    uint64_t* p = nullptr;
    std::size_t bytes = 0;
    Buffer(std::size_t b, bool huge) : bytes(b) {
        const std::size_t align = huge ? (2u << 20) : 4096;
        const std::size_t rounded = (b + align - 1) / align * align;
        p = static_cast<uint64_t*>(std::aligned_alloc(align, rounded));
        if (huge) madvise(p, rounded, MADV_HUGEPAGE);
        std::memset(p, 1, rounded);
    }
    ~Buffer() { std::free(p); }
};

double latency_ns(std::size_t bytes, bool huge) {
    const std::size_t lines = bytes / 64;
    Buffer buf(bytes, huge);
    // A random cyclic order of all lines; line i stores the address of the next line.
    std::vector<std::size_t> order(lines);
    std::iota(order.begin(), order.end(), 0);
    std::shuffle(order.begin() + 1, order.end(), std::mt19937_64(7));
    for (std::size_t i = 0; i < lines; ++i) {
        uint64_t* from = buf.p + order[i] * 8;
        uint64_t* to = buf.p + order[(i + 1) % lines] * 8;
        *reinterpret_cast<uint64_t**>(from) = to;
    }
    const std::size_t steps = std::max<std::size_t>(lines, 1 << 16);
    double per_rep = measure([&](long reps) {
        uint64_t* p = buf.p;
        for (long r = 0; r < reps; ++r)
            for (std::size_t s = 0; s < steps; ++s) p = *reinterpret_cast<uint64_t**>(p);
        g_sink = reinterpret_cast<uint64_t>(p);
    });
    return per_rep / steps;
}

double read_gbps(std::size_t bytes, bool huge) {
    Buffer buf(bytes, huge);
    const std::size_t n = bytes / 8;
    double per_rep = measure([&](long reps) {
        uint64_t s0 = 0, s1 = 0, s2 = 0, s3 = 0;
        for (long r = 0; r < reps; ++r) {
            const uint64_t* p = buf.p;
            for (std::size_t i = 0; i < n; i += 4) {
                s0 += p[i];
                s1 += p[i + 1];
                s2 += p[i + 2];
                s3 += p[i + 3];
            }
        }
        g_sink = s0 + s1 + s2 + s3;
    });
    return bytes / per_rep;
}

double write_gbps(std::size_t bytes, bool huge, bool nt) {
    Buffer buf(bytes, huge);
    const std::size_t n = bytes / 8;
    double per_rep = measure([&](long reps) {
        for (long r = 0; r < reps; ++r) {
            uint64_t* p = buf.p;
            if (nt) {
                const __m256i v = _mm256_set1_epi64x(r);
                for (std::size_t i = 0; i < n; i += 4)
                    _mm256_stream_si256(reinterpret_cast<__m256i*>(p + i), v);
                _mm_sfence();
            } else {
                for (std::size_t i = 0; i < n; ++i) p[i] = static_cast<uint64_t>(r);
            }
        }
        g_sink = buf.p[n / 2];
    });
    return bytes / per_rep;
}

}  // namespace

int main(int argc, char** argv) {
    std::size_t max_mb = 512;
    bool huge = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--max-mb" && i + 1 < argc) max_mb = std::stoull(argv[++i]);
        else if (a == "--hugepages") huge = true;
        else {
            std::fprintf(stderr, "usage: %s [--max-mb M] [--hugepages]\n", argv[0]);
            return 1;
        }
    }
    std::printf("test,bytes,value\n");
    // Sizes 4 KB .. max_mb, four points per doubling.
    std::vector<std::size_t> sizes;
    for (double b = 4096; b <= max_mb * 1048576.0 + 1; b *= 1.189207115)  // 2^(1/4)
        sizes.push_back(static_cast<std::size_t>(b) / 256 * 256);
    for (std::size_t b : sizes) {
        std::printf("latency,%zu,%.3f\n", b, latency_ns(b, huge));
        std::printf("read,%zu,%.3f\n", b, read_gbps(b, huge));
        std::printf("write,%zu,%.3f\n", b, write_gbps(b, huge, false));
        std::printf("write_nt,%zu,%.3f\n", b, write_gbps(b, huge, true));
        std::fflush(stdout);
    }
    return 0;
}
