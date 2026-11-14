// bitpack: one frame for the whole column. Stores v - min(column) in w bits, where w is
// the width of max - min. Compresses well only if the whole column has a narrow range.
#include <algorithm>

#include "encodings/bits.h"
#include "encodings/encoded_column.h"

namespace {

class BitpackCursor final : public ColumnCursor {
public:
    BitpackCursor(const uint64_t* words, unsigned w, uint64_t min) : words_(words), w_(w), min_(min) {}
    int64_t next() override {
        return static_cast<int64_t>(bits::extract(words_, w_, i_++) + min_);
    }

private:
    const uint64_t* words_;
    unsigned w_;
    uint64_t min_;
    std::size_t i_ = 0;
};

class BitpackColumn final : public EncodedColumn {
public:
    explicit BitpackColumn(const std::vector<int64_t>& values) : n_(values.size()) {
        if (values.empty()) return;
        auto [lo, hi] = std::minmax_element(values.begin(), values.end());
        min_ = static_cast<uint64_t>(*lo);
        w_ = bits::width_for(static_cast<uint64_t>(*hi) - min_);
        std::vector<uint64_t> offsets(values.size());
        for (std::size_t i = 0; i < values.size(); ++i)
            offsets[i] = static_cast<uint64_t>(values[i]) - min_;
        bits::pack(offsets.data(), offsets.size(), w_, words_);
        bits::pad(words_);
    }

    std::size_t size() const override { return n_; }
    std::size_t footprint_bytes() const override {
        return words_.size() * sizeof(uint64_t) + sizeof(min_) + sizeof(w_);
    }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        uint64_t* u = reinterpret_cast<uint64_t*>(out);
        bits::unpack(words_.data() + start / bits::kGroup * w_, w_, bits::groups_for(n), u, min_);
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<BitpackCursor>(words_.data(), w_, min_);
    }

    // Compares the packed offsets without adding min back: v - lo == offset - (lo - min).
    std::size_t select(std::size_t start, std::size_t n, const Predicate& p, uint32_t* sel,
                       int64_t* scratch) const override {
        const uint64_t lo = static_cast<uint64_t>(p.lo) - min_, span = p.span();
        uint64_t* u = reinterpret_cast<uint64_t*>(scratch);
        std::size_t cnt = 0;
        for (std::size_t off = 0; off < n; off += kBlock) {
            const std::size_t len = std::min(kBlock, n - off);
            bits::unpack(words_.data() + (start + off) / bits::kGroup * w_, w_,
                         bits::groups_for(len), u);
            cnt = select_unpacked(u, len, lo, span, static_cast<uint32_t>(off), sel, cnt);
        }
        return cnt;
    }

    std::size_t refine(std::size_t start, uint32_t* sel, std::size_t cnt, const Predicate& p,
                       int64_t*) const override {
        const uint64_t lo = static_cast<uint64_t>(p.lo) - min_, span = p.span();
        std::size_t kept = 0;
        for (std::size_t t = 0; t < cnt; ++t) {
            const uint32_t pos = sel[t];
            sel[kept] = pos;
            kept += bits::extract(words_.data(), w_, start + pos) - lo <= span;
        }
        return kept;
    }

    uint64_t sum(std::size_t start, std::size_t n, int64_t*) const override {
        return bits::sum(words_.data() + start / bits::kGroup * w_, w_, n) + n * min_;
    }

    void gather(std::size_t start, const uint32_t* sel, std::size_t cnt, int64_t* out,
                std::size_t stride, int64_t*) const override {
        for (std::size_t t = 0; t < cnt; ++t)
            out[t * stride] =
                static_cast<int64_t>(bits::extract(words_.data(), w_, start + sel[t]) + min_);
    }

private:
    std::size_t n_;
    uint64_t min_ = 0;
    unsigned w_ = 0;
    std::vector<uint64_t> words_;
};

}  // namespace

std::unique_ptr<EncodedColumn> make_bitpack_column(const std::vector<int64_t>& values) {
    return std::make_unique<BitpackColumn>(values);
}
