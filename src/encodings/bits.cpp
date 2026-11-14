#include "encodings/bits.h"

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
void unpack_groups(const uint64_t* in, std::size_t groups, uint64_t* out) {
    for (std::size_t g = 0; g < groups; ++g) unpack_group<W>(in + g * W, out + g * kGroup);
}

using UnpackFn = void (*)(const uint64_t*, std::size_t, uint64_t*);

template <std::size_t... W>
constexpr std::array<UnpackFn, sizeof...(W)> make_table(std::index_sequence<W...>) {
    return {&unpack_groups<static_cast<unsigned>(W)>...};
}

constexpr auto kUnpack = make_table(std::make_index_sequence<65>{});

}  // namespace

void unpack(const uint64_t* in, unsigned w, std::size_t groups, uint64_t* out) {
    kUnpack[w](in, groups, out);
}

}  // namespace bits
