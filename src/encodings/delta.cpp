// delta: per block of kBatch values, the block's first value plus the difference of each
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
        const Block& b = blocks_[i_ / kBatch];
        const std::size_t j = i_ % kBatch;
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
        std::vector<uint64_t> deltas(kBatch);
        for (std::size_t start = 0; start < n_; start += kBatch) {
            const std::size_t n = std::min(kBatch, n_ - start);
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
    }

    std::size_t size() const override { return n_; }
    std::size_t footprint_bytes() const override {
        // Per block: 8-byte first value, 4-byte word offset, 1-byte width.
        return words_.size() * sizeof(uint64_t) + blocks_.size() * (8 + 4 + 1);
    }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        const Block& b = blocks_[start / kBatch];
        uint64_t* u = reinterpret_cast<uint64_t*>(out);
        bits::unpack(words_.data() + b.offset, b.width, bits::groups_for(n), u);
        uint64_t acc = b.first;
        for (std::size_t i = 0; i < n; ++i) {
            acc += bits::unzigzag(u[i]);
            u[i] = acc;
        }
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<DeltaCursor>(blocks_, words_.data());
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
