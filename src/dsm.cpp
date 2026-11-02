#include "dsm.h"

#include "query.h"

DsmLayout::DsmLayout(const Table& t) : rows_(t.rows) {
    columns_.reserve(t.cols);
    for (std::size_t c = 0; c < t.cols; ++c) {
        AlignedBuffer<int64_t> col(t.rows);
        for (std::size_t r = 0; r < t.rows; ++r) col[r] = t.at(r, c);
        columns_.push_back(std::move(col));
    }
}

void DsmLayout::run(Query& q) const { q.run(*this); }
