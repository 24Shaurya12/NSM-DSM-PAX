#pragma once

#include <vector>

#include "aligned_buffer.h"
#include "encodings/encoded_column.h"
#include "query.h"

// SELECT c1, c2, ... FROM table.
// Materialises the selected columns as row-major tuples into a preallocated output
// buffer (row r, j-th selected column at r * columns.size() + j), so every layout has to
// reconstruct each tuple.
class ProjectionQuery : public Query {
public:
    // Allocates (and touches) the rows x columns.size() output buffer.
    ProjectionQuery(std::vector<int> columns, std::size_t rows);

    std::string name() const override { return "projection"; }

    void run(const NsmLayout& t) override;
    void run(const DsmLayout& t) override;
    void run(const PaxLayout& t) override;
    void run(const EncodedDsmLayout& t) override;

    // Position-weighted checksum of the output, so values landing in the wrong row or
    // column are caught, not just wrong values.
    int64_t result() const override;

private:
    std::vector<int> columns_;
    AlignedBuffer<int64_t> out_;
    AlignedBuffer<int64_t> batch_;  // decode buffer for vectorized scans
};
