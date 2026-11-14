#pragma once

// Scan kernels shared by every layout. A kernel works on one *chunk*: a run of n
// consecutive rows whose values it reads through an accessor:
//
//   ColumnChunk  one pointer per column to the chunk's first value (DSM: the column array at
//                the chunk's first row; PAX: a minipage; encoded DSM: a decoded vector).
//   RowChunk     a pointer to the chunk's first tuple and the tuple width (NSM).
//
// DSM and PAX run literally the same kernels on ColumnChunks; the only difference between
// them is where the chunk's columns sit in memory (separate arrays vs one page).
//
// Strategies (see Strategy in query.h):
//   early       stitch the tuple, then test it; branch-free output cursor
//   early-pipe  test the predicate attributes, fetch the rest only for qualifying rows
//   late        selection vector (branch-free), then column-at-a-time gather
//   late-branch selection vector built with a branch per row, then the same gather

#include <immintrin.h>

#include <cstddef>
#include <cstdint>
#include <type_traits>

#include "predicate.h"

namespace kern {

// Contiguous values of one column (DSM, PAX, decoded vectors).
struct Contig {
    const int64_t* p;
    int64_t operator[](std::size_t i) const { return p[i]; }
};

// One attribute of consecutive NSM tuples: every `stride`-th value.
struct Strided {
    const int64_t* p;
    std::size_t stride;
    int64_t operator[](std::size_t i) const { return p[i * stride]; }
};

struct ColumnChunk {
    static constexpr bool kColumnar = true;
    const int64_t* const* col;  // col[c] = this chunk's first value of column c
    Contig column(int c) const { return {col[c]}; }
    int64_t at(int c, std::size_t i) const { return col[c][i]; }
};

struct RowChunk {
    static constexpr bool kColumnar = false;
    const int64_t* base;  // this chunk's first tuple
    std::size_t stride;   // values per tuple
    Strided column(int c) const { return {base + c, stride}; }
    int64_t at(int c, std::size_t i) const { return base[i * stride + c]; }
    const int64_t* row(std::size_t i) const { return base + i * stride; }
};

// --- Per-row kernels: compile-time column counts -----------------------------------------
//
// A tuple-at-a-time loop does a little work per attribute of every row, so loop overhead
// and reloading the column pointers and predicate bounds from memory would dominate it.
// The per-row kernels therefore copy pointers and bounds into local arrays first and take
// the number of output columns M and of predicates NP as template parameters, so that for
// the common counts the inner loops unroll completely and everything stays in registers
// (what a query compiler would generate for one query). kDyn means "known only at run
// time"; with_counts() picks the instantiation.

constexpr int kDyn = -1;
constexpr std::size_t kMaxCols = 1024;  // run-time column counts
constexpr std::size_t kMaxPreds = 64;

template <int N>
constexpr std::size_t count_or(std::size_t runtime) {
    if constexpr (N >= 0) return static_cast<std::size_t>(N);
    else return runtime;
}

template <int N, std::size_t Max>
constexpr std::size_t capacity() {
    return N > 0 ? static_cast<std::size_t>(N) : (N == 0 ? 1 : Max);
}

// Calls f(M, NP) with std::integral_constant<int, ...> arguments: M is m for the counts
// {1, 2, 3, 4, 6, 8, 12, 16} and kDyn otherwise; NP is np for {0, 1, 2} and kDyn otherwise.
template <class F>
inline decltype(auto) with_counts(std::size_t m, std::size_t np, F&& f) {
    auto with_np = [&](auto M) -> decltype(auto) {
        switch (np) {
            case 0: return f(M, std::integral_constant<int, 0>{});
            case 1: return f(M, std::integral_constant<int, 1>{});
            case 2: return f(M, std::integral_constant<int, 2>{});
            default: return f(M, std::integral_constant<int, kDyn>{});
        }
    };
    switch (m) {
        case 1: return with_np(std::integral_constant<int, 1>{});
        case 2: return with_np(std::integral_constant<int, 2>{});
        case 3: return with_np(std::integral_constant<int, 3>{});
        case 4: return with_np(std::integral_constant<int, 4>{});
        case 6: return with_np(std::integral_constant<int, 6>{});
        case 8: return with_np(std::integral_constant<int, 8>{});
        case 12: return with_np(std::integral_constant<int, 12>{});
        case 16: return with_np(std::integral_constant<int, 16>{});
        default: return with_np(std::integral_constant<int, kDyn>{});
    }
}

// Pointers to the M output columns and the NP predicate columns of one chunk, plus the
// predicates' bounds, all in local arrays. get(base, i) reads row i of a column.
template <int M, int NP, class Chunk>
struct RowAccess {
    std::size_t m, np, stride;
    const int64_t* out[capacity<M, kMaxCols>()];
    const int64_t* pcol[capacity<NP, kMaxPreds>()];
    uint64_t plo[capacity<NP, kMaxPreds>()];
    uint64_t pspan[capacity<NP, kMaxPreds>()];

    RowAccess(const Chunk& ch, const Predicate* p, std::size_t np_rt, const int* cols,
              std::size_t m_rt)
        : m(count_or<M>(m_rt)), np(count_or<NP>(np_rt)) {
        if constexpr (Chunk::kColumnar) {
            stride = 1;
            for (std::size_t j = 0; j < m; ++j) out[j] = ch.col[cols[j]];
            for (std::size_t q = 0; q < np; ++q) pcol[q] = ch.col[p[q].col];
        } else {
            stride = ch.stride;
            for (std::size_t j = 0; j < m; ++j) out[j] = ch.base + cols[j];
            for (std::size_t q = 0; q < np; ++q) pcol[q] = ch.base + p[q].col;
        }
        for (std::size_t q = 0; q < np; ++q) {
            plo[q] = static_cast<uint64_t>(p[q].lo);
            pspan[q] = p[q].span();
        }
    }

    int64_t get(const int64_t* base, std::size_t i) const {
        if constexpr (Chunk::kColumnar) return base[i];
        else return base[i * stride];
    }
    // All predicates, no short-circuit (no branches).
    bool pass_all(std::size_t i) const {
        bool ok = true;
        for (std::size_t q = 0; q < count_or<NP>(np); ++q)
            ok &= static_cast<uint64_t>(get(pcol[q], i)) - plo[q] <= pspan[q];
        return ok;
    }
    // Short-circuit: later predicate attributes are read only if the earlier ones pass.
    bool pass_short(std::size_t i) const {
        for (std::size_t q = 0; q < count_or<NP>(np); ++q)
            if (static_cast<uint64_t>(get(pcol[q], i)) - plo[q] > pspan[q]) return false;
        return true;
    }
};

// Selection vector: positions (ascending) of the rows in [0, n) passing every predicate;
// returns how many. np >= 1. The first predicate scans its whole column; each further one
// is only evaluated at the positions that are still selected (pipelined refinement).
template <bool kBranchless, class Chunk>
inline std::size_t select(const Chunk& ch, const Predicate* p, std::size_t np, std::size_t n,
                          uint32_t* __restrict sel) {
    std::size_t cnt = 0;
    {
        const auto v = ch.column(p[0].col);
        const uint64_t lo = static_cast<uint64_t>(p[0].lo), span = p[0].span();
        for (std::size_t i = 0; i < n; ++i) {
            const bool ok = static_cast<uint64_t>(v[i]) - lo <= span;
            if constexpr (kBranchless) {
                sel[cnt] = static_cast<uint32_t>(i);
                cnt += ok;
            } else if (ok) {
                sel[cnt++] = static_cast<uint32_t>(i);
            }
        }
    }
    for (std::size_t q = 1; q < np; ++q) {
        const auto v = ch.column(p[q].col);
        const uint64_t lo = static_cast<uint64_t>(p[q].lo), span = p[q].span();
        std::size_t kept = 0;
        for (std::size_t t = 0; t < cnt; ++t) {
            const uint32_t i = sel[t];
            const bool ok = static_cast<uint64_t>(v[i]) - lo <= span;
            if constexpr (kBranchless) {
                sel[kept] = i;
                kept += ok;
            } else if (ok) {
                sel[kept++] = i;
            }
        }
        cnt = kept;
    }
    return cnt;
}

// AVX2 version of the first predicate's pass over a contiguous column: tests 8 values per
// step (two 4 x int64 compares), turns the 8 results into a bit mask and appends the
// passing offsets with one 8-lane store from a 256-entry table (entry b lists the set bits
// of b; 8 KB, so it stays in L1). sel needs room for 8 entries past the result.
struct SelectLut {
    alignas(32) uint32_t idx[256][8];
    SelectLut() {
        for (int b = 0; b < 256; ++b) {
            int n = 0;
            for (int i = 0; i < 8; ++i)
                if (b >> i & 1) idx[b][n++] = static_cast<uint32_t>(i);
            for (; n < 8; ++n) idx[b][n] = 0;
        }
    }
};

inline const SelectLut& select_lut() {
    static const SelectLut lut;
    return lut;
}

inline std::size_t select_avx2(const int64_t* __restrict v, std::size_t n, const Predicate& p,
                               uint32_t* __restrict sel) {
    const SelectLut& lut = select_lut();
    // Unsigned v - lo <= span via a signed compare: flipping the sign bit of both sides maps
    // unsigned order onto signed order. A lane fails when (v - lo) ^ sign > span ^ sign.
    const __m256i sign = _mm256_set1_epi64x(static_cast<int64_t>(1ULL << 63));
    const __m256i lo = _mm256_set1_epi64x(p.lo);
    const __m256i span = _mm256_xor_si256(_mm256_set1_epi64x(static_cast<int64_t>(p.span())), sign);
    std::size_t cnt = 0, i = 0;
    for (; i + 8 <= n; i += 8) {
        const __m256i a = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(v + i));
        const __m256i b = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(v + i + 4));
        const __m256i fa = _mm256_cmpgt_epi64(_mm256_xor_si256(_mm256_sub_epi64(a, lo), sign), span);
        const __m256i fb = _mm256_cmpgt_epi64(_mm256_xor_si256(_mm256_sub_epi64(b, lo), sign), span);
        const unsigned fail = static_cast<unsigned>(_mm256_movemask_pd(_mm256_castsi256_pd(fa))) |
                              static_cast<unsigned>(_mm256_movemask_pd(_mm256_castsi256_pd(fb))) << 4;
        const unsigned pass = ~fail & 0xffu;
        const __m256i offs = _mm256_add_epi32(
            _mm256_load_si256(reinterpret_cast<const __m256i*>(lut.idx[pass])),
            _mm256_set1_epi32(static_cast<int>(i)));
        _mm256_storeu_si256(reinterpret_cast<__m256i*>(sel + cnt), offs);
        cnt += static_cast<std::size_t>(__builtin_popcount(pass));
    }
    for (; i < n; ++i) {
        sel[cnt] = static_cast<uint32_t>(i);
        cnt += p.test(v[i]);
    }
    return cnt;
}

// select<true> with the first predicate's pass done by select_avx2 (contiguous columns only;
// NSM chunks fall back to the scalar branch-free loop).
template <class Chunk>
inline std::size_t select_simd(const Chunk& ch, const Predicate* p, std::size_t np, std::size_t n,
                               uint32_t* __restrict sel) {
    if constexpr (!Chunk::kColumnar) {
        return select<true>(ch, p, np, n, sel);
    } else {
        std::size_t cnt = select_avx2(ch.col[p[0].col], n, p[0], sel);
        for (std::size_t q = 1; q < np; ++q) {
            const int64_t* __restrict v = ch.col[p[q].col];
            std::size_t kept = 0;
            for (std::size_t t = 0; t < cnt; ++t) {
                const uint32_t i = sel[t];
                sel[kept] = i;
                kept += p[q].test(v[i]);
            }
            cnt = kept;
        }
        return cnt;
    }
}

// Copies n values to dst with non-temporal (streaming) stores: they bypass the cache and
// need no read-for-ownership, so an output larger than the LLC costs one write per line
// instead of a read plus a write, and does not evict the input.
inline void stream_copy(int64_t* __restrict dst, const int64_t* __restrict src, std::size_t n) {
    std::size_t i = 0;
    // Align dst to 32 bytes with 8-byte streaming stores, then stream 32 bytes at a time.
    for (; i < n && (reinterpret_cast<uintptr_t>(dst + i) & 31) != 0; ++i)
        _mm_stream_si64(reinterpret_cast<long long*>(dst + i), src[i]);
    for (; i + 4 <= n; i += 4)
        _mm256_stream_si256(reinterpret_cast<__m256i*>(dst + i),
                            _mm256_loadu_si256(reinterpret_cast<const __m256i*>(src + i)));
    for (; i < n; ++i) _mm_stream_si64(reinterpret_cast<long long*>(dst + i), src[i]);
}

// --- Projection: write qualifying tuples (m attributes each) row-major to out -------------

// early: stitch all m output attributes of every row, then keep the row if it passes.
// The tuple is always written; the cursor advances only for qualifying rows, so a rejected
// tuple is overwritten by the next one. Requires room for one tuple past the last
// qualifying one, which the caller guarantees (out has room for every row of the chunk).
template <int M, int NP, class Chunk>
inline std::size_t early_project(const Chunk& ch, const Predicate* p, std::size_t np,
                                 const int* cols, std::size_t m_rt, std::size_t n,
                                 int64_t* __restrict out) {
    const RowAccess<M, NP, Chunk> a(ch, p, np, cols, m_rt);
    const std::size_t m = count_or<M>(m_rt);
    std::size_t cnt = 0;
    for (std::size_t i = 0; i < n; ++i) {
        int64_t* __restrict dst = out + cnt * m;
        for (std::size_t j = 0; j < m; ++j) dst[j] = a.get(a.out[j], i);
        cnt += a.pass_all(i);
    }
    return cnt;
}

// early-pipe: test the predicate attributes; fetch the output attributes only on a match.
template <int M, int NP, class Chunk>
inline std::size_t early_pipe_project(const Chunk& ch, const Predicate* p, std::size_t np,
                                      const int* cols, std::size_t m_rt, std::size_t n,
                                      int64_t* __restrict out) {
    const RowAccess<M, NP, Chunk> a(ch, p, np, cols, m_rt);
    const std::size_t m = count_or<M>(m_rt);
    std::size_t cnt = 0;
    for (std::size_t i = 0; i < n; ++i) {
        if (!a.pass_short(i)) continue;
        int64_t* __restrict dst = out + cnt * m;
        for (std::size_t j = 0; j < m; ++j) dst[j] = a.get(a.out[j], i);
        ++cnt;
    }
    return cnt;
}

// late: write the selected rows (sel == nullptr: all cnt rows) into out. Columnar chunks are
// gathered one output column at a time, each into its slot of every output tuple; NSM rows
// are copied one tuple at a time, since a tuple's attributes are already together.
template <int M, class Chunk>
inline void gather_project(const Chunk& ch, const int* cols, std::size_t m_rt,
                           const uint32_t* __restrict sel, std::size_t cnt,
                           int64_t* __restrict out) {
    const std::size_t m = count_or<M>(m_rt);
    if constexpr (Chunk::kColumnar) {
        for (std::size_t j = 0; j < m; ++j) {
            const int64_t* __restrict v = ch.col[cols[j]];
            int64_t* __restrict o = out + j;
            if (sel != nullptr) {
                for (std::size_t t = 0; t < cnt; ++t) o[t * m] = v[sel[t]];
            } else {
                for (std::size_t t = 0; t < cnt; ++t) o[t * m] = v[t];
            }
        }
    } else {
        const RowAccess<M, 0, Chunk> a(ch, nullptr, 0, cols, m_rt);
        for (std::size_t t = 0; t < cnt; ++t) {
            const std::size_t r = sel != nullptr ? sel[t] : t;
            int64_t* __restrict dst = out + t * m;
            for (std::size_t j = 0; j < m; ++j) dst[j] = a.get(a.out[j], r);
        }
    }
}

// --- Aggregation: SUM of m attributes over qualifying rows, plus COUNT --------------------

struct SumCount {
    uint64_t sum = 0;  // wraps mod 2^64
    std::size_t count = 0;
};

// early: per row, add up its m attributes, then add that to the total if the row passes
// (masked, no branch).
template <int M, int NP, class Chunk>
inline SumCount early_sum(const Chunk& ch, const Predicate* p, std::size_t np, const int* cols,
                          std::size_t m_rt, std::size_t n) {
    const RowAccess<M, NP, Chunk> a(ch, p, np, cols, m_rt);
    const std::size_t m = count_or<M>(m_rt);
    SumCount r;
    for (std::size_t i = 0; i < n; ++i) {
        uint64_t s = 0;
        for (std::size_t j = 0; j < m; ++j) s += static_cast<uint64_t>(a.get(a.out[j], i));
        const uint64_t ok = a.pass_all(i);
        r.sum += s & (0 - ok);
        r.count += ok;
    }
    return r;
}

template <int M, int NP, class Chunk>
inline SumCount early_pipe_sum(const Chunk& ch, const Predicate* p, std::size_t np,
                               const int* cols, std::size_t m_rt, std::size_t n) {
    const RowAccess<M, NP, Chunk> a(ch, p, np, cols, m_rt);
    const std::size_t m = count_or<M>(m_rt);
    SumCount r;
    for (std::size_t i = 0; i < n; ++i) {
        if (!a.pass_short(i)) continue;
        for (std::size_t j = 0; j < m; ++j) r.sum += static_cast<uint64_t>(a.get(a.out[j], i));
        ++r.count;
    }
    return r;
}

// late: one column at a time over the selected positions (sel == nullptr: all cnt rows).
template <int M, class Chunk>
inline uint64_t gather_sum(const Chunk& ch, const int* cols, std::size_t m_rt,
                           const uint32_t* __restrict sel, std::size_t cnt) {
    const std::size_t m = count_or<M>(m_rt);
    uint64_t sum = 0;
    if constexpr (Chunk::kColumnar) {
        for (std::size_t j = 0; j < m; ++j) {
            const int64_t* __restrict v = ch.col[cols[j]];
            uint64_t s = 0;
            if (sel != nullptr) {
                for (std::size_t t = 0; t < cnt; ++t) s += static_cast<uint64_t>(v[sel[t]]);
            } else {
                for (std::size_t t = 0; t < cnt; ++t) s += static_cast<uint64_t>(v[t]);
            }
            sum += s;
        }
    } else {
        const RowAccess<M, 0, Chunk> a(ch, nullptr, 0, cols, m_rt);
        for (std::size_t t = 0; t < cnt; ++t) {
            const std::size_t r = sel != nullptr ? sel[t] : t;
            for (std::size_t j = 0; j < m; ++j) sum += static_cast<uint64_t>(a.get(a.out[j], r));
        }
    }
    return sum;
}

}  // namespace kern
