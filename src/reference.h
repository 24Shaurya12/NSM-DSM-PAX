#pragma once

#include <cstdint>
#include <vector>

#include "predicate.h"
#include "query_spec.h"
#include "table.h"

// Straightforward evaluation of queries on the logical table, used to check every layout.
// Every new operator gets its reference implementation here before any layout implements it.

// One pass over the table for a WHERE clause: how many rows qualify and the per-column sums
// over those rows. Every Sum/Materialize result for that clause follows from these, whatever
// the projection, so callers can compute them once per clause.
struct ReferenceStats {
    int64_t count = 0;
    std::vector<int64_t> col_sum;
};

ReferenceStats reference_stats(const Table& t, const Conjunction& where);

// Expected result of `op` over `project` given the stats of its WHERE clause.
//   Sum:         {count, sum of the projected columns}
//   Materialize: {count, TupleSink checksum = sum over output position j of (j+1) * column sum}
QueryResult reference_result(const ReferenceStats& s, Op op, const Projection& project);

// Expected result of a lookup: {ids.size(), checksum of the looked-up tuples}.
QueryResult reference_lookup(const Table& t, const RowIds& ids, const Projection& project);

// Convenience for one query (one table pass; lookups generate their ids from the spec).
QueryResult reference_result(const Table& t, const QuerySpec& q);
