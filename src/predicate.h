#pragma once

#include <cstdint>
#include <string>
#include <vector>

// A predicate on one column: lo <= value <= hi (both inclusive). Every comparison the engine
// supports is a range: `c = v` is [v, v], `c < v` is [INT64_MIN, v - 1], `c >= v` is
// [v, INT64_MAX], and `c = a..b` is [a, b]. A WHERE clause is a conjunction of these.
struct Predicate {
    int col = 0;
    int64_t lo = 0;
    int64_t hi = 0;

    // One subtraction and one unsigned compare, with no branch: shifting by lo maps
    // [lo, hi] onto [0, hi - lo] and everything else above it (mod 2^64).
    bool test(int64_t v) const {
        return static_cast<uint64_t>(v) - static_cast<uint64_t>(lo) <= span();
    }
    uint64_t span() const { return static_cast<uint64_t>(hi) - static_cast<uint64_t>(lo); }

    // Compact text form, e.g. "c0<500" or "c3=10..20". Contains no commas (used in CSV).
    std::string str() const;
};

// Parses a conjunction such as "c0<500&c1=7&c2>=10&c3=10..20". Supported operators:
// =  <  <=  >  >=  and `=a..b` (inclusive range). Returns false on a syntax error or an
// empty range (e.g. c0<INT64_MIN), with a message in `error`.
bool parse_where(const std::string& text, std::vector<Predicate>& out, std::string& error);

// "c0<500&c1=7", or "-" for an empty conjunction (no WHERE clause).
std::string where_str(const std::vector<Predicate>& preds);
