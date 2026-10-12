#include "table.h"

#include <random>

Table generate_table(std::size_t rows, std::size_t cols, uint64_t seed) {
    Table t;
    t.rows = rows;
    t.cols = cols;
    t.values.resize(rows * cols);

    std::mt19937_64 rng(seed);
    std::uniform_int_distribution<int64_t> dist(0, 99);
    for (auto& v : t.values) v = dist(rng);
    return t;
}
