#pragma once

#include <cstdint>
#include <vector>

#include "aligned_buffer.h"
#include "layout.h"
#include "table.h"

// Partition Attributes Across: the table is split into fixed-size pages, each holding
// `tuples_per_page` tuples. Inside a page, column c occupies its own contiguous minipage
// starting at byte offset c * tuples_per_page * 8. Pages live back-to-back in one aligned
// buffer at offsets i * page_size, so page boundaries are explicit.
class PaxLayout : public Layout {
public:
    PaxLayout(const Table& t, std::size_t page_size);

    std::string name() const override { return "PAX"; }
    std::size_t rows() const override { return rows_; }
    std::size_t cols() const override { return cols_; }
    std::size_t footprint_bytes() const override { return num_pages() * page_size_; }

    int64_t sum_first_k(int k) const override;

    std::size_t page_size() const { return page_size_; }
    std::size_t tuples_per_page() const { return tuples_per_page_; }
    std::size_t num_pages() const { return page_tuple_counts_.size(); }
    std::size_t page_tuple_count(std::size_t p) const { return page_tuple_counts_[p]; }

    // Start of column c's minipage within page p.
    const int64_t* minipage(std::size_t p, std::size_t c) const {
        const int64_t* page = buffer_.data() + p * (page_size_ / sizeof(int64_t));
        return page + c * tuples_per_page_;
    }

private:
    int64_t* minipage(std::size_t p, std::size_t c) {
        return const_cast<int64_t*>(static_cast<const PaxLayout*>(this)->minipage(p, c));
    }

    std::size_t rows_;
    std::size_t cols_;
    std::size_t page_size_;
    std::size_t tuples_per_page_;
    std::vector<std::size_t> page_tuple_counts_;  // last page may be partial
    AlignedBuffer<int64_t> buffer_;
};
