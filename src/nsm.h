#pragma once

#include "aligned_buffer.h"
#include "layout.h"
#include "table.h"

// N-ary Storage Model: one contiguous row-major array; (r, c) is at r * cols + c.
class NsmLayout : public Layout {
public:
    explicit NsmLayout(const Table& t);

    std::string name() const override { return "NSM"; }
    std::size_t rows() const override { return rows_; }
    std::size_t cols() const override { return cols_; }
    std::size_t footprint_bytes() const override { return data_.size() * sizeof(int64_t); }

    int64_t sum_first_k(int k) const override;

    const int64_t* row(std::size_t r) const { return data_.data() + r * cols_; }

private:
    std::size_t rows_;
    std::size_t cols_;
    AlignedBuffer<int64_t> data_;
};
