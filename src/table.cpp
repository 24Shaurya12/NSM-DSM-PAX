#include "table.h"

#include <algorithm>
#include <memory>

Table generate_table(std::size_t rows, std::size_t cols, uint64_t seed, const DistSpec& dists) {
    Table t;
    t.rows = rows;
    t.cols = cols;
    t.values.resize(rows * cols);

    std::vector<std::unique_ptr<ColumnGenerator>> gens;
    for (std::size_t c = 0; c < cols; ++c)
        gens.push_back(make_generator(dists.for_column(static_cast<int>(c)), rows,
                                      seed * 1000003 + c));

    // Fill in row blocks so every generator writes into a cache-resident slice of the table.
    constexpr std::size_t kBlock = 4096;
    for (std::size_t first = 0; first < rows; first += kBlock) {
        std::size_t n = std::min(kBlock, rows - first);
        for (std::size_t c = 0; c < cols; ++c)
            gens[c]->fill(t.values.data() + first * cols + c, cols, first, n);
    }
    return t;
}
