#include "encodings/bits.h"

#include <immintrin.h>

#include <algorithm>
#include <array>
#include <utility>

namespace bits {

void pack(const uint64_t* values, std::size_t n, unsigned w, std::vector<uint64_t>& out) {
    const std::size_t base = out.size();
    out.resize(base + words_for(n, w), 0);
    if (w == 0) return;
    uint64_t* dst = out.data() + base;
    for (std::size_t i = 0; i < n; ++i) {
        const std::size_t bit = i * w;
        const std::size_t word = bit / 64;
        const unsigned shift = bit % 64;
        dst[word] |= values[i] << shift;
        if (shift + w > 64) dst[word + 1] |= values[i] >> (64 - shift);
    }
}

namespace {

// One group of 64 values at compile-time width W. After unrolling, every word index,
// shift and the "crosses a word" test are constants.
template <unsigned W>
inline void unpack_group(const uint64_t* in, uint64_t* out) {
    if constexpr (W == 0) {
        for (unsigned i = 0; i < kGroup; ++i) out[i] = 0;
    } else {
        constexpr uint64_t mask = W == 64 ? ~uint64_t{0} : (uint64_t{1} << W) - 1;
#pragma GCC unroll 64
        for (unsigned i = 0; i < kGroup; ++i) {
            const unsigned bit = i * W;
            const unsigned word = bit / 64;
            const unsigned shift = bit % 64;
            uint64_t v = in[word] >> shift;
            if (shift + W > 64) v |= in[word + 1] << ((64 - shift) & 63);
            out[i] = v & mask;
        }
    }
}

template <unsigned W>
void unpack_groups(const uint64_t* in, std::size_t groups, uint64_t* out, uint64_t base) {
    for (std::size_t g = 0; g < groups; ++g) unpack_group<W>(in + g * W, out + g * kGroup);
    if (base != 0)
        for (std::size_t i = 0; i < groups * kGroup; ++i) out[i] += base;
}

// --- AVX2 ---------------------------------------------------------------------------------
// Bits are LSB first, so on a little-endian machine the packed run is also a bit stream in
// byte order, and 8 consecutive values of width W occupy exactly W bytes: every step of 8
// values starts on a byte boundary. Value j of a step starts at bit j*W. For each of the 8
// 32-bit lanes, one shuffle moves the 4 bytes holding that value into the lane and a
// variable shift drops the bits before it (at most 7, so W + 7 <= 32 bits must fit: W <= 25).
// vpshufb cannot move bytes across 128-bit halves, so the upper four lanes shuffle from a
// second 16-byte load that starts H = floor(4W / 8) bytes in.
constexpr unsigned kMaxSimdWidth = 25;

template <unsigned W>
struct SimdConsts {
    alignas(32) int8_t shuf[32];
    alignas(32) int32_t shift[8];
    static constexpr unsigned H = 4 * W / 8;
    constexpr SimdConsts() : shuf{}, shift{} {
        for (unsigned j = 0; j < 8; ++j) {
            const unsigned rel = j * W - (j < 4 ? 0 : 8 * H);  // bit offset in its 16-byte load
            for (unsigned b = 0; b < 4; ++b) shuf[j * 4 + b] = static_cast<int8_t>(rel / 8 + b);
            shift[j] = static_cast<int32_t>(rel % 8);
        }
    }
};

template <unsigned W>
__attribute__((target("avx2"))) void unpack_groups_avx2(const uint64_t* in, std::size_t groups,
                                                         uint64_t* out, uint64_t base) {
    static constexpr SimdConsts<W> c{};
    constexpr unsigned H = SimdConsts<W>::H;
    const __m256i shuf = _mm256_load_si256(reinterpret_cast<const __m256i*>(c.shuf));
    const __m256i shift = _mm256_load_si256(reinterpret_cast<const __m256i*>(c.shift));
    const __m256i mask = _mm256_set1_epi32(static_cast<int32_t>((1u << W) - 1));
    const __m256i b64 = _mm256_set1_epi64x(static_cast<int64_t>(base));
    const uint8_t* p = reinterpret_cast<const uint8_t*>(in);
    const std::size_t steps = groups * (kGroup / 8);
    for (std::size_t t = 0; t < steps; ++t, p += W, out += 8) {
        const __m128i lo = _mm_loadu_si128(reinterpret_cast<const __m128i*>(p));
        const __m128i hi = _mm_loadu_si128(reinterpret_cast<const __m128i*>(p + H));
        __m256i v = _mm256_inserti128_si256(_mm256_castsi128_si256(lo), hi, 1);
        v = _mm256_and_si256(_mm256_srlv_epi32(_mm256_shuffle_epi8(v, shuf), shift), mask);
        const __m256i a = _mm256_add_epi64(_mm256_cvtepu32_epi64(_mm256_castsi256_si128(v)), b64);
        const __m256i b = _mm256_add_epi64(_mm256_cvtepu32_epi64(_mm256_extracti128_si256(v, 1)), b64);
        _mm256_storeu_si256(reinterpret_cast<__m256i*>(out), a);
        _mm256_storeu_si256(reinterpret_cast<__m256i*>(out + 4), b);
    }
}

// The same steps as unpack_groups_avx2, but the 8 values are added into four 64-bit lane
// sums instead of being stored.
template <unsigned W>
__attribute__((target("avx2"))) uint64_t sum_groups_avx2(const uint64_t* in, std::size_t groups) {
    static constexpr SimdConsts<W> c{};
    constexpr unsigned H = SimdConsts<W>::H;
    const __m256i shuf = _mm256_load_si256(reinterpret_cast<const __m256i*>(c.shuf));
    const __m256i shift = _mm256_load_si256(reinterpret_cast<const __m256i*>(c.shift));
    const __m256i mask = _mm256_set1_epi32(static_cast<int32_t>((1u << W) - 1));
    __m256i acc = _mm256_setzero_si256();
    const uint8_t* p = reinterpret_cast<const uint8_t*>(in);
    const std::size_t steps = groups * (kGroup / 8);
    for (std::size_t t = 0; t < steps; ++t, p += W) {
        const __m128i lo = _mm_loadu_si128(reinterpret_cast<const __m128i*>(p));
        const __m128i hi = _mm_loadu_si128(reinterpret_cast<const __m128i*>(p + H));
        __m256i v = _mm256_inserti128_si256(_mm256_castsi128_si256(lo), hi, 1);
        v = _mm256_and_si256(_mm256_srlv_epi32(_mm256_shuffle_epi8(v, shuf), shift), mask);
        acc = _mm256_add_epi64(acc, _mm256_cvtepu32_epi64(_mm256_castsi256_si128(v)));
        acc = _mm256_add_epi64(acc, _mm256_cvtepu32_epi64(_mm256_extracti128_si256(v, 1)));
    }
    alignas(32) uint64_t lanes[4];
    _mm256_store_si256(reinterpret_cast<__m256i*>(lanes), acc);
    return lanes[0] + lanes[1] + lanes[2] + lanes[3];
}

// Sum over a block of m zigzag-encoded values x_k of unzigzag(x_k) * (m - k) (delta's SUM, see
// delta.cpp), with the same unpacking steps but no multiplications in the loop. Write
// k = 8t + j (step t, lane j) and pad to M = 8 * steps values (the padding deltas are zero).
// Each lane keeps S_j, the sum of its deltas so far, and R_j, the sum of S_j over all steps,
// so that at the end R_j = sum_t d_tj * (steps - t). Then
//     sum_k d_k (M - k) = 8 * sum_j R_j - sum_j j * S_j,
// and subtracting (M - m) * sum_j S_j gives the weights m - k. Two additions per lane and
// step; the deltas are unzigzagged in 32-bit lanes and sign-extended to 64 bits.
template <unsigned W>
__attribute__((target("avx2"))) uint64_t wzz_groups_avx2(const uint64_t* in, std::size_t groups,
                                                         uint64_t m) {
    static constexpr SimdConsts<W> c{};
    constexpr unsigned H = SimdConsts<W>::H;
    const __m256i shuf = _mm256_load_si256(reinterpret_cast<const __m256i*>(c.shuf));
    const __m256i shift = _mm256_load_si256(reinterpret_cast<const __m256i*>(c.shift));
    const __m256i mask = _mm256_set1_epi32(static_cast<int32_t>((1u << W) - 1));
    const __m256i one = _mm256_set1_epi32(1);
    __m256i sa = _mm256_setzero_si256(), sb = sa, ra = sa, rb = sa;
    const uint8_t* p = reinterpret_cast<const uint8_t*>(in);
    const std::size_t steps = groups * (kGroup / 8);
    for (std::size_t t = 0; t < steps; ++t, p += W) {
        const __m128i lo = _mm_loadu_si128(reinterpret_cast<const __m128i*>(p));
        const __m128i hi = _mm_loadu_si128(reinterpret_cast<const __m128i*>(p + H));
        __m256i z = _mm256_inserti128_si256(_mm256_castsi128_si256(lo), hi, 1);
        z = _mm256_and_si256(_mm256_srlv_epi32(_mm256_shuffle_epi8(z, shuf), shift), mask);
        // unzigzag: (z >> 1) ^ -(z & 1), a signed 32-bit delta
        const __m256i d = _mm256_xor_si256(_mm256_srli_epi32(z, 1),
                                           _mm256_sub_epi32(_mm256_setzero_si256(),
                                                            _mm256_and_si256(z, one)));
        sa = _mm256_add_epi64(sa, _mm256_cvtepi32_epi64(_mm256_castsi256_si128(d)));
        sb = _mm256_add_epi64(sb, _mm256_cvtepi32_epi64(_mm256_extracti128_si256(d, 1)));
        ra = _mm256_add_epi64(ra, sa);
        rb = _mm256_add_epi64(rb, sb);
    }
    alignas(32) uint64_t s[8], r[8];
    _mm256_store_si256(reinterpret_cast<__m256i*>(s), sa);
    _mm256_store_si256(reinterpret_cast<__m256i*>(s + 4), sb);
    _mm256_store_si256(reinterpret_cast<__m256i*>(r), ra);
    _mm256_store_si256(reinterpret_cast<__m256i*>(r + 4), rb);
    uint64_t total = 0, all = 0;
    for (unsigned j = 0; j < 8; ++j) {
        total += 8 * r[j] - j * s[j];
        all += s[j];
    }
    return total - (8 * steps - m) * all;
}

using WzzFn = uint64_t (*)(const uint64_t*, std::size_t, uint64_t);

template <std::size_t... W>
constexpr std::array<WzzFn, sizeof...(W)> make_wzz_table(std::index_sequence<W...>) {
    return {&wzz_groups_avx2<static_cast<unsigned>(W + 1)>...};
}

constexpr auto kWzzSimd = make_wzz_table(std::make_index_sequence<kMaxSimdWidth>{});

using SumFn = uint64_t (*)(const uint64_t*, std::size_t);

template <std::size_t... W>
constexpr std::array<SumFn, sizeof...(W)> make_sum_table(std::index_sequence<W...>) {
    return {&sum_groups_avx2<static_cast<unsigned>(W + 1)>...};
}

constexpr auto kSumSimd = make_sum_table(std::make_index_sequence<kMaxSimdWidth>{});

using UnpackFn = void (*)(const uint64_t*, std::size_t, uint64_t*, uint64_t);

template <std::size_t... W>
constexpr std::array<UnpackFn, sizeof...(W)> make_table(std::index_sequence<W...>) {
    return {&unpack_groups<static_cast<unsigned>(W)>...};
}

template <std::size_t... W>
constexpr std::array<UnpackFn, sizeof...(W) + 1> make_simd_table(std::index_sequence<W...>) {
    // Width 0 has no bits to unpack; it keeps the scalar kernel (which writes zeros).
    return {&unpack_groups<0>, &unpack_groups_avx2<static_cast<unsigned>(W + 1)>...};
}

constexpr auto kUnpack = make_table(std::make_index_sequence<65>{});
constexpr auto kUnpackSimd = make_simd_table(std::make_index_sequence<kMaxSimdWidth>{});

bool g_simd = false;

}  // namespace

void set_simd(bool on) { g_simd = on && __builtin_cpu_supports("avx2"); }
bool simd() { return g_simd; }

void unpack(const uint64_t* in, unsigned w, std::size_t groups, uint64_t* out, uint64_t base) {
    if (g_simd && w <= kMaxSimdWidth) kUnpackSimd[w](in, groups, out, base);
    else kUnpack[w](in, groups, out, base);
}

uint64_t sum(const uint64_t* in, unsigned w, std::size_t n) {
    if (w == 0 || n == 0) return 0;
    const std::size_t full = n / kGroup, tail = n % kGroup;
    uint64_t total = 0;
    if (g_simd && w <= kMaxSimdWidth) {
        total = kSumSimd[w - 1](in, full);
    } else {
        uint64_t buf[kGroup];
        for (std::size_t g = 0; g < full; ++g) {
            kUnpack[w](in + g * w, 1, buf, 0);
            for (uint64_t v : buf) total += v;
        }
    }
    if (tail) {
        // The last group may hold values past n (or padding zeros): unpack and add only n.
        uint64_t buf[kGroup];
        kUnpack[w](in + full * w, 1, buf, 0);
        for (std::size_t i = 0; i < tail; ++i) total += buf[i];
    }
    return total;
}

uint64_t weighted_zigzag_sum(const uint64_t* in, unsigned w, std::size_t m) {
    if (w == 0 || m == 0) return 0;
    if (g_simd && w <= kMaxSimdWidth) return kWzzSimd[w - 1](in, groups_for(m), m);
    uint64_t total = 0, buf[kGroup];
    for (std::size_t g = 0; g * kGroup < m; ++g) {
        kUnpack[w](in + g * w, 1, buf, 0);
        const std::size_t len = std::min(kGroup, m - g * kGroup);
        for (std::size_t i = 0; i < len; ++i)
            total += unzigzag(buf[i]) * (m - (g * kGroup + i));
    }
    return total;
}

}  // namespace bits
