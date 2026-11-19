#pragma once

#include <vector>

#include "aligned_buffer.h"
#include "query.h"
#include "table.h"

// SELECT c1, c2, ... FROM table [WHERE ...].
// Materialises the qualifying rows' selected columns as row-major tuples into a
// preallocated output buffer (j-th selected column of the t-th output row at
// t * columns.size() + j), so every layout has to reconstruct each tuple. With every
// column selected this is "reconstruct complete qualifying tuples" (SELECT *).
class ProjectionQuery : public Query {
public:
    // Allocates (and touches) the rows x columns.size() output buffer.
    explicit ProjectionQuery(QueryParams p);

    std::string name() const override { return "projection"; }

    void run(const NsmLayout& t) override;
    void run(const DsmLayout& t) override;
    void run(const PaxLayout& t) override;
    void run(const EncodedDsmLayout& t) override;

    // Position-weighted checksum of the output rows, plus their count, so values landing in
    // the wrong row or column are caught, not just wrong values.
    int64_t result() const override;
    // What result() must return for these parameters, computed by a naive row-at-a-time
    // loop over the logical table (used to check the layouts; never timed).
    static int64_t reference(const Table& t, const QueryParams& p);

    void set_batch(std::size_t batch) override;

private:
    // Runs the strategy on one chunk of n rows, writing its qualifying tuples at out;
    // returns how many qualified.
    // chunk(ch, n, out) runs p_.strategy, resolving auto per vector; the overload with a
    // Strategy runs that one (staging the output if asked); chunk_to() is the strategy itself.
    template <class Chunk>
    std::size_t chunk(const Chunk& ch, std::size_t n, int64_t* out);
    template <class Chunk>
    std::size_t chunk(const Chunk& ch, std::size_t n, int64_t* out, Strategy s);
    template <class Chunk>
    std::size_t chunk_to(const Chunk& ch, std::size_t n, int64_t* out, Strategy s);
    void finish(std::size_t total);
    void run_value_mode(const EncodedDsmLayout& t);
    void allocate_vectors();

    std::vector<int> refs_;          // columns the scan reads
    AlignedBuffer<int64_t> out_;     // rows x columns.size()
    AlignedBuffer<uint32_t> sel_;    // selection vector: batch entries
    AlignedBuffer<int64_t> decoded_; // encoded columns: one decoded vector per referenced column
    AlignedBuffer<int64_t> scratch_; // encoded columns: one storage block
    AlignedBuffer<int64_t> stage_;   // stage / nt_stores: one vector of output tuples
};
