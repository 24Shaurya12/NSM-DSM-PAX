// dict: a sorted array of the column's distinct values, plus one bit-packed code per row
// (the value's index in that array). Compresses well when there are few distinct
// values, regardless of how large or spread out the values themselves are.
#include <algorithm>

#include "encodings/bits.h"
#include "encodings/encoded_column.h"

namespace {

class DictCursor final : public ColumnCursor {
public:
    DictCursor(const int64_t* dict, const uint64_t* words, unsigned w)
        : dict_(dict), words_(words), w_(w) {}
    int64_t next() override { return dict_[bits::extract(words_, w_, i_++)]; }

private:
    const int64_t* dict_;
    const uint64_t* words_;
    unsigned w_;
    std::size_t i_ = 0;
};

class DictColumn final : public EncodedColumn {
public:
    explicit DictColumn(const std::vector<int64_t>& values) : n_(values.size()), dict_(values) {
        std::sort(dict_.begin(), dict_.end());
        dict_.erase(std::unique(dict_.begin(), dict_.end()), dict_.end());
        w_ = dict_.empty() ? 0 : bits::width_for(dict_.size() - 1);
        std::vector<uint64_t> codes(values.size());
        for (std::size_t i = 0; i < values.size(); ++i)
            codes[i] = std::lower_bound(dict_.begin(), dict_.end(), values[i]) - dict_.begin();
        bits::pack(codes.data(), codes.size(), w_, words_);
        bits::pad(words_);
    }

    std::size_t size() const override { return n_; }
    std::size_t footprint_bytes() const override {
        return dict_.size() * sizeof(int64_t) + words_.size() * sizeof(uint64_t) + sizeof(w_);
    }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        // Unpack the codes into out, then replace each code with its value in place.
        uint64_t* u = reinterpret_cast<uint64_t*>(out);
        bits::unpack(words_.data() + start / bits::kGroup * w_, w_, bits::groups_for(n), u);
        for (std::size_t i = 0; i < n; ++i) out[i] = dict_[u[i]];
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<DictCursor>(dict_.data(), words_.data(), w_);
    }

    // The dictionary is sorted, so codes preserve order: [lo, hi] on values becomes a range
    // of codes, found with two binary searches. The scan then compares codes and never
    // touches the dictionary.
    std::size_t select(std::size_t start, std::size_t n, const Predicate& p, uint32_t* sel,
                       int64_t* scratch) const override {
        uint64_t clo, cspan;
        if (!code_range(p, clo, cspan)) return 0;
        uint64_t* u = reinterpret_cast<uint64_t*>(scratch);
        std::size_t cnt = 0;
        for (std::size_t off = 0; off < n; off += kBlock) {
            const std::size_t len = std::min(kBlock, n - off);
            bits::unpack(words_.data() + (start + off) / bits::kGroup * w_, w_,
                         bits::groups_for(len), u);
            cnt = select_unpacked(u, len, clo, cspan, static_cast<uint32_t>(off), sel, cnt);
        }
        return cnt;
    }

    std::size_t refine(std::size_t start, uint32_t* sel, std::size_t cnt, const Predicate& p,
                       int64_t*) const override {
        uint64_t clo, cspan;
        if (!code_range(p, clo, cspan)) return 0;
        std::size_t kept = 0;
        for (std::size_t t = 0; t < cnt; ++t) {
            const uint32_t pos = sel[t];
            sel[kept] = pos;
            kept += bits::extract(words_.data(), w_, start + pos) - clo <= cspan;
        }
        return kept;
    }

    void gather(std::size_t start, const uint32_t* sel, std::size_t cnt, int64_t* out,
                std::size_t stride, int64_t*) const override {
        for (std::size_t t = 0; t < cnt; ++t)
            out[t * stride] = dict_[bits::extract(words_.data(), w_, start + sel[t])];
    }

private:
    // Codes whose values lie in [p.lo, p.hi], as [clo, clo + cspan]; false if none.
    bool code_range(const Predicate& p, uint64_t& clo, uint64_t& cspan) const {
        const auto first = std::lower_bound(dict_.begin(), dict_.end(), p.lo);
        const auto last = std::upper_bound(first, dict_.end(), p.hi);
        if (first == last) return false;
        clo = static_cast<uint64_t>(first - dict_.begin());
        cspan = static_cast<uint64_t>(last - first) - 1;
        return true;
    }

    std::size_t n_;
    std::vector<int64_t> dict_;
    unsigned w_ = 0;
    std::vector<uint64_t> words_;
};

}  // namespace

std::unique_ptr<EncodedColumn> make_dict_column(const std::vector<int64_t>& values) {
    return std::make_unique<DictColumn>(values);
}
