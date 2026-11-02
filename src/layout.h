#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

class Query;

// Common interface for all physical layouts so the benchmark driver is layout-agnostic.
// Layouts only own storage; query logic lives in src/queries/.
class Layout {
public:
    virtual ~Layout() = default;

    virtual std::string name() const = 0;
    virtual std::size_t rows() const = 0;
    virtual std::size_t cols() const = 0;

    // Bytes actually allocated for the table data (PAX may include page padding).
    virtual std::size_t footprint_bytes() const = 0;

    // Runs q against this layout by calling q.run(*this) with the concrete layout type.
    virtual void run(Query& q) const = 0;
};
