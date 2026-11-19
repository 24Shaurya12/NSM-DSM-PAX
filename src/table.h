#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

// Logical table: the source of truth every physical layout is built from.
// Values are stored row-major (row r, column c at r * cols + c).
struct Table {
    std::size_t rows = 0;
    std::size_t cols = 0;
    std::vector<int64_t> values;

    int64_t at(std::size_t r, std::size_t c) const { return values[r * cols + c]; }
};

// How each column's values are generated. Every column is generated independently.
//   uniform  uniform in [0, domain), default domain 100 (7 bits; the original benchmark data)
//   wide     uniform over the full int64 range          (incompressible)
//   sorted   increasing: each value = previous + uniform [0, 8)
//   runs     runs of equal values, values in [0, 2^20), run lengths uniform in
//            [1, 2 * run_length - 1] (mean run_length)
//   zipf     Zipf(s = 1) over 10,000 distinct values, each a full-range int64
enum class Distribution { Uniform, Wide, Sorted, Runs, Zipf };

const std::vector<Distribution>& all_distributions();
std::string distribution_name(Distribution d);
// Returns false if `name` is not a distribution.
bool parse_distribution(const std::string& name, Distribution& out);

// Fills a rows x cols table from a fixed-seed mt19937_64.
Table generate_table(std::size_t rows, std::size_t cols, uint64_t seed,
                     Distribution dist = Distribution::Uniform, std::size_t run_length = 64,
                     int64_t domain = 100);
