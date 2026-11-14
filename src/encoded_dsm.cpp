#include "encoded_dsm.h"

#include "query.h"

std::string scan_mode_name(ScanMode m) { return m == ScanMode::Value ? "value" : "vector"; }

bool parse_scan_mode(const std::string& name, ScanMode& out) {
    if (name == "value") out = ScanMode::Value;
    else if (name == "vector") out = ScanMode::Vector;
    else return false;
    return true;
}

EncodedColumns::EncodedColumns(const Table& t, Encoding e) : encoding(e), rows(t.rows) {
    std::vector<int64_t> values(t.rows);
    columns.reserve(t.cols);
    for (std::size_t c = 0; c < t.cols; ++c) {
        for (std::size_t r = 0; r < t.rows; ++r) values[r] = t.at(r, c);
        columns.push_back(encode_column(e, values));
    }
}

EncodedDsmLayout::EncodedDsmLayout(std::shared_ptr<const EncodedColumns> data, ScanMode mode)
    : data_(std::move(data)), mode_(mode) {}

std::string EncodedDsmLayout::name() const {
    return "DSM-" + encoding_name(data_->encoding) + "-" + scan_mode_name(mode_);
}

std::size_t EncodedDsmLayout::footprint_bytes() const {
    std::size_t bytes = 0;
    for (const auto& c : data_->columns) bytes += c->footprint_bytes();
    return bytes;
}

void EncodedDsmLayout::run(Query& q) const { q.run(*this); }
