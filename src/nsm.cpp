#include "nsm.h"

#include <cstring>

#include "tuple_sink.h"

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

// Tuple at a time: test the predicates on the tuple's own fields, stopping at the first one that
// fails, and add the first k fields of every qualifying tuple.
QueryResult NsmLayout::filter_sum(const Conjunction& where, int k) const {
    QueryResult res;
    for (std::size_t r = 0; r < rows_; ++r) {
        const int64_t* tuple = row(r);
        bool qualifies = true;
        for (const Predicate& p : where) {
            if (!p.matches(tuple[p.col])) {
                qualifies = false;
                break;
            }
        }
        if (!qualifies) continue;
        ++res.count;
        for (int c = 0; c < k; ++c) res.sum += tuple[c];
    }
    return res;
}

// Tuple at a time: the first k attributes of a qualifying tuple are already contiguous, so the
// output tuple is a single copy.
QueryResult NsmLayout::filter_materialize(const Conjunction& where, int k) const {
    TupleSink sink(k);
    for (std::size_t r = 0; r < rows_; ++r) {
        const int64_t* tuple = row(r);
        bool qualifies = true;
        for (const Predicate& p : where) {
            if (!p.matches(tuple[p.col])) {
                qualifies = false;
                break;
            }
        }
        if (!qualifies) continue;
        std::memcpy(sink.next(), tuple, k * sizeof(int64_t));
    }
    return sink.finish();
}
