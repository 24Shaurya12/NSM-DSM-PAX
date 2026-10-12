#include "pax.h"

#include <stdexcept>

PaxLayout::PaxLayout(const Table& t, std::size_t page_size)
    : rows_(t.rows),
      cols_(t.cols),
      page_size_(page_size),
      tuples_per_page_(page_size / (t.cols * sizeof(int64_t))) {
    if (page_size_ % kCacheLineSize != 0)
        throw std::invalid_argument("PAX page size must be a multiple of 64 bytes");
    if (tuples_per_page_ == 0)
        throw std::invalid_argument("PAX page size too small to hold a single tuple");

    std::size_t pages = (rows_ + tuples_per_page_ - 1) / tuples_per_page_;
    for (std::size_t p = 0; p < pages; ++p) {
        std::size_t first = p * tuples_per_page_;
        std::size_t remaining = rows_ - first;
        page_tuple_counts_.push_back(remaining < tuples_per_page_ ? remaining : tuples_per_page_);
    }

    buffer_ = AlignedBuffer<int64_t>(pages * page_size_ / sizeof(int64_t));
    for (std::size_t p = 0; p < pages; ++p) {
        std::size_t first = p * tuples_per_page_;
        for (std::size_t c = 0; c < cols_; ++c) {
            int64_t* mp = minipage(p, c);
            for (std::size_t i = 0; i < page_tuple_counts_[p]; ++i) mp[i] = t.at(first + i, c);
        }
    }
}

int64_t PaxLayout::sum_first_k(int k) const {
    int64_t sum = 0;
    for (std::size_t p = 0; p < num_pages(); ++p) {
        std::size_t n = page_tuple_counts_[p];
        for (int c = 0; c < k; ++c) {
            const int64_t* mp = minipage(p, c);
            for (std::size_t i = 0; i < n; ++i) sum += mp[i];
        }
    }
    return sum;
}
