#include "util.h"

#include <cctype>
#include <sstream>
#include <stdexcept>

std::vector<std::string> split(const std::string& s, char sep) {
    std::vector<std::string> out;
    if (s.empty()) return out;
    std::string item;
    std::stringstream ss(s);
    while (std::getline(ss, item, sep)) out.push_back(item);
    if (s.back() == sep) out.emplace_back();
    return out;
}

std::size_t parse_size(const std::string& s) {
    std::size_t pos = 0;
    std::size_t n = 0;
    try {
        n = std::stoull(s, &pos);
    } catch (const std::exception&) {
        throw std::invalid_argument("bad size '" + s + "'");
    }
    std::string suffix = s.substr(pos);
    if (suffix.empty()) return n;
    if (suffix.size() == 1) {
        switch (std::toupper(static_cast<unsigned char>(suffix[0]))) {
            case 'K': return n << 10;
            case 'M': return n << 20;
            case 'G': return n << 30;
        }
    }
    throw std::invalid_argument("bad size suffix in '" + s + "'");
}

long long parse_int(const std::string& s, const std::string& what) {
    std::size_t pos = 0;
    long long v = 0;
    try {
        v = std::stoll(s, &pos);
    } catch (const std::exception&) {
        throw std::invalid_argument("bad " + what + " '" + s + "'");
    }
    if (pos != s.size()) throw std::invalid_argument("bad " + what + " '" + s + "'");
    return v;
}
