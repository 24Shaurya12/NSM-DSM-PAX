#include "dsm.h"

#include "query.h"

DsmLayout::DsmLayout(const Table& t, std::size_t offset_step, std::size_t offset_wrap)
    : rows_(t.rows) {
    columns_.reserve(t.cols);
    for (std::size_t c = 0; c < t.cols; ++c) {
        const std::size_t offset = offset_wrap ? c * offset_step % offset_wrap : 0;
        AlignedBuffer<int64_t> col(t.rows, offset / kCacheLineSize * kCacheLineSize);
        for (std::size_t r = 0; r < t.rows; ++r) col[r] = t.at(r, c);
        columns_.push_back(std::move(col));
    }
}

void DsmLayout::run(Query& q) const { q.run(*this); }
