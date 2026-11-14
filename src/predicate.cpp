#include "predicate.h"

#include <cerrno>
#include <cstdlib>
#include <limits>
#include <sstream>

namespace {

constexpr int64_t kMin = std::numeric_limits<int64_t>::min();
constexpr int64_t kMax = std::numeric_limits<int64_t>::max();

bool parse_int(const std::string& s, int64_t& out) {
    if (s.empty()) return false;
    char* end = nullptr;
    errno = 0;
    long long v = std::strtoll(s.c_str(), &end, 10);
    if (errno != 0 || *end != '\0') return false;
    out = v;
    return true;
}

bool parse_one(const std::string& term, Predicate& p, std::string& error) {
    // c<digits><op><value>
    if (term.size() < 3 || term[0] != 'c') {
        error = "predicate '" + term + "' must look like c<column><op><value>";
        return false;
    }
    std::size_t i = 1;
    while (i < term.size() && term[i] >= '0' && term[i] <= '9') ++i;
    if (i == 1) {
        error = "predicate '" + term + "' has no column number";
        return false;
    }
    p.col = std::atoi(term.substr(1, i - 1).c_str());
    std::string op, value;
    if (term.compare(i, 2, "<=") == 0 || term.compare(i, 2, ">=") == 0) {
        op = term.substr(i, 2);
        value = term.substr(i + 2);
    } else if (i < term.size() && (term[i] == '<' || term[i] == '>' || term[i] == '=')) {
        op = term.substr(i, 1);
        value = term.substr(i + 1);
    } else {
        error = "predicate '" + term + "' has no operator (= < <= > >=)";
        return false;
    }

    int64_t v = 0, v2 = 0;
    std::size_t dots = value.find("..");
    if (op == "=" && dots != std::string::npos) {
        if (!parse_int(value.substr(0, dots), v) || !parse_int(value.substr(dots + 2), v2) ||
            v > v2) {
            error = "bad range in predicate '" + term + "'";
            return false;
        }
        p.lo = v;
        p.hi = v2;
        return true;
    }
    if (!parse_int(value, v)) {
        error = "bad value in predicate '" + term + "'";
        return false;
    }
    if (op == "=") p.lo = p.hi = v;
    else if (op == "<=") p.lo = kMin, p.hi = v;
    else if (op == ">=") p.lo = v, p.hi = kMax;
    else if (op == "<") {
        if (v == kMin) {
            error = "predicate '" + term + "' matches nothing";
            return false;
        }
        p.lo = kMin, p.hi = v - 1;
    } else {  // ">"
        if (v == kMax) {
            error = "predicate '" + term + "' matches nothing";
            return false;
        }
        p.lo = v + 1, p.hi = kMax;
    }
    return true;
}

}  // namespace

std::string Predicate::str() const {
    std::ostringstream s;
    s << 'c' << col;
    if (lo == kMin && hi == kMax) s << ">=" << lo;
    else if (lo == kMin) s << '<' << (hi == kMax ? hi : hi + 1);
    else if (hi == kMax) s << ">=" << lo;
    else if (lo == hi) s << '=' << lo;
    else s << '=' << lo << ".." << hi;
    return s.str();
}

bool parse_where(const std::string& text, std::vector<Predicate>& out, std::string& error) {
    std::stringstream ss(text);
    std::string term;
    while (std::getline(ss, term, '&')) {
        Predicate p;
        if (!parse_one(term, p, error)) return false;
        out.push_back(p);
    }
    return true;
}

std::string where_str(const std::vector<Predicate>& preds) {
    if (preds.empty()) return "-";
    std::string s;
    for (std::size_t i = 0; i < preds.size(); ++i) s += (i ? "&" : "") + preds[i].str();
    return s;
}
