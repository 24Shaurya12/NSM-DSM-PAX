#include "dsm.h"

DsmLayout::DsmLayout(const Table& t) : rows_(t.rows) {
    columns_.reserve(t.cols);
    for (std::size_t c = 0; c < t.cols; ++c) {
        AlignedBuffer<int64_t> col(t.rows);
        for (std::size_t r = 0; r < t.rows; ++r) col[r] = t.at(r, c);
        columns_.push_back(std::move(col));
    }
}

int64_t DsmLayout::sum_first_k(int k) const {
    int64_t sum = 0;
    for (int c = 0; c < k; ++c) {
        const int64_t* col = column(c);
        for (std::size_t r = 0; r < rows_; ++r) sum += col[r];
    }
    return sum;
}
