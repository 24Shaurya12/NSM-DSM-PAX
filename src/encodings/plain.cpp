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

private:
    AlignedBuffer<int64_t> data_;
};

}  // namespace

std::unique_ptr<EncodedColumn> make_plain_column(const std::vector<int64_t>& values) {
    return std::make_unique<PlainColumn>(values);
}
