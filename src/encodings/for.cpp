// for (frame of reference): like bitpack, but with a separate frame per block of kBatch
// values: each block stores its own min and bit width, so it adapts to local ranges
// (e.g. sorted data, where each block covers a small slice of the value range).
#include <algorithm>

#include "encodings/bits.h"
#include "encodings/encoded_column.h"

namespace {

struct Block {
    uint64_t min;
    std::size_t offset;  // first word of this block in words_
    unsigned width;
};

class ForCursor final : public ColumnCursor {
public:
    ForCursor(const std::vector<Block>& blocks, const uint64_t* words)
        : blocks_(blocks), words_(words) {}
    int64_t next() override {
        const Block& b = blocks_[i_ / kBatch];
        uint64_t v = bits::extract(words_ + b.offset, b.width, i_ % kBatch) + b.min;
        ++i_;
        return static_cast<int64_t>(v);
    }

private:
    const std::vector<Block>& blocks_;
    const uint64_t* words_;
    std::size_t i_ = 0;
};

class ForColumn final : public EncodedColumn {
public:
    explicit ForColumn(const std::vector<int64_t>& values) : n_(values.size()) {
        std::vector<uint64_t> offsets(kBatch);
        for (std::size_t start = 0; start < n_; start += kBatch) {
            const std::size_t n = std::min(kBatch, n_ - start);
            auto first = values.begin() + start;
            auto [lo, hi] = std::minmax_element(first, first + n);
            Block b{static_cast<uint64_t>(*lo), words_.size(), 0};
            b.width = bits::width_for(static_cast<uint64_t>(*hi) - b.min);
            for (std::size_t i = 0; i < n; ++i)
                offsets[i] = static_cast<uint64_t>(values[start + i]) - b.min;
            bits::pack(offsets.data(), n, b.width, words_);
            blocks_.push_back(b);
        }
    }

    std::size_t size() const override { return n_; }
    std::size_t footprint_bytes() const override {
        // Per block: 8-byte min, 4-byte word offset, 1-byte width.
        return words_.size() * sizeof(uint64_t) + blocks_.size() * (8 + 4 + 1);
    }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        const Block& b = blocks_[start / kBatch];
        uint64_t* u = reinterpret_cast<uint64_t*>(out);
        bits::unpack(words_.data() + b.offset, b.width, bits::groups_for(n), u);
        for (std::size_t i = 0; i < n; ++i) u[i] += b.min;
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<ForCursor>(blocks_, words_.data());
    }

private:
    std::size_t n_;
    std::vector<Block> blocks_;
    std::vector<uint64_t> words_;
};

}  // namespace

std::unique_ptr<EncodedColumn> make_for_column(const std::vector<int64_t>& values) {
    return std::make_unique<ForColumn>(values);
}
