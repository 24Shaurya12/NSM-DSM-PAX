#include "nsm.h"

#include <cstring>

#include "layout_factory.h"
#include "tuple_sink.h"

namespace {

const bool kRegistered = [] {
    register_layout("nsm", [](const Table& t, const LayoutParams& p) {
        check_layout_params("nsm", p, {});
        return std::unique_ptr<Layout>(new NsmLayout(t));
    });
    return true;
}();

// Whether a tuple satisfies every predicate. Branchy: stop at the first failing predicate.
// Branchless: evaluate all of them without a data-dependent branch.
inline bool qualifies(const int64_t* tuple, const Conjunction& where, FilterMode mode) {
    if (mode == FilterMode::Branchless) {
        bool q = true;
        for (const Predicate& p : where) q &= p.matches_nobranch(tuple[p.col]);
        return q;
    }
    for (const Predicate& p : where)
        if (!p.matches(tuple[p.col])) return false;
    return true;
}

}  // namespace

NsmLayout::NsmLayout(const Table& t) : rows_(t.rows), cols_(t.cols), data_(t.rows * t.cols) {
    (void)kRegistered;
    std::memcpy(data_.data(), t.values.data(), t.values.size() * sizeof(int64_t));
}

QueryResult NsmLayout::scan_sum(const Projection& project) const {
    const int first = project.contiguous_first();
    const std::size_t k = project.size();
    int64_t sum = 0;
    if (first >= 0) {  // contiguous projection: the original tight loop
        for (std::size_t r = 0; r < rows_; ++r) {
            const int64_t* tuple = row(r) + first;
            for (std::size_t c = 0; c < k; ++c) sum += tuple[c];
        }
    } else {
        for (std::size_t r = 0; r < rows_; ++r) sum += sum_fields(row(r), project, -1);
    }
    return {static_cast<int64_t>(rows_), sum};
}

// Tuple at a time: test the predicates on the tuple's own fields (branchy: stop at the first
// failing one) and add the projected fields of every qualifying tuple. The branch-free version
// evaluates every predicate and adds the fields masked by the match bit.
//
// The branchy loops are written out in full rather than through helpers: on this hot path the
// helper version measured 15-17% slower (CP1 regression check).
QueryResult NsmLayout::filter_sum(const Conjunction& where, const Projection& project,
                                  const ExecOptions& opts) const {
    const int first = project.contiguous_first();
    QueryResult res;
    if (opts.filter == FilterMode::Branchless) {
        for (std::size_t r = 0; r < rows_; ++r) {
            const int64_t* tuple = row(r);
            int64_t q = qualifies(tuple, where, FilterMode::Branchless);
            res.count += q;
            res.sum += sum_fields(tuple, project, first) & -q;
        }
        return res;
    }
    for (std::size_t r = 0; r < rows_; ++r) {
        const int64_t* tuple = row(r);
        bool q = true;
        for (const Predicate& p : where) {
            if (!p.matches(tuple[p.col])) {
                q = false;
                break;
            }
        }
        if (!q) continue;
        ++res.count;
        res.sum += sum_fields(tuple, project, first);
    }
    return res;
}

// Tuple at a time: the projected fields of a qualifying tuple are copied out together (a single
// memcpy when they are contiguous). The branch-free version always copies and then commits.
QueryResult NsmLayout::filter_materialize(const Conjunction& where, const Projection& project,
                                          const ExecOptions& opts) const {
    const int first = project.contiguous_first();
    const std::size_t k = project.size();
    TupleSink sink(static_cast<int>(k));
    if (opts.filter == FilterMode::Branchless) {
        for (std::size_t r = 0; r < rows_; ++r) {
            const int64_t* tuple = row(r);
            copy_fields(sink.slot(), tuple, project, first);
            sink.commit(qualifies(tuple, where, FilterMode::Branchless));
        }
        return sink.finish();
    }
    if (first >= 0) {  // contiguous projection: one memcpy per qualifying tuple
        for (std::size_t r = 0; r < rows_; ++r) {
            const int64_t* tuple = row(r);
            bool q = true;
            for (const Predicate& p : where) {
                if (!p.matches(tuple[p.col])) {
                    q = false;
                    break;
                }
            }
            if (!q) continue;
            std::memcpy(sink.next(), tuple + first, k * sizeof(int64_t));
        }
        return sink.finish();
    }
    for (std::size_t r = 0; r < rows_; ++r) {
        const int64_t* tuple = row(r);
        bool q = true;
        for (const Predicate& p : where) {
            if (!p.matches(tuple[p.col])) {
                q = false;
                break;
            }
        }
        if (!q) continue;
        copy_fields(sink.next(), tuple, project, -1);
    }
    return sink.finish();
}

// One row per lookup: the projected fields are next to each other in the row (one memcpy when
// they are contiguous), so a full-tuple lookup touches one or two cache lines.
QueryResult NsmLayout::lookup(const RowIds& ids, const Projection& project) const {
    const int first = project.contiguous_first();
    const std::size_t k = project.size();
    TupleSink sink(static_cast<int>(k));
    if (first >= 0) {
        for (std::size_t id : ids) std::memcpy(sink.next(), row(id) + first, k * sizeof(int64_t));
        return sink.finish();
    }
    for (std::size_t id : ids) copy_fields(sink.next(), row(id), project, -1);
    return sink.finish();
}
