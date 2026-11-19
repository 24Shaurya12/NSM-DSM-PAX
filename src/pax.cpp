#include "pax.h"

#include <stdexcept>

#include "layout_factory.h"
#include "tuple_sink.h"

namespace {

const bool kRegistered = [] {
    register_layout("pax", [](const Table& t, const LayoutParams& p) {
        check_layout_params("pax", p, {"page"});
        return std::unique_ptr<Layout>(
            new PaxLayout(t, layout_param_size(p, "page", PaxLayout::kDefaultPageSize)));
    });
    return true;
}();

}  // namespace

PaxLayout::PaxLayout(const Table& t, std::size_t page_size)
    : rows_(t.rows),
      cols_(t.cols),
      page_size_(page_size),
      tuples_per_page_(page_size / (t.cols * sizeof(int64_t))) {
    (void)kRegistered;
    if (page_size_ % kCacheLineSize != 0)
        throw std::invalid_argument("PAX page size must be a multiple of 64 bytes");
    if (tuples_per_page_ == 0)
        throw std::invalid_argument("PAX page size too small to hold a single tuple");

    std::size_t pages = (rows_ + tuples_per_page_ - 1) / tuples_per_page_;
    for (std::size_t p = 0; p < pages; ++p) {
        std::size_t first = p * tuples_per_page_;
        std::size_t remaining = rows_ - first;
        page_tuple_counts_.push_back(remaining < tuples_per_page_ ? remaining : tuples_per_page_);
    }

    buffer_ = AlignedBuffer<int64_t>(pages * page_size_ / sizeof(int64_t));
    for (std::size_t p = 0; p < pages; ++p) {
        std::size_t first = p * tuples_per_page_;
        for (std::size_t c = 0; c < cols_; ++c) {
            int64_t* mp = minipage(p, c);
            for (std::size_t i = 0; i < page_tuple_counts_[p]; ++i) mp[i] = t.at(first + i, c);
        }
    }
}

QueryResult PaxLayout::scan_sum(const Projection& project) const {
    const int first = project.contiguous_first();
    const int k = static_cast<int>(project.size());
    int64_t sum = 0;
    if (first >= 0) {  // contiguous projection: counted loop over adjacent minipages
        for (std::size_t p = 0; p < num_pages(); ++p) {
            std::size_t n = page_tuple_counts_[p];
            for (int c = first; c < first + k; ++c) {
                const int64_t* mp = minipage(p, c);
                for (std::size_t i = 0; i < n; ++i) sum += mp[i];
            }
        }
        return {static_cast<int64_t>(rows_), sum};
    }
    for (std::size_t p = 0; p < num_pages(); ++p) {
        std::size_t n = page_tuple_counts_[p];
        for (int c : project.cols) {
            const int64_t* mp = minipage(p, c);
            for (std::size_t i = 0; i < n; ++i) sum += mp[i];
        }
    }
    return {static_cast<int64_t>(rows_), sum};
}

// Page at a time: evaluate the predicates on their minipages to get the qualifying positions in
// the page, then read only those positions of the projected columns' minipages.
QueryResult PaxLayout::filter_sum(const Conjunction& where, const Projection& project,
                                  const ExecOptions& opts) const {
    QueryResult res;
    std::vector<uint32_t> sel(tuples_per_page_);
    for (std::size_t p = 0; p < num_pages(); ++p) {
        auto minipage_at = [&](int c) { return minipage(p, c); };
        std::size_t m =
            select_positions(where, minipage_at, page_tuple_counts_[p], sel.data(), opts.filter);
        res.count += static_cast<int64_t>(m);
        for (int c : project.cols) {
            const int64_t* v = minipage(p, c);
            for (std::size_t j = 0; j < m; ++j) res.sum += v[sel[j]];
        }
    }
    return res;
}

// Same selection as filter_sum; then each qualifying tuple is stitched together from the
// projected columns' minipages within its page.
//
// For a contiguous projection the minipages of consecutive columns are exactly
// tuples_per_page values apart, so the copy loop walks them with a constant stride (one load
// per value). The general path reads each column's minipage pointer from a small array, which
// adds a dependent load per value; it measured ~40% slower for the contiguous case (CP1).
QueryResult PaxLayout::filter_materialize(const Conjunction& where, const Projection& project,
                                          const ExecOptions& opts) const {
    const std::size_t k = project.size();
    const int first = project.contiguous_first();
    const std::size_t stride = tuples_per_page_;
    TupleSink sink(static_cast<int>(k));
    std::vector<uint32_t> sel(tuples_per_page_);
    std::vector<const int64_t*> src(k);
    for (std::size_t p = 0; p < num_pages(); ++p) {
        auto minipage_at = [&](int c) { return minipage(p, c); };
        std::size_t m =
            select_positions(where, minipage_at, page_tuple_counts_[p], sel.data(), opts.filter);
        if (first >= 0) {
            const int64_t* base = minipage(p, first);
            for (std::size_t i = 0; i < m; ++i) {
                int64_t* out = sink.next();
                const int64_t* v = base + sel[i];
                for (std::size_t j = 0; j < k; ++j) out[j] = v[j * stride];
            }
            continue;
        }
        for (std::size_t j = 0; j < k; ++j) src[j] = minipage_at(project.cols[j]);
        for (std::size_t i = 0; i < m; ++i) {
            int64_t* out = sink.next();
            for (std::size_t j = 0; j < k; ++j) out[j] = src[j][sel[i]];
        }
    }
    return sink.finish();
}

// Find the page (id / tuples_per_page) and the slot within it (id % tuples_per_page), then read
// the slot of each projected minipage: k values in one page, tuples_per_page * 8 bytes apart.
QueryResult PaxLayout::lookup(const RowIds& ids, const Projection& project) const {
    const std::size_t k = project.size();
    const int first = project.contiguous_first();
    const std::size_t stride = tuples_per_page_;
    TupleSink sink(static_cast<int>(k));
    if (first >= 0) {  // constant stride between consecutive columns (see filter_materialize)
        for (std::size_t id : ids) {
            int64_t* out = sink.next();
            const int64_t* v = minipage(id / stride, first) + id % stride;
            for (std::size_t j = 0; j < k; ++j) out[j] = v[j * stride];
        }
        return sink.finish();
    }
    for (std::size_t id : ids) {
        int64_t* out = sink.next();
        const std::size_t p = id / stride, slot = id % stride;
        for (std::size_t j = 0; j < k; ++j) out[j] = minipage(p, project.cols[j])[slot];
    }
    return sink.finish();
}
