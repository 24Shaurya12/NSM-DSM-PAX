#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

#include "distributions.h"

// Logical table: the source of truth every physical layout is built from.
// Values are stored row-major (row r, column c at r * cols + c).
struct Table {
    std::size_t rows = 0;
    std::size_t cols = 0;
    std::vector<int64_t> values;

    int64_t at(std::size_t r, std::size_t c) const { return values[r * cols + c]; }
};

// Fills a rows x cols table. Column c draws its values from dists.for_column(c), with its own
// deterministic seed derived from `seed`, so the same arguments always give the same table.
Table generate_table(std::size_t rows, std::size_t cols, uint64_t seed,
                     const DistSpec& dists = DistSpec());
