#include "dsm.h"

#include <algorithm>

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

// Column at a time within each batch of rows: evaluate the predicates on their columns to get
// the qualifying positions, then read only those positions of columns [0, k).
QueryResult DsmLayout::filter_sum(const Conjunction& where, int k) const {
    QueryResult res;
    std::vector<uint32_t> sel(kBatchRows);
    for (std::size_t start = 0; start < rows_; start += kBatchRows) {
        std::size_t n = std::min(kBatchRows, rows_ - start);
        auto column_at = [&](int c) { return column(c) + start; };
        std::size_t m = select_positions(where, column_at, n, sel.data());
        res.count += static_cast<int64_t>(m);
        for (int c = 0; c < k; ++c) {
            const int64_t* v = column_at(c);
            for (std::size_t j = 0; j < m; ++j) res.sum += v[sel[j]];
        }
    }
    return res;
}
