#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <string>

#include "exec_options.h"
#include "predicate.h"
#include "projection.h"
#include "row_ids.h"

// Physical parameters of a layout, e.g. {"page": "16384"} for PAX. Printed in the CSV.
using LayoutParams = std::map<std::string, std::string>;

// Common interface for all physical layouts so the driver is layout-agnostic.
//
// Adding a layout: subclass this, implement every operator, and register a builder in the
// layout's .cpp file with register_layout (layout_factory.h). Nothing else changes.
// Adding an operator: add a virtual here and implement it in every layout (plus a reference
// implementation in reference.cpp and a case in the driver's run_query).
class Layout {
public:
    virtual ~Layout() = default;

    virtual std::string name() const = 0;
    virtual LayoutParams params() const { return {}; }
    virtual std::size_t rows() const = 0;
    virtual std::size_t cols() const = 0;

    // Bytes actually allocated for the table data (PAX may include page padding).
    virtual std::size_t footprint_bytes() const = 0;

    // Whether ExecOptions::dsm_batch changes how this layout runs filters, so the driver only
    // sweeps it where it matters.
    virtual bool uses_batch_rows() const { return false; }

    // SELECT COUNT(*), SUM(<projected columns>) FROM T, without a filter.
    virtual QueryResult scan_sum(const Projection& project) const = 0;

    // The same with a WHERE clause: rows must satisfy every predicate in `where`.
    virtual QueryResult filter_sum(const Conjunction& where, const Projection& project,
                                   const ExecOptions& opts) const = 0;

    // Tuple reconstruction: SELECT <projected columns> FROM T WHERE `where`. Each qualifying
    // row is written as a row-major tuple into a TupleSink. Returns {tuples, sink checksum}.
    virtual QueryResult filter_materialize(const Conjunction& where, const Projection& project,
                                           const ExecOptions& opts) const = 0;

    // Point lookups: for each id in `ids` (in order), write the projected columns of that row as
    // one tuple into a TupleSink. Returns {ids.size(), sink checksum}. Ids are < rows().
    virtual QueryResult lookup(const RowIds& ids, const Projection& project) const = 0;

    // Evicts all of the table's data from the CPU caches (for cold-cache measurements).
    virtual void flush_cache() const = 0;
};
