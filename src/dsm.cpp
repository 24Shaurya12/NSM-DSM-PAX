#include "dsm.h"

#include <algorithm>
#include <stdexcept>

#include "layout_factory.h"
#include "tuple_sink.h"

namespace {

const bool kRegistered = [] {
    register_layout("dsm", [](const Table& t, const LayoutParams& p) {
        check_layout_params("dsm", p, {});
        return std::unique_ptr<Layout>(new DsmLayout(t));
    });
    return true;
}();

std::size_t batch_rows(const ExecOptions& opts) {
    if (opts.dsm_batch == 0) throw std::invalid_argument("dsm_batch must be at least 1");
    return opts.dsm_batch;
}

}  // namespace

DsmLayout::DsmLayout(const Table& t) : rows_(t.rows) {
    (void)kRegistered;
    columns_.reserve(t.cols);
    for (std::size_t c = 0; c < t.cols; ++c) {
        AlignedBuffer<int64_t> col(t.rows);
        for (std::size_t r = 0; r < t.rows; ++r) col[r] = t.at(r, c);
        columns_.push_back(std::move(col));
    }
}

QueryResult DsmLayout::scan_sum(const Projection& project) const {
    int64_t sum = 0;
    for (int c : project.cols) {
        const int64_t* col = column(c);
        for (std::size_t r = 0; r < rows_; ++r) sum += col[r];
    }
    return {static_cast<int64_t>(rows_), sum};
}

// Column at a time within each batch of rows: evaluate the predicates on their columns to get
// the qualifying positions, then read only those positions of the projected columns.
QueryResult DsmLayout::filter_sum(const Conjunction& where, const Projection& project,
                                  const ExecOptions& opts) const {
    const std::size_t batch = batch_rows(opts);
    QueryResult res;
    std::vector<uint32_t> sel(batch);
    for (std::size_t start = 0; start < rows_; start += batch) {
        std::size_t n = std::min(batch, rows_ - start);
        auto column_at = [&](int c) { return column(c) + start; };
        std::size_t m = select_positions(where, column_at, n, sel.data(), opts.filter);
        res.count += static_cast<int64_t>(m);
        for (int c : project.cols) {
            const int64_t* v = column_at(c);
            for (std::size_t j = 0; j < m; ++j) res.sum += v[sel[j]];
        }
    }
    return res;
}

// Same selection as filter_sum; then each qualifying tuple is stitched together by reading its
// position from each projected column array.
QueryResult DsmLayout::filter_materialize(const Conjunction& where, const Projection& project,
                                          const ExecOptions& opts) const {
    const std::size_t batch = batch_rows(opts);
    const std::size_t k = project.size();
    TupleSink sink(static_cast<int>(k));
    std::vector<uint32_t> sel(batch);
    std::vector<const int64_t*> src(k);
    for (std::size_t start = 0; start < rows_; start += batch) {
        std::size_t n = std::min(batch, rows_ - start);
        auto column_at = [&](int c) { return column(c) + start; };
        std::size_t m = select_positions(where, column_at, n, sel.data(), opts.filter);
        for (std::size_t j = 0; j < k; ++j) src[j] = column_at(project.cols[j]);
        for (std::size_t i = 0; i < m; ++i) {
            int64_t* out = sink.next();
            for (std::size_t j = 0; j < k; ++j) out[j] = src[j][sel[i]];
        }
    }
    return sink.finish();
}

// One value from each projected column array per lookup: k independent random accesses, each
// to a different array (k cache lines, and k pages for the TLB).
QueryResult DsmLayout::lookup(const RowIds& ids, const Projection& project) const {
    const std::size_t k = project.size();
    TupleSink sink(static_cast<int>(k));
    std::vector<const int64_t*> src(k);
    for (std::size_t j = 0; j < k; ++j) src[j] = column(project.cols[j]);
    for (std::size_t id : ids) {
        int64_t* out = sink.next();
        for (std::size_t j = 0; j < k; ++j) out[j] = src[j][id];
    }
    return sink.finish();
}
