#include "queries/sum.h"

#include <algorithm>

#include "dsm.h"
#include "encoded_dsm.h"
#include "nsm.h"
#include "pax.h"

void SumQuery::run(const NsmLayout& t) {
    // Read once: rows() is virtual, so `r < t.rows()` would re-check the vtable on every
    // iteration and stop the compiler from vectorizing the loop.
    const std::size_t rows = t.rows();
    uint64_t sum = 0;
    for (std::size_t r = 0; r < rows; ++r) {
        const int64_t* tuple = t.row(r);
        for (int c : columns_) sum += static_cast<uint64_t>(tuple[c]);
    }
    sum_ = static_cast<int64_t>(sum);
}

void SumQuery::run(const DsmLayout& t) {
    // Read once: rows() is virtual, so `r < t.rows()` would re-check the vtable on every
    // iteration and stop the compiler from vectorizing the loop.
    const std::size_t rows = t.rows();
    uint64_t sum = 0;
    for (int c : columns_) {
        const int64_t* col = t.column(c);
        for (std::size_t r = 0; r < rows; ++r) sum += static_cast<uint64_t>(col[r]);
    }
    sum_ = static_cast<int64_t>(sum);
}

void SumQuery::run(const PaxLayout& t) {
    uint64_t sum = 0;
    for (std::size_t p = 0; p < t.num_pages(); ++p) {
        std::size_t n = t.page_tuple_count(p);
        for (int c : columns_) {
            const int64_t* mp = t.minipage(p, c);
            for (std::size_t i = 0; i < n; ++i) sum += static_cast<uint64_t>(mp[i]);
        }
    }
    sum_ = static_cast<int64_t>(sum);
}

void SumQuery::run(const EncodedDsmLayout& t) {
    // Read once: rows() is virtual, so `r < t.rows()` would re-check the vtable on every
    // iteration and stop the compiler from vectorizing the loop.
    const std::size_t rows = t.rows();
    uint64_t sum = 0;
    if (t.mode() == ScanMode::Value) {
        for (int c : columns_) {
            auto cur = t.column(c).cursor();
            for (std::size_t r = 0; r < rows; ++r) sum += static_cast<uint64_t>(cur->next());
        }
    } else {
        int64_t* buf = batch_.data();
        for (int c : columns_) {
            const EncodedColumn& col = t.column(c);
            for (std::size_t start = 0; start < rows; start += kBatch) {
                const std::size_t n = std::min(kBatch, rows - start);
                col.decode(start, n, buf);
                for (std::size_t i = 0; i < n; ++i) sum += static_cast<uint64_t>(buf[i]);
            }
        }
    }
    sum_ = static_cast<int64_t>(sum);
}
