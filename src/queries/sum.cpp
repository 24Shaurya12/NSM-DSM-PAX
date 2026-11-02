#include "queries/sum.h"

#include "dsm.h"
#include "nsm.h"
#include "pax.h"

void SumQuery::run(const NsmLayout& t) {
    int64_t sum = 0;
    for (std::size_t r = 0; r < t.rows(); ++r) {
        const int64_t* tuple = t.row(r);
        for (int c : columns_) sum += tuple[c];
    }
    sum_ = sum;
}

void SumQuery::run(const DsmLayout& t) {
    int64_t sum = 0;
    for (int c : columns_) {
        const int64_t* col = t.column(c);
        for (std::size_t r = 0; r < t.rows(); ++r) sum += col[r];
    }
    sum_ = sum;
}

void SumQuery::run(const PaxLayout& t) {
    int64_t sum = 0;
    for (std::size_t p = 0; p < t.num_pages(); ++p) {
        std::size_t n = t.page_tuple_count(p);
        for (int c : columns_) {
            const int64_t* mp = t.minipage(p, c);
            for (std::size_t i = 0; i < n; ++i) sum += mp[i];
        }
    }
    sum_ = sum;
}
