#pragma once

#include <cstddef>
#include <string>
#include <vector>

// Splits s on `sep`. Empty input gives an empty vector; empty fields are kept.
std::vector<std::string> split(const std::string& s, char sep);

// "64M" -> 64 * 2^20. Suffixes K, M, G (binary, case-insensitive); no suffix means bytes.
// Throws std::invalid_argument on malformed input.
std::size_t parse_size(const std::string& s);

// Strict non-negative integer parse; throws std::invalid_argument naming `what` on failure.
long long parse_int(const std::string& s, const std::string& what);
