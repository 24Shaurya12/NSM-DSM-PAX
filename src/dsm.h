#pragma once

#include <vector>

#include "aligned_buffer.h"
#include "layout.h"
#include "table.h"

// Decomposition Storage Model: one contiguous array of `rows` values per column.
// Registered as "dsm" (no storage parameters). Filters run in batches of
// ExecOptions::dsm_batch rows, so the selection vector of a batch stays cache-resident.
class DsmLayout : public Layout {
public:
    explicit DsmLayout(const Table& t);

    std::string name() const override { return "DSM"; }
    std::size_t rows() const override { return rows_; }
    std::size_t cols() const override { return columns_.size(); }
    std::size_t footprint_bytes() const override {
        return columns_.size() * rows_ * sizeof(int64_t);
    }
    bool uses_batch_rows() const override { return true; }

    QueryResult scan_sum(const Projection& project) const override;
    QueryResult filter_sum(const Conjunction& where, const Projection& project,
                           const ExecOptions& opts) const override;
    QueryResult filter_materialize(const Conjunction& where, const Projection& project,
                                   const ExecOptions& opts) const override;
    QueryResult lookup(const RowIds& ids, const Projection& project) const override;
    void flush_cache() const override {
        for (const auto& col : columns_) flush_from_cache(col.data(), rows_ * sizeof(int64_t));
    }

    const int64_t* column(std::size_t c) const { return columns_[c].data(); }

private:
    std::size_t rows_;
    std::vector<AlignedBuffer<int64_t>> columns_;
};
