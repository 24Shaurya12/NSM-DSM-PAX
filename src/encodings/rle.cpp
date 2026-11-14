// rle (run-length encoding): one (value, run end) pair per run of equal consecutive
// values. Run ends are cumulative, so the run holding any row can be binary-searched.
// Compresses well only when values repeat in long runs.
#include <algorithm>

#include "encodings/encoded_column.h"

namespace {

class RleCursor final : public ColumnCursor {
public:
    RleCursor(const int64_t* values, const uint64_t* ends)
        : values_(values), ends_(ends), left_(ends[0]) {}
    int64_t next() override {
        if (left_ == 0) {
            ++run_;
            left_ = ends_[run_] - ends_[run_ - 1];
        }
        --left_;
        return values_[run_];
    }

private:
    const int64_t* values_;
    const uint64_t* ends_;
    std::size_t run_ = 0;
    uint64_t left_;  // rows still to return from the current run
};

class RleColumn final : public EncodedColumn {
public:
    explicit RleColumn(const std::vector<int64_t>& values) : n_(values.size()) {
        for (std::size_t i = 0; i < values.size(); ++i) {
            if (i == 0 || values[i] != values[i - 1]) {
                values_.push_back(values[i]);
                ends_.push_back(i + 1);
            } else {
                ends_.back() = i + 1;
            }
        }
    }

    std::size_t size() const override { return n_; }
    std::size_t footprint_bytes() const override {
        return values_.size() * (sizeof(int64_t) + sizeof(uint64_t));
    }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        std::size_t run = std::upper_bound(ends_.begin(), ends_.end(), start) - ends_.begin();
        std::size_t pos = 0;
        while (pos < n) {
            const std::size_t stop = std::min<std::size_t>(ends_[run] - start, n);
            std::fill(out + pos, out + stop, values_[run]);
            pos = stop;
            ++run;
        }
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<RleCursor>(values_.data(), ends_.data());
    }

private:
    std::size_t n_;
    std::vector<int64_t> values_;
    std::vector<uint64_t> ends_;  // ends_[r] = one past the last row of run r
};

}  // namespace

std::unique_ptr<EncodedColumn> make_rle_column(const std::vector<int64_t>& values) {
    return std::make_unique<RleColumn>(values);
}
