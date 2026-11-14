#include "queries/projection.h"

#include <algorithm>
#include <memory>

#include "dsm.h"
#include "encoded_dsm.h"
#include "nsm.h"
#include "pax.h"

ProjectionQuery::ProjectionQuery(std::vector<int> columns, std::size_t rows)
    : columns_(std::move(columns)), out_(rows * columns_.size()), batch_(kBatch) {}

void ProjectionQuery::run(const NsmLayout& t) {
    // Read once: rows() is virtual, so `r < t.rows()` would re-check the vtable on every
    // iteration and stop the compiler from vectorizing the loop.
    const std::size_t rows = t.rows();
    const std::size_t m = columns_.size();
    int64_t* out = out_.data();
    for (std::size_t r = 0; r < rows; ++r) {
        const int64_t* tuple = t.row(r);
        int64_t* dst = out + r * m;
        for (std::size_t j = 0; j < m; ++j) dst[j] = tuple[columns_[j]];
    }
}

void ProjectionQuery::run(const DsmLayout& t) {
    // Read once: rows() is virtual, so `r < t.rows()` would re-check the vtable on every
    // iteration and stop the compiler from vectorizing the loop.
    const std::size_t rows = t.rows();
    const std::size_t m = columns_.size();
    std::vector<const int64_t*> cols(m);
    for (std::size_t j = 0; j < m; ++j) cols[j] = t.column(columns_[j]);

    // Tuple at a time: each output row gathers one value from each of m column arrays.
    int64_t* out = out_.data();
    for (std::size_t r = 0; r < rows; ++r) {
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

void ProjectionQuery::run(const EncodedDsmLayout& t) {
    // Read once: rows() is virtual, so `r < t.rows()` would re-check the vtable on every
    // iteration and stop the compiler from vectorizing the loop.
    const std::size_t rows = t.rows();
    const std::size_t m = columns_.size();
    int64_t* out = out_.data();
    if (t.mode() == ScanMode::Value) {
        // Tuple at a time, like DSM: each output row pulls one value from each of m cursors.
        std::vector<std::unique_ptr<ColumnCursor>> cursors;
        for (int c : columns_) cursors.push_back(t.column(c).cursor());
        for (std::size_t r = 0; r < rows; ++r) {
            int64_t* dst = out + r * m;
            for (std::size_t j = 0; j < m; ++j) dst[j] = cursors[j]->next();
        }
    } else {
        // Batch at a time: decode kBatch values of one column, then scatter them into
        // column j of the batch's kBatch output rows.
        int64_t* buf = batch_.data();
        for (std::size_t start = 0; start < rows; start += kBatch) {
            const std::size_t n = std::min(kBatch, rows - start);
            int64_t* batch_out = out + start * m;
            for (std::size_t j = 0; j < m; ++j) {
                t.column(columns_[j]).decode(start, n, buf);
                for (std::size_t i = 0; i < n; ++i) batch_out[i * m + j] = buf[i];
            }
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
