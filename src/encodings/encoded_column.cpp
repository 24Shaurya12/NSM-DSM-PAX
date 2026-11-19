#include "encodings/encoded_column.h"

#include <immintrin.h>

#include <algorithm>
#include <stdexcept>

#include "encodings/bits.h"
#include "kernels.h"

namespace {

struct EncodingInfo {
    Encoding encoding;
    const char* name;
    std::unique_ptr<EncodedColumn> (*make)(const std::vector<int64_t>&);
};

// To add an encoding: write src/encodings/<name>.cpp and add one entry here.
const EncodingInfo kEncodings[] = {
    {Encoding::Plain, "plain", make_plain_column},
    {Encoding::Bitpack, "bitpack", make_bitpack_column},
    {Encoding::For, "for", make_for_column},
    {Encoding::Dict, "dict", make_dict_column},
    {Encoding::DictB, "dictb", make_dictb_column},
    {Encoding::Rle, "rle", make_rle_column},
    {Encoding::Delta, "delta", make_delta_column},
};

const EncodingInfo& info(Encoding e) {
    for (const auto& i : kEncodings)
        if (i.encoding == e) return i;
    throw std::logic_error("unregistered encoding");
}

}  // namespace

const std::vector<Encoding>& all_encodings() {
    static const std::vector<Encoding> all = [] {
        std::vector<Encoding> v;
        for (const auto& i : kEncodings) v.push_back(i.encoding);
        return v;
    }();
    return all;
}

std::string encoding_name(Encoding e) { return info(e).name; }

bool parse_encoding(const std::string& name, Encoding& out) {
    for (const auto& i : kEncodings) {
        if (name == i.name) {
            out = i.encoding;
            return true;
        }
    }
    return false;
}

std::unique_ptr<EncodedColumn> encode_column(Encoding e, const std::vector<int64_t>& values) {
    return info(e).make(values);
}

// --- Default predicate / gather implementations: decode, then work on plain values. ---

namespace {

// Largest decode() range that starts at `row` (rounded down to a multiple of 64) and stays in
// row's block: [from, block end).
inline std::size_t block_end(std::size_t row) { return row / kBlock * kBlock + kBlock; }

}  // namespace

uint64_t EncodedColumn::sum(std::size_t start, std::size_t n, int64_t* scratch) const {
    uint64_t total = 0;
    for (std::size_t off = 0; off < n;) {
        const std::size_t s = start + off;
        const std::size_t len = std::min(n - off, kBlock - s % kBlock);
        decode(s, len, scratch);
        for (std::size_t i = 0; i < len; ++i) total += static_cast<uint64_t>(scratch[i]);
        off += len;
    }
    return total;
}

void decode_range(const EncodedColumn& col, std::size_t start, std::size_t n, int64_t* out) {
    for (std::size_t off = 0; off < n;) {
        const std::size_t s = start + off;
        const std::size_t len = std::min(n - off, block_end(s) - s);
        col.decode(s, len, out + off);
        off += len;
    }
}

std::size_t EncodedColumn::select(std::size_t start, std::size_t n, const Predicate& p,
                                  uint32_t* sel, int64_t* scratch) const {
    const uint64_t lo = static_cast<uint64_t>(p.lo), span = p.span();
    std::size_t cnt = 0;
    for (std::size_t off = 0; off < n;) {
        const std::size_t s = start + off;
        const std::size_t len = std::min(n - off, block_end(s) - s);
        decode(s, len, scratch);
        for (std::size_t i = 0; i < len; ++i) {
            sel[cnt] = static_cast<uint32_t>(off + i);
            cnt += static_cast<uint64_t>(scratch[i]) - lo <= span;
        }
        off += len;
    }
    return cnt;
}

// Walks the ascending offsets block by block. For each block it decodes [from, last + 1),
// where from is the first offset's row rounded down to 64, and calls f(t, value) for every
// offset t in that block.
template <class F>
static void for_each_selected(const EncodedColumn& col, std::size_t start, const uint32_t* sel,
                              std::size_t cnt, int64_t* scratch, F&& f) {
    for (std::size_t t = 0; t < cnt;) {
        const std::size_t first_row = start + sel[t];
        const std::size_t from = first_row & ~std::size_t{63};
        const std::size_t end = block_end(first_row);
        std::size_t u = t;
        while (u < cnt && start + sel[u] < end) ++u;
        const std::size_t last_row = start + sel[u - 1];
        col.decode(from, last_row + 1 - from, scratch);
        for (; t < u; ++t) f(t, scratch[start + sel[t] - from]);
    }
}

std::size_t EncodedColumn::refine(std::size_t start, uint32_t* sel, std::size_t cnt,
                                  const Predicate& p, int64_t* scratch) const {
    std::size_t kept = 0;
    for_each_selected(*this, start, sel, cnt, scratch, [&](std::size_t t, int64_t v) {
        const uint32_t pos = sel[t];  // read before the write below can overwrite it
        sel[kept] = pos;
        kept += p.test(v);
    });
    return kept;
}

void EncodedColumn::gather(std::size_t start, const uint32_t* sel, std::size_t cnt, int64_t* out,
                           std::size_t stride, int64_t* scratch) const {
    for_each_selected(*this, start, sel, cnt, scratch,
                      [&](std::size_t t, int64_t v) { out[t * stride] = v; });
}

namespace {

__attribute__((target("avx2"))) std::size_t select_unpacked_avx2(
    const uint64_t* u, std::size_t n, uint64_t lo, uint64_t span, uint32_t base, uint32_t* sel,
    std::size_t cnt) {
    const kern::SelectLut& lut = kern::select_lut();
    // Unsigned (u - lo) <= span via a signed compare after flipping the sign bits.
    const __m256i sign = _mm256_set1_epi64x(static_cast<int64_t>(1ULL << 63));
    const __m256i vlo = _mm256_set1_epi64x(static_cast<int64_t>(lo));
    const __m256i vspan = _mm256_xor_si256(_mm256_set1_epi64x(static_cast<int64_t>(span)), sign);
    std::size_t i = 0;
    for (; i + 8 <= n; i += 8) {
        const __m256i a = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(u + i));
        const __m256i b = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(u + i + 4));
        const __m256i fa = _mm256_cmpgt_epi64(_mm256_xor_si256(_mm256_sub_epi64(a, vlo), sign), vspan);
        const __m256i fb = _mm256_cmpgt_epi64(_mm256_xor_si256(_mm256_sub_epi64(b, vlo), sign), vspan);
        const unsigned fail = static_cast<unsigned>(_mm256_movemask_pd(_mm256_castsi256_pd(fa))) |
                              static_cast<unsigned>(_mm256_movemask_pd(_mm256_castsi256_pd(fb))) << 4;
        const unsigned pass = ~fail & 0xffu;
        const __m256i offs = _mm256_add_epi32(
            _mm256_load_si256(reinterpret_cast<const __m256i*>(lut.idx[pass])),
            _mm256_set1_epi32(static_cast<int>(base + i)));
        _mm256_storeu_si256(reinterpret_cast<__m256i*>(sel + cnt), offs);
        cnt += static_cast<std::size_t>(__builtin_popcount(pass));
    }
    for (; i < n; ++i) {
        sel[cnt] = static_cast<uint32_t>(base + i);
        cnt += u[i] - lo <= span;
    }
    return cnt;
}

}  // namespace

std::size_t select_unpacked(const uint64_t* u, std::size_t n, uint64_t lo, uint64_t span,
                            uint32_t base, uint32_t* sel, std::size_t cnt) {
    if (bits::simd()) return select_unpacked_avx2(u, n, lo, span, base, sel, cnt);
    for (std::size_t i = 0; i < n; ++i) {
        sel[cnt] = static_cast<uint32_t>(base + i);
        cnt += u[i] - lo <= span;
    }
    return cnt;
}
