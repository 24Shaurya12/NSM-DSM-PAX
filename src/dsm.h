#pragma once

#include <vector>

#include "aligned_buffer.h"
#include "layout.h"
#include "table.h"

// Decomposition Storage Model: one contiguous array of `rows` values per column.
//
// Column c's array starts (c * offset_step) mod offset_wrap bytes past its aligned
// allocation (both multiples of 64; offset_step = 0 puts every column at the same offset).
// See auto_dsm_offset() in machine.h for why that matters.
class DsmLayout : public Layout {
public:
    explicit DsmLayout(const Table& t, std::size_t offset_step = 0, std::size_t offset_wrap = 65536);

    std::string name() const override { return "DSM"; }
    std::size_t rows() const override { return rows_; }
    std::size_t cols() const override { return columns_.size(); }
    std::size_t footprint_bytes() const override {
        return columns_.size() * rows_ * sizeof(int64_t);
    }

    void run(Query& q) const override;

    const int64_t* column(std::size_t c) const { return columns_[c].data(); }

private:
    std::size_t rows_;
    std::vector<AlignedBuffer<int64_t>> columns_;
};
