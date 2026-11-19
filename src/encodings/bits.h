#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

// Bit-packing of unsigned integers at a fixed width w (0..64 bits per value), LSB first.
//
// Values are packed in groups of 64: a group of width w is exactly w 64-bit words, so
// every group starts on a word boundary and can be unpacked by a kernel specialised for
// w (see unpack()). A packed run of n values is padded with zeros to a whole number of
// groups.
namespace bits {

constexpr std::size_t kGroup = 64;

// Bits needed to store every value in [0, max_value]; 0 when max_value == 0.
inline unsigned width_for(uint64_t max_value) {
    return max_value == 0 ? 0 : 64 - static_cast<unsigned>(__builtin_clzll(max_value));
}

inline std::size_t groups_for(std::size_t n) { return (n + kGroup - 1) / kGroup; }

inline std::size_t words_for(std::size_t n, unsigned w) { return groups_for(n) * w; }

// Appends values[0, n), each < 2^w, to out at width w.
void pack(const uint64_t* values, std::size_t n, unsigned w, std::vector<uint64_t>& out);

// Value i of a packed run of width w starting at `in` (value-at-a-time access).
inline uint64_t extract(const uint64_t* in, unsigned w, std::size_t i) {
    if (w == 0) return 0;
    const std::size_t bit = i * w;
    const std::size_t word = bit / 64;
    const unsigned shift = bit % 64;
    uint64_t v = in[word] >> shift;
    // A value never crosses a group's last word, so in[word + 1] is in bounds here.
    if (shift + w > 64) v |= in[word + 1] << (64 - shift);
    return w == 64 ? v : v & ((uint64_t{1} << w) - 1);
}

// Unpacks `groups` whole groups (64 * groups values) of width w from `in` into out, adding
// `base` to every value. Dispatches once on w to a fully unrolled kernel with constant
// shifts and masks; with set_simd(true), widths 1..25 use an AVX2 kernel instead (8 values
// per step), which reads up to kPadWords words past the end of the last group, so every
// packed buffer must end with that much padding (see pad()).
void unpack(const uint64_t* in, unsigned w, std::size_t groups, uint64_t* out, uint64_t base = 0);

// Sum of the first n values of a packed run of width w starting at `in` (a group
// boundary), without storing them: fused AVX2 unpack-and-add with set_simd(true) and
// w <= 25, otherwise unpack one group at a time onto the stack.
uint64_t sum(const uint64_t* in, unsigned w, std::size_t n);

// Sum over k < m of unzigzag(x_k) * (m - k), for the m zigzag-encoded values of a packed run
// of width w starting at `in` (a group boundary, padded with zeros to a whole group): the
// sum of a delta block's values minus m times its first value (see delta.cpp). Fused AVX2
// unpack-and-multiply-add with set_simd(true) and w <= 25.
uint64_t weighted_zigzag_sum(const uint64_t* in, unsigned w, std::size_t m);

// AVX2 unpacking on or off (process-wide; scan_bench --simd). Off by default.
void set_simd(bool on);
bool simd();

// Zero words to append after the last packed group, for the AVX2 kernel's over-read.
constexpr std::size_t kPadWords = 4;
inline void pad(std::vector<uint64_t>& words) { words.resize(words.size() + kPadWords, 0); }

// Zigzag maps signed deltas to unsigned so small negative values stay small.
inline uint64_t zigzag(uint64_t d) { return (d << 1) ^ (0 - (d >> 63)); }
inline uint64_t unzigzag(uint64_t z) { return (z >> 1) ^ (0 - (z & 1)); }

}  // namespace bits
