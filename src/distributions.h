#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <vector>

// Value generators for table columns, chosen by name so new distributions plug in without
// touching layouts or queries.
//
// A spec is "name" or "name:key=value:key=value", e.g. "uniform:lo=0:hi=99", "sorted",
// "clustered:run=1024". Built in: uniform, sorted, clustered. To add one, implement
// ColumnGenerator and call register_distribution (see distributions.cpp).
class ColumnGenerator {
public:
    virtual ~ColumnGenerator() = default;
    // Writes the values of rows [first_row, first_row + n) to out[0], out[stride], ...
    // Rows are requested in increasing order, in blocks.
    virtual void fill(int64_t* out, std::size_t stride, std::size_t first_row, std::size_t n) = 0;
};

using DistArgs = std::map<std::string, std::string>;
using DistFactory = std::function<std::unique_ptr<ColumnGenerator>(
    const DistArgs& args, std::size_t rows, uint64_t seed)>;

void register_distribution(const std::string& name, DistFactory factory);
std::vector<std::string> registered_distributions();

// Builds the generator for one column. Throws std::invalid_argument for unknown names/keys.
std::unique_ptr<ColumnGenerator> make_generator(const std::string& spec, std::size_t rows,
                                                uint64_t seed);

// Which distribution each column uses: a default plus per-column overrides.
struct DistSpec {
    std::string default_spec = "uniform:lo=0:hi=99";
    std::map<int, std::string> per_column;

    const std::string& for_column(int c) const {
        auto it = per_column.find(c);
        return it == per_column.end() ? default_spec : it->second;
    }
    // Adds "default=SPEC" or "cN=SPEC" (the --dist flag).
    void add(const std::string& assignment);
    // Comma-free description for logs/CSV, e.g. "uniform:lo=0:hi=99|c0=sorted".
    std::string label() const;
};
