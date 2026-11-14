#pragma once

#include <cstdint>
#include <vector>

#include "aligned_buffer.h"
#include "layout.h"
#include "table.h"

// Partition Attributes Across: the table is split into fixed-size pages, each holding
// `tuples_per_page` tuples. Inside a page, column c occupies its own contiguous minipage
// starting at byte offset c * minipage_stride, where minipage_stride is the minipage size
// (tuples_per_page * 8 bytes) plus `minipage_pad` bytes of padding. Pages live
// back-to-back in one aligned buffer at offsets i * page_size, so page boundaries are
// explicit.
//
// The minipage stride matters for the cache: when it is a multiple of the L1 set stride
// (4 KB on the test machine; any page of 64 KB or more with 16 columns), value i of every
// minipage maps to the same L1 set, and reconstructing tuples row by row from those
// minipages runs 2-3x slower (see the report). Explicit padding does not help with a fixed
// page size: it only lowers tuples_per_page, and the stride stays page_size / N.
// kOddStride instead picks tuples_per_page so that the stride is an odd number of cache
// lines (so up to 64 minipages land in distinct L1 sets), leaving at most one line per
// minipage unused at the end of the page. It does so only where plain packing would put more
// minipages in one L1 set than it has ways; elsewhere it keeps plain packing (odd_stride()
// then reports false).
class PaxLayout : public Layout {
public:
    static constexpr std::size_t kOddStride = ~std::size_t{0};  // minipage_pad: "auto"

    // l1_set_stride / l1_ways: the L1D geometry kOddStride checks the stride against.
    PaxLayout(const Table& t, std::size_t page_size, std::size_t minipage_pad = 0,
              std::size_t l1_set_stride = 4096, std::size_t l1_ways = 8);

    std::string name() const override { return "PAX"; }
    std::size_t rows() const override { return rows_; }
    std::size_t cols() const override { return cols_; }
    std::size_t footprint_bytes() const override { return num_pages() * page_size_; }

    void run(Query& q) const override;

    std::size_t page_size() const { return page_size_; }
    std::size_t minipage_pad() const { return pad_values_ * sizeof(int64_t); }
    bool odd_stride() const { return odd_stride_; }
    std::size_t minipage_stride_bytes() const { return (tuples_per_page_ + pad_values_) * 8; }
    std::size_t tuples_per_page() const { return tuples_per_page_; }
    std::size_t num_pages() const { return page_tuple_counts_.size(); }
    std::size_t page_tuple_count(std::size_t p) const { return page_tuple_counts_[p]; }
    std::size_t page_first_row(std::size_t p) const { return p * tuples_per_page_; }

    // Start of column c's minipage within page p.
    const int64_t* minipage(std::size_t p, std::size_t c) const {
        const int64_t* page = buffer_.data() + p * (page_size_ / sizeof(int64_t));
        return page + c * (tuples_per_page_ + pad_values_);
    }

private:
    int64_t* minipage(std::size_t p, std::size_t c) {
        return const_cast<int64_t*>(static_cast<const PaxLayout*>(this)->minipage(p, c));
    }

    std::size_t rows_;
    std::size_t cols_;
    std::size_t page_size_;
    std::size_t tuples_per_page_;
    std::size_t pad_values_;  // padding after each minipage, in int64 values
    bool odd_stride_;
    std::vector<std::size_t> page_tuple_counts_;  // last page may be partial
    AlignedBuffer<int64_t> buffer_;
};
