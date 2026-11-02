#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

// A predicate on one attribute, normalised to the closed range lo <= value <= hi.
// Equality (c = v) has lo == hi; a one-sided comparison uses an int64 limit for the open end.
struct Predicate {
    int col = 0;
    int64_t lo = std::numeric_limits<int64_t>::min();
    int64_t hi = std::numeric_limits<int64_t>::max();

    bool matches(int64_t v) const { return lo <= v && v <= hi; }
};

// A conjunction: a row qualifies if it satisfies every predicate. Empty means every row.
using Conjunction = std::vector<Predicate>;

// Result of SELECT COUNT(*), SUM(c0 + ... + c{k-1}) FROM T WHERE <conjunction>.
struct QueryResult {
    int64_t count = 0;
    int64_t sum = 0;

    bool operator==(const QueryResult& o) const { return count == o.count && sum == o.sum; }
    bool operator!=(const QueryResult& o) const { return !(*this == o); }
};

// Parses a comma-separated list of terms, all of which must hold:
//   c3=42       equality
//   c0<10       also <=, >, >=
//   c2=10..19   inclusive range (BETWEEN 10 AND 19)
// Throws std::invalid_argument on malformed input.
Conjunction parse_conjunction(const std::string& text);

// Writes the positions i in [0, n) whose row satisfies every predicate into sel, in increasing
// order, and returns how many there are. `column(c)` returns a pointer to the n values of
// column c for this batch of rows. The first predicate scans all n values; each later predicate
// only re-checks the positions that are still selected.
template <typename ColumnAt>
std::size_t select_positions(const Conjunction& where, ColumnAt column, std::size_t n,
                             uint32_t* sel) {
    std::size_t m = 0;
    if (where.empty()) {
        for (std::size_t i = 0; i < n; ++i) sel[m++] = static_cast<uint32_t>(i);
        return m;
    }

    const Predicate& first = where[0];
    const int64_t* v = column(first.col);
    for (std::size_t i = 0; i < n; ++i)
        if (first.matches(v[i])) sel[m++] = static_cast<uint32_t>(i);

    for (std::size_t p = 1; p < where.size(); ++p) {
        const Predicate& pred = where[p];
        v = column(pred.col);
        std::size_t kept = 0;
        for (std::size_t j = 0; j < m; ++j)
            if (pred.matches(v[sel[j]])) sel[kept++] = sel[j];
        m = kept;
    }
    return m;
}
