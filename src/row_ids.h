#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <vector>

// Row-id generators for point lookups, chosen by name so new access distributions plug in
// without touching layouts. A spec is "name" or "name:key=value:...", like column distributions.
// Built in:
//   uniform  ids drawn uniformly with replacement from [0, rows), in random order
//   sorted   the same ids, sorted ascending (an index range scan / sorted probe batch)
// Ids are generated before timing, so every layout sees exactly the same lookups. Each lookup is
// independent of the previous ones (the CPU may overlap their cache misses), as in a batch of
// index probes.
using RowIds = std::vector<std::size_t>;
using RowIdFactory = std::function<RowIds(const std::map<std::string, std::string>& args,
                                          std::size_t rows, std::size_t count, uint64_t seed)>;

void register_row_id_dist(const std::string& name, RowIdFactory factory);
std::vector<std::string> registered_row_id_dists();

// Throws std::invalid_argument for unknown names/keys or rows == 0.
RowIds make_row_ids(const std::string& spec, std::size_t rows, std::size_t count, uint64_t seed);
