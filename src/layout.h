#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

#include "predicate.h"

// Common interface for all physical layouts so the benchmark driver is layout-agnostic.
// New operators (predicated scans, tuple reconstruction, ...) are added here as further
// virtual methods, implemented by each layout over its existing storage.
class Layout {
public:
    virtual ~Layout() = default;

    virtual std::string name() const = 0;
    virtual std::size_t rows() const = 0;
    virtual std::size_t cols() const = 0;

    // Bytes actually allocated for the table data (PAX may include page padding).
    virtual std::size_t footprint_bytes() const = 0;

    // SUM over all values of columns [0, k).
    virtual int64_t sum_first_k(int k) const = 0;

    // COUNT(*) and SUM over columns [0, k) of the rows that satisfy every predicate in `where`.
    virtual QueryResult filter_sum(const Conjunction& where, int k) const = 0;

    // Tuple reconstruction: SELECT c0, ..., c{k-1} FROM T WHERE `where`. Each qualifying row is
    // written as a row-major tuple of its first k attributes into a TupleSink. Returns the number
    // of tuples and the sink's checksum.
    virtual QueryResult filter_materialize(const Conjunction& where, int k) const = 0;

    // Evicts all of the table's data from the CPU caches (for cold-cache measurements).
    virtual void flush_cache() const = 0;
};
