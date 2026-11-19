#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "predicate.h"

// Storage block size. Block-based encodings (FOR, delta) keep one header per block of
// kBlock values, and decode() never crosses a block boundary.
constexpr std::size_t kBlock = 1024;

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
//
// Besides decoding, a column can evaluate a predicate and fetch values at given positions
// itself (select / refine / gather). The defaults decode a block at a time and work on
// the decoded values; encodings override them to work on the compressed form directly
// (e.g. dict compares codes, rle tests a whole run at once, bitpack extracts single values).
class EncodedColumn {
public:
    virtual ~EncodedColumn() = default;

    virtual std::size_t size() const = 0;

    // Bytes the encoded data occupies (packed values, dictionaries, per-block headers).
    virtual std::size_t footprint_bytes() const = 0;

    // Vectorized: decodes values [start, start + n) into out with one virtual call.
    // Requires start % 64 == 0 and [start, start + n) inside one block of kBlock values.
    // `out` must have room for n rounded up to a multiple of 64: decoders may write past n.
    virtual void decode(std::size_t start, std::size_t n, int64_t* out) const = 0;

    // Value-at-a-time: a cursor positioned at row 0.
    virtual std::unique_ptr<ColumnCursor> cursor() const = 0;

    // Writes the offsets i (ascending) in [0, n) where value(start + i) passes p to sel and
    // returns how many. Requires start % 64 == 0. `scratch` holds kBlock values.
    virtual std::size_t select(std::size_t start, std::size_t n, const Predicate& p,
                               uint32_t* sel, int64_t* scratch) const;

    // Keeps the offsets in sel[0, cnt) (ascending, relative to start) whose value passes p;
    // returns the new count.
    virtual std::size_t refine(std::size_t start, uint32_t* sel, std::size_t cnt,
                               const Predicate& p, int64_t* scratch) const;

    // out[t * stride] = value(start + sel[t]) for t < cnt; sel ascending.
    virtual void gather(std::size_t start, const uint32_t* sel, std::size_t cnt, int64_t* out,
                        std::size_t stride, int64_t* scratch) const;

    // Sum of values [start, start + n) modulo 2^64 (start % 64 == 0; may span blocks), for
    // aggregation straight on the compressed form. The default decodes a block at a time
    // into scratch (kBlock values); bitpack and for add up the unpacked offsets without
    // storing them (plus n * min), rle multiplies each run's value by its length.
    virtual uint64_t sum(std::size_t start, std::size_t n, int64_t* scratch) const;
};

// The selection step shared by encodings that compare unpacked offsets: appends base + i to
// sel[cnt...] for every i < n with u[i] - lo <= span (unsigned, i.e. lo <= u[i] <= lo + span
// without wrap-around); returns the new count. Branch-free; with bits::simd() it tests 8
// values per step with AVX2 (sel needs room for 8 entries past the result).
std::size_t select_unpacked(const uint64_t* u, std::size_t n, uint64_t lo, uint64_t span,
                            uint32_t base, uint32_t* sel, std::size_t cnt);

// Decodes [start, start + n) (start % 64 == 0) in one decode() call per block it overlaps.
// `out` needs room for n rounded up to a multiple of 64.
void decode_range(const EncodedColumn& col, std::size_t start, std::size_t n, int64_t* out);

enum class Encoding { Plain, Bitpack, For, Dict, DictB, Rle, Delta };

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
std::unique_ptr<EncodedColumn> make_dictb_column(const std::vector<int64_t>& values);
std::unique_ptr<EncodedColumn> make_rle_column(const std::vector<int64_t>& values);
std::unique_ptr<EncodedColumn> make_delta_column(const std::vector<int64_t>& values);
