#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

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
};
