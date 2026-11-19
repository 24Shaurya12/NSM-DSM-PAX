#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

#include "predicate.h"
#include "projection.h"
#include "row_ids.h"

// What a query does with the qualifying rows.
//   Sum:         SELECT COUNT(*), SUM(<projected columns>) FROM T [WHERE ...]
//   Materialize: SELECT <projected columns> FROM T [WHERE ...]   (tuple reconstruction)
//   Lookup:      fetch <projected columns> of `lookups.count` row ids drawn from lookups.dist
// Adding an operator: a value here, a name in op_name/parse, a reference implementation in
// reference.cpp, a virtual in Layout implemented per layout, and a case in run_query.
enum class Op { Sum, Materialize, Lookup };

const char* op_name(Op op);

// Point-lookup parameters (used by Op::Lookup).
struct LookupSpec {
    std::size_t count = 0;        // number of lookups
    std::string dist = "uniform"; // row-id distribution spec (row_ids.h): uniform | sorted | ...
    uint64_t seed = 1;
};

// One query, fully described. Built from a --query string or from the legacy
// --op/--where/--k flags.
struct QuerySpec {
    Op op = Op::Sum;
    Projection project;
    Conjunction where;            // empty = no filter
    std::string where_label = "none";  // the predicate as written, ',' shown as " & "
    LookupSpec lookups;
    std::string custom_label;     // optional label= key; otherwise label() is generated

    // Comma-free identifier for the CSV, e.g. "sum|c0..c3|c0<5 & c12<50".
    std::string label() const;
};

// Parses "op=sum;cols=0-3;where=c0<5,c12<50" (keys: op, cols, where, label, n, dist, seed).
// `cols` defaults to all columns, `where` to none. Throws std::invalid_argument on errors,
// including predicates or columns outside [0, table_cols).
QuerySpec parse_query_spec(const std::string& text, int table_cols);

// Parses a WHERE clause ("none" or a conjunction) and checks its columns.
void parse_where(const std::string& text, int table_cols, QuerySpec& q);

// The row ids of a lookup query on a table with `rows` rows (deterministic in the spec's seed).
RowIds lookup_ids(const QuerySpec& q, std::size_t rows);
