#pragma once

#include <vector>

#include "aligned_buffer.h"
#include "layout.h"
#include "table.h"

// Decomposition Storage Model: one contiguous array of `rows` values per column.
class DsmLayout : public Layout {
public:
    explicit DsmLayout(const Table& t);

    std::string name() const override { return "DSM"; }
    std::size_t rows() const override { return rows_; }
    std::size_t cols() const override { return columns_.size(); }
    std::size_t footprint_bytes() const override {
        return columns_.size() * rows_ * sizeof(int64_t);
    }

    int64_t sum_first_k(int k) const override;

    const int64_t* column(std::size_t c) const { return columns_[c].data(); }

private:
    std::size_t rows_;
    std::vector<AlignedBuffer<int64_t>> columns_;
};
