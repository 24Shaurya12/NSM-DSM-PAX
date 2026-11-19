#include "row_ids.h"

#include <algorithm>
#include <map>
#include <stdexcept>

#include "util.h"

namespace {

std::map<std::string, RowIdFactory>& registry() {
    static std::map<std::string, RowIdFactory> r;
    return r;
}

uint64_t splitmix64(uint64_t& state) {
    uint64_t z = (state += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

void no_args(const std::string& name, const std::map<std::string, std::string>& args) {
    if (!args.empty())
        throw std::invalid_argument("row-id distribution '" + name + "' takes no parameters");
}

RowIds uniform_ids(std::size_t rows, std::size_t count, uint64_t seed) {
    RowIds ids(count);
    uint64_t state = seed * 0x2545f4914f6cdd1dULL + 1;
    for (std::size_t i = 0; i < count; ++i) ids[i] = splitmix64(state) % rows;
    return ids;
}

const bool kBuiltins = [] {
    register_row_id_dist("uniform", [](const auto& args, std::size_t rows, std::size_t count,
                                       uint64_t seed) {
        no_args("uniform", args);
        return uniform_ids(rows, count, seed);
    });
    register_row_id_dist("sorted", [](const auto& args, std::size_t rows, std::size_t count,
                                      uint64_t seed) {
        no_args("sorted", args);
        RowIds ids = uniform_ids(rows, count, seed);
        std::sort(ids.begin(), ids.end());
        return ids;
    });
    return true;
}();

}  // namespace

void register_row_id_dist(const std::string& name, RowIdFactory factory) {
    registry()[name] = std::move(factory);
}

std::vector<std::string> registered_row_id_dists() {
    (void)kBuiltins;
    std::vector<std::string> names;
    for (const auto& [name, f] : registry()) names.push_back(name);
    return names;
}

RowIds make_row_ids(const std::string& spec, std::size_t rows, std::size_t count, uint64_t seed) {
    (void)kBuiltins;
    if (rows == 0) throw std::invalid_argument("lookups on an empty table");
    std::vector<std::string> parts = split(spec, ':');
    const std::string& name = parts.at(0);
    std::map<std::string, std::string> args;
    for (std::size_t i = 1; i < parts.size(); ++i) {
        std::size_t eq = parts[i].find('=');
        if (eq == std::string::npos)
            throw std::invalid_argument("expected key=value in row-id spec '" + spec + "'");
        args[parts[i].substr(0, eq)] = parts[i].substr(eq + 1);
    }
    auto it = registry().find(name);
    if (it == registry().end()) {
        std::string known;
        for (const std::string& n : registered_row_id_dists()) known += (known.empty() ? "" : ", ") + n;
        throw std::invalid_argument("unknown row-id distribution '" + name + "' (" + known + ")");
    }
    return it->second(args, rows, count, seed);
}
