#include "queries/projection.h"

#include "dsm.h"
#include "nsm.h"
#include "pax.h"

ProjectionQuery::ProjectionQuery(std::vector<int> columns, std::size_t rows)
    : columns_(std::move(columns)), out_(rows * columns_.size()) {}

void ProjectionQuery::run(const NsmLayout& t) {
    const std::size_t m = columns_.size();
    int64_t* out = out_.data();
    for (std::size_t r = 0; r < t.rows(); ++r) {
        const int64_t* tuple = t.row(r);
        int64_t* dst = out + r * m;
        for (std::size_t j = 0; j < m; ++j) dst[j] = tuple[columns_[j]];
    }
}

void ProjectionQuery::run(const DsmLayout& t) {
    const std::size_t m = columns_.size();
    std::vector<const int64_t*> cols(m);
    for (std::size_t j = 0; j < m; ++j) cols[j] = t.column(columns_[j]);

    // Tuple at a time: each output row gathers one value from each of m column arrays.
    int64_t* out = out_.data();
    for (std::size_t r = 0; r < t.rows(); ++r) {
        int64_t* dst = out + r * m;
        for (std::size_t j = 0; j < m; ++j) dst[j] = cols[j][r];
    }
}

void ProjectionQuery::run(const PaxLayout& t) {
    const std::size_t m = columns_.size();
    std::vector<const int64_t*> mps(m);
    int64_t* out = out_.data();
    for (std::size_t p = 0; p < t.num_pages(); ++p) {
        for (std::size_t j = 0; j < m; ++j) mps[j] = t.minipage(p, columns_[j]);

        // Same gather as DSM, but all m minipages sit within one page.
        std::size_t n = t.page_tuple_count(p);
        int64_t* page_out = out + t.page_first_row(p) * m;
        for (std::size_t i = 0; i < n; ++i) {
            int64_t* dst = page_out + i * m;
            for (std::size_t j = 0; j < m; ++j) dst[j] = mps[j][i];
        }
    }
}

int64_t ProjectionQuery::result() const {
    // Unsigned arithmetic so overflow wraps instead of being undefined.
    uint64_t checksum = 0;
    for (std::size_t i = 0; i < out_.size(); ++i)
        checksum += static_cast<uint64_t>(out_[i]) * (i + 1);
    return static_cast<int64_t>(checksum);
}
