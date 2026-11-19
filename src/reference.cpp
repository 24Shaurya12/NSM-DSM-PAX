#include "reference.h"

#include <stdexcept>

ReferenceStats reference_stats(const Table& t, const Conjunction& where) {
    ReferenceStats s;
    s.col_sum.assign(t.cols, 0);
    for (std::size_t r = 0; r < t.rows; ++r) {
        bool qualifies = true;
        for (const Predicate& p : where) qualifies = qualifies && p.matches(t.at(r, p.col));
        if (!qualifies) continue;
        ++s.count;
        for (std::size_t c = 0; c < t.cols; ++c) s.col_sum[c] += t.at(r, c);
    }
    return s;
}

QueryResult reference_result(const ReferenceStats& s, Op op, const Projection& project) {
    if (op == Op::Lookup) throw std::logic_error("lookups need their row ids: use reference_lookup");
    QueryResult res{s.count, 0};
    for (std::size_t j = 0; j < project.size(); ++j) {
        int64_t col = s.col_sum[project.cols[j]];
        res.sum += op == Op::Materialize ? static_cast<int64_t>(j + 1) * col : col;
    }
    return res;
}

QueryResult reference_lookup(const Table& t, const RowIds& ids, const Projection& project) {
    QueryResult res{static_cast<int64_t>(ids.size()), 0};
    for (std::size_t id : ids) {
        if (id >= t.rows) throw std::out_of_range("lookup id beyond the table");
        for (std::size_t j = 0; j < project.size(); ++j)
            res.sum += static_cast<int64_t>(j + 1) * t.at(id, project.cols[j]);
    }
    return res;
}

QueryResult reference_result(const Table& t, const QuerySpec& q) {
    if (q.op == Op::Lookup) return reference_lookup(t, lookup_ids(q, t.rows), q.project);
    return reference_result(reference_stats(t, q.where), q.op, q.project);
}
