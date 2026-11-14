#pragma once

#include <vector>

#include "aligned_buffer.h"
#include "encodings/encoded_column.h"
#include "query.h"

// SELECT SUM(c1) + SUM(c2) + ... FROM table, over the given columns.
// Aggregates column by column wherever the layout allows; no tuple is ever reconstructed.
// The sum wraps modulo 2^64 (unsigned arithmetic), so full-range data cannot overflow.
class SumQuery : public Query {
public:
    explicit SumQuery(std::vector<int> columns) : columns_(std::move(columns)), batch_(kBatch) {}

    std::string name() const override { return "sum"; }

    void run(const NsmLayout& t) override;
    void run(const DsmLayout& t) override;
    void run(const PaxLayout& t) override;
    void run(const EncodedDsmLayout& t) override;

    int64_t result() const override { return sum_; }

private:
    std::vector<int> columns_;
    AlignedBuffer<int64_t> batch_;  // decode buffer for vectorized scans
    int64_t sum_ = 0;
};
