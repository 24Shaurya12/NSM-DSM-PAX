#include "predicate.h"

#include <cctype>
#include <sstream>
#include <stdexcept>

namespace {

[[noreturn]] void bad(const std::string& term, const std::string& why) {
    throw std::invalid_argument("bad predicate '" + term + "': " + why);
}

int64_t parse_int(const std::string& term, const std::string& s) {
    std::size_t pos = 0;
    int64_t v = 0;
    try {
        v = std::stoll(s, &pos);
    } catch (const std::exception&) {
        bad(term, "expected an integer, got '" + s + "'");
    }
    if (pos != s.size()) bad(term, "expected an integer, got '" + s + "'");
    return v;
}

Predicate parse_term(const std::string& term) {
    if (term.size() < 2 || term[0] != 'c' || !std::isdigit(static_cast<unsigned char>(term[1])))
        bad(term, "must start with a column such as c0");

    std::size_t i = 1;
    while (i < term.size() && std::isdigit(static_cast<unsigned char>(term[i]))) ++i;
    Predicate p;
    p.col = std::stoi(term.substr(1, i - 1));

    std::string op;
    while (i < term.size() && (term[i] == '<' || term[i] == '>' || term[i] == '=')) op += term[i++];
    std::string rhs = term.substr(i);
    if (rhs.empty()) bad(term, "missing value");

    if (op == "=") {
        std::size_t dots = rhs.find("..");
        if (dots == std::string::npos) {
            p.lo = p.hi = parse_int(term, rhs);
        } else {
            p.lo = parse_int(term, rhs.substr(0, dots));
            p.hi = parse_int(term, rhs.substr(dots + 2));
            if (p.lo > p.hi) bad(term, "empty range");
        }
    } else if (op == "<") {
        int64_t v = parse_int(term, rhs);
        if (v == p.lo) bad(term, "empty range");
        p.hi = v - 1;
    } else if (op == "<=") {
        p.hi = parse_int(term, rhs);
    } else if (op == ">") {
        int64_t v = parse_int(term, rhs);
        if (v == p.hi) bad(term, "empty range");
        p.lo = v + 1;
    } else if (op == ">=") {
        p.lo = parse_int(term, rhs);
    } else {
        bad(term, "operator must be one of = < <= > >=");
    }
    return p;
}

}  // namespace

Conjunction parse_conjunction(const std::string& text) {
    Conjunction where;
    std::stringstream ss(text);
    std::string term;
    while (std::getline(ss, term, ',')) where.push_back(parse_term(term));
    if (where.empty() || text.back() == ',')
        throw std::invalid_argument("empty term in predicate list '" + text + "'");
    return where;
}
