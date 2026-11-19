// delta: per block of kBlock values, the block's first value plus the difference of each
// value from the previous one, zigzag-encoded and bit-packed at the block's width.
// Compresses well when neighbouring values are close (e.g. sorted data). Decoding is a
// running sum, so each value depends on the one before it.
#include <algorithm>

#include "encodings/bits.h"
#include "encodings/encoded_column.h"

namespace {

struct Block {
    uint64_t first;
    std::size_t offset;  // first word of this block in words_
    unsigned width;
};

class DeltaCursor final : public ColumnCursor {
public:
    DeltaCursor(const std::vector<Block>& blocks, const uint64_t* words)
        : blocks_(blocks), words_(words) {}
    int64_t next() override {
        const Block& b = blocks_[i_ / kBlock];
        const std::size_t j = i_ % kBlock;
        if (j == 0) acc_ = b.first;  // the first delta of a block is always 0
        acc_ += bits::unzigzag(bits::extract(words_ + b.offset, b.width, j));
        ++i_;
        return static_cast<int64_t>(acc_);
    }

private:
    // i_ and acc_ are kept apart on purpose: when adjacent, GCC merges their stores into
    // one 16-byte store, and the next call's 8-byte loads of them cannot be
    // store-forwarded from it (Zen 3), costing ~25 cycles per value.
    std::size_t i_ = 0;
    const std::vector<Block>& blocks_;
    const uint64_t* words_;
    uint64_t acc_ = 0;
};

class DeltaColumn final : public EncodedColumn {
public:
    explicit DeltaColumn(const std::vector<int64_t>& values) : n_(values.size()) {
        std::vector<uint64_t> deltas(kBlock);
        for (std::size_t start = 0; start < n_; start += kBlock) {
            const std::size_t n = std::min(kBlock, n_ - start);
            deltas[0] = 0;
            uint64_t widest = 0;
            for (std::size_t i = 1; i < n; ++i) {
                uint64_t d = static_cast<uint64_t>(values[start + i]) -
                             static_cast<uint64_t>(values[start + i - 1]);
                deltas[i] = bits::zigzag(d);
                widest = std::max(widest, deltas[i]);
            }
            Block b{static_cast<uint64_t>(values[start]), words_.size(),
                    bits::width_for(widest)};
            bits::pack(deltas.data(), n, b.width, words_);
            blocks_.push_back(b);
        }
        bits::pad(words_);
    }

    std::size_t size() const override { return n_; }
    std::size_t footprint_bytes() const override {
        // Per block: 8-byte first value, 4-byte word offset, 1-byte width.
        return words_.size() * sizeof(uint64_t) + blocks_.size() * (8 + 4 + 1);
    }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        const Block& b = blocks_[start / kBlock];
        uint64_t* u = reinterpret_cast<uint64_t*>(out);
        uint64_t acc = b.first;
        // Starting inside a block: a value is the block's first value plus every delta before
        // it, so first sum up the deltas of the block's earlier groups (unpacked one group at
        // a time into out, which always has room for at least one group).
        const std::size_t group = start % kBlock / bits::kGroup;
        for (std::size_t g = 0; g < group; ++g) {
            bits::unpack(words_.data() + b.offset + g * b.width, b.width, 1, u);
            for (std::size_t i = 0; i < bits::kGroup; ++i) acc += bits::unzigzag(u[i]);
        }
        bits::unpack(words_.data() + b.offset + group * b.width, b.width, bits::groups_for(n), u);
        for (std::size_t i = 0; i < n; ++i) {
            acc += bits::unzigzag(u[i]);
            u[i] = acc;
        }
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<DeltaCursor>(blocks_, words_.data());
    }

    // SUM straight from the deltas. In a block of m values with first value f and deltas
    // d_1..d_{m-1}, value i is f + d_1 + ... + d_i, so the block sums to
    //     m*f + sum_k d_k * (m - k):
    // each delta is counted once for every value after it. That is an independent multiply-add
    // per delta (bits::weighted_zigzag_sum; with --simd fused into the AVX2 unpacking),
    // instead of decoding's running sum, where each value waits for the one before. Blocks
    // only partly inside [start, start + n) are decoded.
    uint64_t sum(std::size_t start, std::size_t n, int64_t* scratch) const override {
        uint64_t total = 0;
        for (std::size_t off = 0; off < n;) {
            const std::size_t s = start + off, bstart = s / kBlock * kBlock;
            const std::size_t m = std::min(kBlock, n_ - bstart);
            const std::size_t len = std::min(n - off, bstart + m - s);
            if (s == bstart && len == m) {
                const Block& b = blocks_[s / kBlock];
                total += m * b.first + bits::weighted_zigzag_sum(words_.data() + b.offset,
                                                                 b.width, m);
            } else {
                decode(s, len, scratch);
                for (std::size_t i = 0; i < len; ++i) total += static_cast<uint64_t>(scratch[i]);
            }
            off += len;
        }
        return total;
    }

private:
    std::size_t n_;
    std::vector<Block> blocks_;
    std::vector<uint64_t> words_;
};

}  // namespace

std::unique_ptr<EncodedColumn> make_delta_column(const std::vector<int64_t>& values) {
    return std::make_unique<DeltaColumn>(values);
}
