#pragma once

#include <memory>
#include <vector>

#include "encodings/encoded_column.h"
#include "layout.h"
#include "table.h"

// How a query reads an encoded column.
//   Value:  one ColumnCursor::next() call (virtual) per value. Tuple at a time, so only the
//           `early` strategy is supported.
//   Vector: one virtual call per vector of `batch` values (at most one storage block per
//           call): decode() into a buffer followed by a tight loop over it (early
//           strategies), or select() / refine() / gather() on the encoded data (late).
enum class ScanMode { Value, Vector };

std::string scan_mode_name(ScanMode m);
bool parse_scan_mode(const std::string& name, ScanMode& out);

// All columns of a table in one encoding. Shared by the Value and Vector layouts so the
// data is encoded (and held in memory) only once per encoding.
struct EncodedColumns {
    EncodedColumns(const Table& t, Encoding e);

    Encoding encoding;
    std::size_t rows;
    std::vector<std::unique_ptr<EncodedColumn>> columns;
};

// DSM (one storage unit per column) with every column encoded, read in one scan mode.
// Named DSM-<encoding>-<mode>, e.g. DSM-dict-vector.
class EncodedDsmLayout : public Layout {
public:
    EncodedDsmLayout(std::shared_ptr<const EncodedColumns> data, ScanMode mode);

    std::string name() const override;
    std::size_t rows() const override { return data_->rows; }
    std::size_t cols() const override { return data_->columns.size(); }
    std::size_t footprint_bytes() const override;

    void run(Query& q) const override;

    // Value mode reads every column front to back, one value per call, so it can only stitch
    // tuples (early). The late strategy has one branch-free variant for encoded columns.
    bool supports(Strategy s) const override {
        return mode_ == ScanMode::Value ? s == Strategy::Early : s != Strategy::LateBranch;
    }

    Encoding encoding() const { return data_->encoding; }
    ScanMode mode() const { return mode_; }
    const EncodedColumn& column(std::size_t c) const { return *data_->columns[c]; }

    // Average encoded bytes per value over the given columns (for the auto strategy).
    double value_bytes(const std::vector<int>& cols) const {
        double bytes = 0;
        for (int c : cols) bytes += static_cast<double>(data_->columns[c]->footprint_bytes());
        return cols.empty() ? 8.0 : bytes / (static_cast<double>(rows()) * cols.size());
    }

private:
    std::shared_ptr<const EncodedColumns> data_;
    ScanMode mode_;
};
