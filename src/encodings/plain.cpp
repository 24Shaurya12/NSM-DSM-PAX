// plain: the values as an int64 array, exactly like DSM. Measures the cost of the
// cursor / batch machinery itself, with no decoding work.
#include <cstring>

#include "aligned_buffer.h"
#include "encodings/encoded_column.h"

namespace {

class PlainCursor final : public ColumnCursor {
public:
    explicit PlainCursor(const int64_t* p) : p_(p) {}
    int64_t next() override { return *p_++; }

private:
    const int64_t* p_;
};

class PlainColumn final : public EncodedColumn {
public:
    explicit PlainColumn(const std::vector<int64_t>& values) : data_(values.size()) {
        std::memcpy(data_.data(), values.data(), values.size() * sizeof(int64_t));
    }

    std::size_t size() const override { return data_.size(); }
    std::size_t footprint_bytes() const override { return data_.size() * sizeof(int64_t); }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        std::memcpy(out, data_.data() + start, n * sizeof(int64_t));
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<PlainCursor>(data_.data());
    }

    // Plain columns need no decoding, so these work on the array itself (no copy).
    std::size_t select(std::size_t start, std::size_t n, const Predicate& p, uint32_t* sel,
                       int64_t*) const override {
        const int64_t* v = data_.data() + start;
        const uint64_t lo = static_cast<uint64_t>(p.lo), span = p.span();
        std::size_t cnt = 0;
        for (std::size_t i = 0; i < n; ++i) {
            sel[cnt] = static_cast<uint32_t>(i);
            cnt += static_cast<uint64_t>(v[i]) - lo <= span;
        }
        return cnt;
    }

    std::size_t refine(std::size_t start, uint32_t* sel, std::size_t cnt, const Predicate& p,
                       int64_t*) const override {
        const int64_t* v = data_.data() + start;
        std::size_t kept = 0;
        for (std::size_t t = 0; t < cnt; ++t) {
            const uint32_t pos = sel[t];
            sel[kept] = pos;
            kept += p.test(v[pos]);
        }
        return kept;
    }

    void gather(std::size_t start, const uint32_t* sel, std::size_t cnt, int64_t* out,
                std::size_t stride, int64_t*) const override {
        const int64_t* v = data_.data() + start;
        for (std::size_t t = 0; t < cnt; ++t) out[t * stride] = v[sel[t]];
    }

private:
    AlignedBuffer<int64_t> data_;
};

}  // namespace

std::unique_ptr<EncodedColumn> make_plain_column(const std::vector<int64_t>& values) {
    return std::make_unique<PlainColumn>(values);
}
