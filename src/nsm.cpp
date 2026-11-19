#include "nsm.h"

#include <cstring>

#include "query.h"

NsmLayout::NsmLayout(const Table& t) : rows_(t.rows), cols_(t.cols), data_(t.rows * t.cols) {
    std::memcpy(data_.data(), t.values.data(), t.values.size() * sizeof(int64_t));
}

void NsmLayout::run(Query& q) const { q.run(*this); }
