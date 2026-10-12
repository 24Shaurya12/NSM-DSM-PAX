#include "nsm.h"

#include <cstring>

NsmLayout::NsmLayout(const Table& t) : rows_(t.rows), cols_(t.cols), data_(t.rows * t.cols) {
    std::memcpy(data_.data(), t.values.data(), t.values.size() * sizeof(int64_t));
}

int64_t NsmLayout::sum_first_k(int k) const {
    int64_t sum = 0;
    for (std::size_t r = 0; r < rows_; ++r) {
        const int64_t* tuple = row(r);
        for (int c = 0; c < k; ++c) sum += tuple[c];
    }
    return sum;
}
