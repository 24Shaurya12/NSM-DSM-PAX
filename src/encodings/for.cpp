// for (frame of reference): like bitpack, but with a separate frame per block of kBlock
// values: each block stores its own min and bit width, so it adapts to local ranges
// (e.g. sorted data, where each block covers a small slice of the value range).
//
// Each block also keeps its max, so a predicate can be decided for a whole block from its
// [min, max] alone (a zone map): blocks entirely outside the range are skipped, and blocks
// entirely inside it are selected, both without unpacking anything.
#include <algorithm>

#include "encodings/bits.h"
#include "encodings/encoded_column.h"

namespace {

struct Block {
    uint64_t min;
    uint64_t max;
    std::size_t offset;  // first word of this block in words_
    unsigned width;
};

class ForCursor final : public ColumnCursor {
public:
    ForCursor(const std::vector<Block>& blocks, const uint64_t* words)
        : blocks_(blocks), words_(words) {}
    int64_t next() override {
        const Block& b = blocks_[i_ / kBlock];
        uint64_t v = bits::extract(words_ + b.offset, b.width, i_ % kBlock) + b.min;
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
        std::vector<uint64_t> offsets(kBlock);
        for (std::size_t start = 0; start < n_; start += kBlock) {
            const std::size_t n = std::min(kBlock, n_ - start);
            auto first = values.begin() + start;
            auto [lo, hi] = std::minmax_element(first, first + n);
            Block b{static_cast<uint64_t>(*lo), static_cast<uint64_t>(*hi), words_.size(), 0};
            b.width = bits::width_for(b.max - b.min);
            for (std::size_t i = 0; i < n; ++i)
                offsets[i] = static_cast<uint64_t>(values[start + i]) - b.min;
            bits::pack(offsets.data(), n, b.width, words_);
            blocks_.push_back(b);
        }
        bits::pad(words_);
    }

    std::size_t size() const override { return n_; }
    std::size_t footprint_bytes() const override {
        // Per block: 8-byte min, 8-byte max, 4-byte word offset, 1-byte width.
        return words_.size() * sizeof(uint64_t) + blocks_.size() * (8 + 8 + 4 + 1);
    }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        uint64_t* u = reinterpret_cast<uint64_t*>(out);
        const Block& b = blocks_[start / kBlock];
        const std::size_t group = start % kBlock / bits::kGroup;
        bits::unpack(words_.data() + b.offset + group * b.width, b.width, bits::groups_for(n), u,
                     b.min);
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<ForCursor>(blocks_, words_.data());
    }

    std::size_t select(std::size_t start, std::size_t n, const Predicate& p, uint32_t* sel,
                       int64_t* scratch) const override {
        std::size_t cnt = 0;
        for (std::size_t off = 0; off < n;) {
            const std::size_t s = start + off;
            const std::size_t len = std::min(n - off, (s / kBlock + 1) * kBlock - s);
            const Block& b = blocks_[s / kBlock];
            const int64_t bmin = static_cast<int64_t>(b.min), bmax = static_cast<int64_t>(b.max);
            if (bmax < p.lo || bmin > p.hi) {
                // Zone map: no value of this block can pass.
            } else if (bmin >= p.lo && bmax <= p.hi) {
                for (std::size_t i = 0; i < len; ++i) sel[cnt++] = static_cast<uint32_t>(off + i);
            } else {
                // Compare the packed offsets directly: v - lo == offset - (lo - min) mod 2^64.
                uint64_t* u = reinterpret_cast<uint64_t*>(scratch);
                unpack(s, len, u);
                const uint64_t lo = static_cast<uint64_t>(p.lo) - b.min, span = p.span();
                cnt = select_unpacked(u, len, lo, span, static_cast<uint32_t>(off), sel, cnt);
            }
            off += len;
        }
        return cnt;
    }

    std::size_t refine(std::size_t start, uint32_t* sel, std::size_t cnt, const Predicate& p,
                       int64_t*) const override {
        std::size_t kept = 0;
        for (std::size_t t = 0; t < cnt; ++t) {
            const uint32_t pos = sel[t];
            sel[kept] = pos;
            kept += p.test(value(start + pos));
        }
        return kept;
    }

    uint64_t sum(std::size_t start, std::size_t n, int64_t*) const override {
        uint64_t total = 0;
        for (std::size_t off = 0; off < n;) {
            const std::size_t s = start + off;
            const std::size_t len = std::min(n - off, kBlock - s % kBlock);
            const Block& b = blocks_[s / kBlock];
            const std::size_t group = s % kBlock / bits::kGroup;
            total += bits::sum(words_.data() + b.offset + group * b.width, b.width, len) +
                     len * b.min;
            off += len;
        }
        return total;
    }

    void gather(std::size_t start, const uint32_t* sel, std::size_t cnt, int64_t* out,
                std::size_t stride, int64_t*) const override {
        for (std::size_t t = 0; t < cnt; ++t) out[t * stride] = value(start + sel[t]);
    }

private:
    // Random access to one value: its block's frame plus one bit-field extraction.
    int64_t value(std::size_t row) const {
        const Block& b = blocks_[row / kBlock];
        return static_cast<int64_t>(bits::extract(words_.data() + b.offset, b.width, row % kBlock) +
                                    b.min);
    }

    // Unpacks the offsets (value - block min) of [start, start + n) into u; returns the block.
    const Block& unpack(std::size_t start, std::size_t n, uint64_t* u) const {
        const Block& b = blocks_[start / kBlock];
        const std::size_t group = start % kBlock / bits::kGroup;
        bits::unpack(words_.data() + b.offset + group * b.width, b.width, bits::groups_for(n), u);
        return b;
    }

    std::size_t n_;
    std::vector<Block> blocks_;
    std::vector<uint64_t> words_;
};

}  // namespace

std::unique_ptr<EncodedColumn> make_for_column(const std::vector<int64_t>& values) {
    return std::make_unique<ForColumn>(values);
}
