// dictb: dict with byte-aligned codes. The same sorted dictionary as dict, but each code is
// stored in 1, 2 or 4 bytes (the smallest that holds dict.size() - 1) instead of exactly
// width_for(dict.size() - 1) bits. That gives up some compression when the width is not a
// multiple of 8 (14 bits become 16), but a code needs no unpacking: a predicate compares
// codes straight from memory, 32 one-byte or 16 two-byte codes per AVX2 instruction.
#include <immintrin.h>

#include <algorithm>
#include <type_traits>

#include "encodings/bits.h"
#include "encodings/encoded_column.h"
#include "kernels.h"

namespace {

// Appends to sel, for each of the 32 codes behind `mask` (bit i set = code i passes), base + i.
// Branch-free: each byte of the mask writes its 8 LUT entries and advances by its popcount.
__attribute__((target("avx2"))) inline std::size_t expand_mask(uint32_t mask, uint32_t base,
                                                               uint32_t* sel, std::size_t cnt) {
    const kern::SelectLut& lut = kern::select_lut();
    for (unsigned b = 0; b < 4; ++b, mask >>= 8, base += 8) {
        const unsigned m = mask & 0xffu;
        const __m256i offs = _mm256_add_epi32(
            _mm256_load_si256(reinterpret_cast<const __m256i*>(lut.idx[m])),
            _mm256_set1_epi32(static_cast<int>(base)));
        _mm256_storeu_si256(reinterpret_cast<__m256i*>(sel + cnt), offs);
        cnt += static_cast<std::size_t>(__builtin_popcount(m));
    }
    return cnt;
}

// 32 codes starting at c: bit i of the result is set when c[i] - lo <= span (unsigned, so
// lo <= c[i] <= lo + span). x <= span is tested as min(x, span) == x.
template <class C>
__attribute__((target("avx2"))) inline uint32_t match32(const C* c, __m256i vlo, __m256i vspan) {
    const auto ld = [&](unsigned k) {
        return _mm256_loadu_si256(reinterpret_cast<const __m256i*>(c) + k);
    };
    if constexpr (sizeof(C) == 1) {
        const __m256i x = _mm256_sub_epi8(ld(0), vlo);
        return static_cast<uint32_t>(
            _mm256_movemask_epi8(_mm256_cmpeq_epi8(_mm256_min_epu8(x, vspan), x)));
    } else if constexpr (sizeof(C) == 2) {
        const __m256i x0 = _mm256_sub_epi16(ld(0), vlo), x1 = _mm256_sub_epi16(ld(1), vlo);
        const __m256i r0 = _mm256_cmpeq_epi16(_mm256_min_epu16(x0, vspan), x0);
        const __m256i r1 = _mm256_cmpeq_epi16(_mm256_min_epu16(x1, vspan), x1);
        // packs works within 128-bit halves: [r0 lo, r1 lo, r0 hi, r1 hi]; reorder the
        // 64-bit quarters to [r0 lo, r0 hi, r1 lo, r1 hi], one byte per code in row order.
        const __m256i packed = _mm256_permute4x64_epi64(_mm256_packs_epi16(r0, r1), 0xD8);
        return static_cast<uint32_t>(_mm256_movemask_epi8(packed));
    } else {
        uint32_t mask = 0;
        for (unsigned k = 0; k < 4; ++k) {
            const __m256i x = _mm256_sub_epi32(ld(k), vlo);
            const __m256i r = _mm256_cmpeq_epi32(_mm256_min_epu32(x, vspan), x);
            mask |= static_cast<uint32_t>(_mm256_movemask_ps(_mm256_castsi256_ps(r))) << (8 * k);
        }
        return mask;
    }
}

// With `skip_empty`, a 32-code group without a match costs only the test; that is the faster
// choice when matches are rare enough for the test to be predictable (see select()).
template <class C, bool skip_empty>
__attribute__((target("avx2"))) std::size_t select_codes_avx2(const C* c, std::size_t n,
                                                              uint32_t lo, uint32_t span,
                                                              uint32_t* sel) {
    __m256i vlo, vspan;
    if constexpr (sizeof(C) == 1) {
        vlo = _mm256_set1_epi8(static_cast<char>(lo));
        vspan = _mm256_set1_epi8(static_cast<char>(span));
    } else if constexpr (sizeof(C) == 2) {
        vlo = _mm256_set1_epi16(static_cast<short>(lo));
        vspan = _mm256_set1_epi16(static_cast<short>(span));
    } else {
        vlo = _mm256_set1_epi32(static_cast<int>(lo));
        vspan = _mm256_set1_epi32(static_cast<int>(span));
    }
    std::size_t cnt = 0, i = 0;
    for (; i + 32 <= n; i += 32) {
        const uint32_t mask = match32(c + i, vlo, vspan);
        if (skip_empty && mask == 0) continue;
        cnt = expand_mask(mask, static_cast<uint32_t>(i), sel, cnt);
    }
    for (; i < n; ++i) {
        sel[cnt] = static_cast<uint32_t>(i);
        cnt += static_cast<C>(c[i] - lo) <= span;
    }
    return cnt;
}

template <class C>
class DictBCursor final : public ColumnCursor {
public:
    DictBCursor(const int64_t* dict, const C* codes) : dict_(dict), codes_(codes) {}
    int64_t next() override { return dict_[*codes_++]; }

private:
    const int64_t* dict_;
    const C* codes_;
};

template <class C>
class DictBColumn final : public EncodedColumn {
public:
    DictBColumn(std::vector<int64_t> dict, const std::vector<int64_t>& values)
        : dict_(std::move(dict)), codes_(values.size()) {
        for (std::size_t i = 0; i < values.size(); ++i)
            codes_[i] = static_cast<C>(std::lower_bound(dict_.begin(), dict_.end(), values[i]) -
                                       dict_.begin());
    }

    std::size_t size() const override { return codes_.size(); }
    std::size_t footprint_bytes() const override {
        return dict_.size() * sizeof(int64_t) + codes_.size() * sizeof(C);
    }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        for (std::size_t i = 0; i < n; ++i) out[i] = dict_[codes_[start + i]];
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<DictBCursor<C>>(dict_.data(), codes_.data());
    }

    // As in dict, [lo, hi] on values becomes a range of codes; the codes are then compared
    // where they lie, with no unpacking.
    std::size_t select(std::size_t start, std::size_t n, const Predicate& p, uint32_t* sel,
                       int64_t*) const override {
        uint32_t lo, span;
        if (!code_range(p, lo, span)) return 0;
        const C* c = codes_.data() + start;
        if (bits::simd()) {
            // Skipping groups without a match is a branch. It is predictable, and saves the
            // expansion, only when matches are rare: at 1%, 27% of 32-code groups hold one and
            // the branch mispredicts so often that it costs 0.6 ms per 4M rows, while at 0.05%
            // skipping makes the scan 2x faster. Decide from the previous call's selectivity
            // (below 0.2%, 6% of groups hold a match).
            const bool rare = prev_cnt_ * 500 < prev_n_;
            const std::size_t cnt = rare ? select_codes_avx2<C, true>(c, n, lo, span, sel)
                                         : select_codes_avx2<C, false>(c, n, lo, span, sel);
            prev_cnt_ = cnt;
            prev_n_ = n;
            return cnt;
        }
        std::size_t cnt = 0;
        for (std::size_t i = 0; i < n; ++i) {
            sel[cnt] = static_cast<uint32_t>(i);
            cnt += static_cast<uint32_t>(c[i] - lo) <= span;
        }
        return cnt;
    }

    std::size_t refine(std::size_t start, uint32_t* sel, std::size_t cnt, const Predicate& p,
                       int64_t*) const override {
        uint32_t lo, span;
        if (!code_range(p, lo, span)) return 0;
        std::size_t kept = 0;
        for (std::size_t t = 0; t < cnt; ++t) {
            const uint32_t pos = sel[t];
            sel[kept] = pos;
            kept += static_cast<uint32_t>(codes_[start + pos] - lo) <= span;
        }
        return kept;
    }

    void gather(std::size_t start, const uint32_t* sel, std::size_t cnt, int64_t* out,
                std::size_t stride, int64_t*) const override {
        for (std::size_t t = 0; t < cnt; ++t) out[t * stride] = dict_[codes_[start + sel[t]]];
    }

    // One lookup per row, added straight away (no decoded vector).
    uint64_t sum(std::size_t start, std::size_t n, int64_t*) const override {
        uint64_t total = 0;
        const C* c = codes_.data() + start;
        for (std::size_t i = 0; i < n; ++i) total += static_cast<uint64_t>(dict_[c[i]]);
        return total;
    }

private:
    // Codes whose values lie in [p.lo, p.hi], as [lo, lo + span]; false if none.
    bool code_range(const Predicate& p, uint32_t& lo, uint32_t& span) const {
        const auto first = std::lower_bound(dict_.begin(), dict_.end(), p.lo);
        const auto last = std::upper_bound(first, dict_.end(), p.hi);
        if (first == last) return false;
        lo = static_cast<uint32_t>(first - dict_.begin());
        span = static_cast<uint32_t>(last - first) - 1;
        return true;
    }

    std::vector<int64_t> dict_;
    std::vector<C> codes_;
    // Selectivity of the previous select() (one scan at a time); starts as "rare".
    mutable std::size_t prev_cnt_ = 0, prev_n_ = 1;
};

}  // namespace

std::unique_ptr<EncodedColumn> make_dictb_column(const std::vector<int64_t>& values) {
    std::vector<int64_t> dict(values);
    std::sort(dict.begin(), dict.end());
    dict.erase(std::unique(dict.begin(), dict.end()), dict.end());
    if (dict.size() <= (1u << 8)) return std::make_unique<DictBColumn<uint8_t>>(std::move(dict), values);
    if (dict.size() <= (1u << 16))
        return std::make_unique<DictBColumn<uint16_t>>(std::move(dict), values);
    return std::make_unique<DictBColumn<uint32_t>>(std::move(dict), values);
}
