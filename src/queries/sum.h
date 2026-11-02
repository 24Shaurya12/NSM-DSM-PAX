#pragma once

#include <vector>

#include "query.h"

// SELECT SUM(c1) + SUM(c2) + ... FROM table, over the given columns.
// Aggregates column by column wherever the layout allows; no tuple is ever reconstructed.
class SumQuery : public Query {
public:
    explicit SumQuery(std::vector<int> columns) : columns_(std::move(columns)) {}

    std::string name() const override { return "sum"; }

    void run(const NsmLayout& t) override;
    void run(const DsmLayout& t) override;
    void run(const PaxLayout& t) override;

    int64_t result() const override { return sum_; }

private:
    std::vector<int> columns_;
    int64_t sum_ = 0;
};
