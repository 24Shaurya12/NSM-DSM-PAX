#pragma once

#include <memory>
#include <vector>

#include "encodings/encoded_column.h"
#include "layout.h"
#include "table.h"

// How a query reads an encoded column.
//   Value:  one ColumnCursor::next() call (virtual) per value.
//   Vector: one EncodedColumn::decode() call (virtual) per kBatch values into a buffer,
//           then a tight loop over the buffer.
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

    Encoding encoding() const { return data_->encoding; }
    ScanMode mode() const { return mode_; }
    const EncodedColumn& column(std::size_t c) const { return *data_->columns[c]; }

private:
    std::shared_ptr<const EncodedColumns> data_;
    ScanMode mode_;
};
