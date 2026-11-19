#include "pax.h"

#include <algorithm>
#include <numeric>
#include <stdexcept>

#include "query.h"

namespace {

// Tuples per page: as many as fit (after `pad` bytes per minipage).
std::size_t tuples_for(std::size_t page_size, std::size_t cols, std::size_t pad) {
    return page_size > cols * pad ? (page_size - cols * pad) / (cols * sizeof(int64_t)) : 0;
}

// How many of the `cols` minipages, `stride` bytes apart, have their value i in the same L1
// set: the starts m * stride (m < cols) fall on sets / gcd(stride lines, sets) distinct sets.
std::size_t minipages_per_set(std::size_t stride, std::size_t cols, std::size_t set_stride) {
    const std::size_t sets = std::max<std::size_t>(1, set_stride / kCacheLineSize);
    const std::size_t lines = stride / kCacheLineSize % sets;
    const std::size_t distinct = std::min(cols, sets / std::gcd(lines, sets));
    return (cols + distinct - 1) / distinct;
}

// With odd_stride: if plain packing puts more minipages in one L1 set than the set has ways
// (a stride that is a multiple of 4 KB on the test machine, i.e. pages of 64 KB or more with
// 16 columns), round the stride down to an odd number of cache lines, so the minipages land in
// distinct sets. Otherwise keep plain packing: rounding down short minipages would waste up to
// half of each page (2 lines -> 1) for no gain.
std::size_t tuples_for_odd(std::size_t page_size, std::size_t cols, std::size_t set_stride,
                           std::size_t ways) {
    const std::size_t plain = tuples_for(page_size, cols, 0);
    if (minipages_per_set(plain * sizeof(int64_t), cols, set_stride) <= ways) return plain;
    std::size_t lines = page_size / (cols * kCacheLineSize);
    if (lines > 1 && lines % 2 == 0) --lines;
    const std::size_t odd = lines * kCacheLineSize / sizeof(int64_t);
    return odd ? odd : plain;
}

}  // namespace

PaxLayout::PaxLayout(const Table& t, std::size_t page_size, std::size_t minipage_pad,
                     std::size_t l1_set_stride, std::size_t l1_ways)
    : rows_(t.rows),
      cols_(t.cols),
      page_size_(page_size),
      tuples_per_page_(minipage_pad == kOddStride
                           ? tuples_for_odd(page_size, t.cols, l1_set_stride, l1_ways)
                           : tuples_for(page_size, t.cols, minipage_pad)),
      pad_values_(minipage_pad == kOddStride ? 0 : minipage_pad / sizeof(int64_t)),
      odd_stride_(minipage_pad == kOddStride &&
                  tuples_per_page_ != tuples_for(page_size, t.cols, 0)) {
    if (page_size_ % kCacheLineSize != 0)
        throw std::invalid_argument("PAX page size must be a multiple of 64 bytes");
    if (minipage_pad != kOddStride && minipage_pad % sizeof(int64_t) != 0)
        throw std::invalid_argument("PAX minipage padding must be a multiple of 8 bytes");
    if (tuples_per_page_ == 0)
        throw std::invalid_argument("PAX page size too small to hold a single tuple");

    std::size_t pages = (rows_ + tuples_per_page_ - 1) / tuples_per_page_;
    for (std::size_t p = 0; p < pages; ++p) {
        std::size_t first = page_first_row(p);
        std::size_t remaining = rows_ - first;
        page_tuple_counts_.push_back(remaining < tuples_per_page_ ? remaining : tuples_per_page_);
    }

    buffer_ = AlignedBuffer<int64_t>(pages * page_size_ / sizeof(int64_t));
    for (std::size_t p = 0; p < pages; ++p) {
        std::size_t first = page_first_row(p);
        for (std::size_t c = 0; c < cols_; ++c) {
            int64_t* mp = minipage(p, c);
            for (std::size_t i = 0; i < page_tuple_counts_[p]; ++i) mp[i] = t.at(first + i, c);
        }
    }
}

void PaxLayout::run(Query& q) const { q.run(*this); }
