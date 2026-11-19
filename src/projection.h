#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

// The columns a query aggregates or returns, in output order (any subset, any order).
struct Projection {
    std::vector<int> cols;

    std::size_t size() const { return cols.size(); }

    // First column if `cols` is the ascending contiguous range [first, first + size), else -1.
    // Kernels use it to keep the fast pointer-offset loops for the common prefix case.
    int contiguous_first() const {
        for (std::size_t j = 1; j < cols.size(); ++j)
            if (cols[j] != cols[0] + static_cast<int>(j)) return -1;
        return cols.empty() ? -1 : cols[0];
    }

    // Comma-free label for the CSV: "c0..c3", "c0|c4|c8" or "c5".
    std::string label() const {
        if (cols.empty()) return "none";
        int first = contiguous_first();
        if (first >= 0 && cols.size() > 1)
            return "c" + std::to_string(first) + "..c" + std::to_string(first + size() - 1);
        std::string s;
        for (std::size_t j = 0; j < cols.size(); ++j)
            s += (j ? "|c" : "c") + std::to_string(cols[j]);
        return s;
    }
};

// Columns [0, k).
inline Projection prefix_projection(int k) {
    Projection p;
    for (int c = 0; c < k; ++c) p.cols.push_back(c);
    return p;
}

// Parses "all", "3", "0-3", "c0-c3", "0,4,8" or any comma-separated mix, for a table with
// `table_cols` columns. Throws std::invalid_argument on malformed or out-of-range input.
Projection parse_projection(const std::string& text, int table_cols);

// SUM of the projected fields of one row-major tuple.
inline int64_t sum_fields(const int64_t* tuple, const Projection& p, int first) {
    int64_t s = 0;
    const std::size_t k = p.size();
    if (first >= 0) {
        const int64_t* t = tuple + first;
        for (std::size_t c = 0; c < k; ++c) s += t[c];
    } else {
        const int* cols = p.cols.data();
        for (std::size_t j = 0; j < k; ++j) s += tuple[cols[j]];
    }
    return s;
}

// Copies the projected fields of one row-major tuple to `out` in projection order.
inline void copy_fields(int64_t* out, const int64_t* tuple, const Projection& p, int first) {
    const std::size_t k = p.size();
    if (first >= 0) {
        std::memcpy(out, tuple + first, k * sizeof(int64_t));
    } else {
        const int* cols = p.cols.data();
        for (std::size_t j = 0; j < k; ++j) out[j] = tuple[cols[j]];
    }
}
