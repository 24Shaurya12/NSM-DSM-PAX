#include "query_spec.h"

#include <stdexcept>

#include "util.h"

const char* op_name(Op op) {
    switch (op) {
        case Op::Sum: return "sum";
        case Op::Materialize: return "materialize";
        case Op::Lookup: return "lookup";
    }
    return "?";
}

namespace {

Op parse_op(const std::string& s) {
    if (s == "sum") return Op::Sum;
    if (s == "materialize") return Op::Materialize;
    if (s == "lookup") return Op::Lookup;
    throw std::invalid_argument("unknown op '" + s + "' (sum, materialize, lookup)");
}

int parse_col(std::string s, int table_cols) {
    if (!s.empty() && s[0] == 'c') s = s.substr(1);
    long long c = parse_int(s, "column");
    if (c < 0 || c >= table_cols)
        throw std::invalid_argument("column c" + s + " outside c0..c" +
                                    std::to_string(table_cols - 1));
    return static_cast<int>(c);
}

}  // namespace

Projection parse_projection(const std::string& text, int table_cols) {
    if (text == "all") return prefix_projection(table_cols);
    Projection p;
    for (const std::string& item : split(text, ',')) {
        if (item.empty()) throw std::invalid_argument("empty column in '" + text + "'");
        std::size_t dash = item.find('-');
        if (dash == std::string::npos) {
            p.cols.push_back(parse_col(item, table_cols));
            continue;
        }
        int a = parse_col(item.substr(0, dash), table_cols);
        int b = parse_col(item.substr(dash + 1), table_cols);
        if (a > b) throw std::invalid_argument("empty column range '" + item + "'");
        for (int c = a; c <= b; ++c) p.cols.push_back(c);
    }
    if (p.cols.empty()) throw std::invalid_argument("no columns in '" + text + "'");
    return p;
}

void parse_where(const std::string& text, int table_cols, QuerySpec& q) {
    q.where.clear();
    q.where_label = "none";
    if (text == "none" || text.empty()) return;
    q.where = parse_conjunction(text);
    for (const Predicate& p : q.where)
        if (p.col < 0 || p.col >= table_cols)
            throw std::invalid_argument("predicate '" + text + "' uses c" +
                                        std::to_string(p.col) + ", but the table has c0..c" +
                                        std::to_string(table_cols - 1));
    q.where_label.clear();
    for (char ch : text) q.where_label += (ch == ',') ? std::string(" & ") : std::string(1, ch);
}

std::string QuerySpec::label() const {
    if (!custom_label.empty()) return custom_label;
    std::string s = std::string(op_name(op)) + "|" + project.label();
    if (op == Op::Lookup) return s + "|n=" + std::to_string(lookups.count) + "|" + lookups.dist;
    return s + "|" + where_label;
}

QuerySpec parse_query_spec(const std::string& text, int table_cols) {
    QuerySpec q;
    bool have_cols = false;
    for (const std::string& kv : split(text, ';')) {
        if (kv.empty()) continue;
        std::size_t eq = kv.find('=');
        if (eq == std::string::npos)
            throw std::invalid_argument("expected key=value in query '" + text + "', got '" +
                                        kv + "'");
        std::string key = kv.substr(0, eq), val = kv.substr(eq + 1);
        if (key == "op") q.op = parse_op(val);
        else if (key == "cols") q.project = parse_projection(val, table_cols), have_cols = true;
        else if (key == "where") parse_where(val, table_cols, q);
        else if (key == "label") {
            if (val.find(',') != std::string::npos)
                throw std::invalid_argument("label must not contain ','");
            q.custom_label = val;
        } else if (key == "n") q.lookups.count = static_cast<std::size_t>(parse_int(val, "n"));
        else if (key == "dist") q.lookups.dist = val;
        else if (key == "seed") q.lookups.seed = static_cast<uint64_t>(parse_int(val, "seed"));
        else throw std::invalid_argument("unknown query key '" + key + "' in '" + text + "'");
    }
    if (!have_cols) q.project = prefix_projection(table_cols);
    if (q.op == Op::Lookup && !q.where.empty())
        throw std::invalid_argument("op=lookup takes no where clause");
    if (q.op == Op::Lookup && q.lookups.count == 0)
        throw std::invalid_argument("op=lookup needs n=<number of lookups> >= 1");
    return q;
}

RowIds lookup_ids(const QuerySpec& q, std::size_t rows) {
    return make_row_ids(q.lookups.dist, rows, q.lookups.count, q.lookups.seed);
}
