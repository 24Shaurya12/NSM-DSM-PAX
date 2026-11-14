#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

// Values per vectorized batch. Block-based encodings (FOR, delta) also use blocks of
// kBatch values, and vectorized scans always start a batch at a multiple of kBatch, so a
// batch is always exactly one block.
constexpr std::size_t kBatch = 1024;

// Value-at-a-time reader over one column: each next() decodes and returns one value.
// It is a virtual call per value, like a tuple-at-a-time (Volcano) scan operator that
// does not know the encoding at compile time.
class ColumnCursor {
public:
    virtual ~ColumnCursor() = default;
    // Must be called at most size() times.
    virtual int64_t next() = 0;
};

// One encoded int64 column.
class EncodedColumn {
public:
    virtual ~EncodedColumn() = default;

    virtual std::size_t size() const = 0;

    // Bytes the encoded data occupies (packed values, dictionaries, per-block headers).
    virtual std::size_t footprint_bytes() const = 0;

    // Vectorized: decodes values [start, start + n) into out with one virtual call.
    // Requires start % kBatch == 0 and n <= kBatch. `out` must have room for kBatch
    // values: decoders may write past n, up to the next multiple of 64.
    virtual void decode(std::size_t start, std::size_t n, int64_t* out) const = 0;

    // Value-at-a-time: a cursor positioned at row 0.
    virtual std::unique_ptr<ColumnCursor> cursor() const = 0;
};

enum class Encoding { Plain, Bitpack, For, Dict, Rle, Delta };

const std::vector<Encoding>& all_encodings();
std::string encoding_name(Encoding e);
// Returns false if `name` is not an encoding.
bool parse_encoding(const std::string& name, Encoding& out);

std::unique_ptr<EncodedColumn> encode_column(Encoding e, const std::vector<int64_t>& values);

// One factory per encoding, each in src/encodings/<name>.cpp.
std::unique_ptr<EncodedColumn> make_plain_column(const std::vector<int64_t>& values);
std::unique_ptr<EncodedColumn> make_bitpack_column(const std::vector<int64_t>& values);
std::unique_ptr<EncodedColumn> make_for_column(const std::vector<int64_t>& values);
std::unique_ptr<EncodedColumn> make_dict_column(const std::vector<int64_t>& values);
std::unique_ptr<EncodedColumn> make_rle_column(const std::vector<int64_t>& values);
std::unique_ptr<EncodedColumn> make_delta_column(const std::vector<int64_t>& values);
