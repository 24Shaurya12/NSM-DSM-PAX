#pragma once

#include <vector>

#include "aligned_buffer.h"
#include "query.h"
#include "table.h"

// SELECT SUM(c1) + SUM(c2) + ..., COUNT(*) FROM table [WHERE ...], over the given columns
// (AVG is SUM / COUNT). No output tuples are built: late strategies aggregate one column
// at a time, early ones one row at a time.
// The sum wraps modulo 2^64 (unsigned arithmetic), so full-range data cannot overflow.
class SumQuery : public Query {
public:
    explicit SumQuery(QueryParams p);

    std::string name() const override { return "sum"; }

    void run(const NsmLayout& t) override;
    void run(const DsmLayout& t) override;
    void run(const PaxLayout& t) override;
    void run(const EncodedDsmLayout& t) override;

    // Combines SUM and COUNT, so both have to match across layouts.
    int64_t result() const override { return combine(sum_, rows_out_); }
    // What result() must return for these parameters, computed by a naive row-at-a-time
    // loop over the logical table (used to check the layouts; never timed).
    static int64_t reference(const Table& t, const QueryParams& p);

    void set_batch(std::size_t batch) override;

private:
    static int64_t combine(uint64_t sum, std::size_t count) {
        return static_cast<int64_t>(sum + static_cast<uint64_t>(count) * 0x9e3779b97f4a7c15ULL);
    }

    struct Partial {
        uint64_t sum;
        std::size_t count;
    };
    // chunk(ch, n) runs p_.strategy, resolving auto per vector; the overload with a
    // Strategy runs that one.
    template <class Chunk>
    Partial chunk(const Chunk& ch, std::size_t n);
    template <class Chunk>
    Partial chunk(const Chunk& ch, std::size_t n, Strategy s);
    void run_value_mode(const EncodedDsmLayout& t);
    void allocate_vectors();

    std::vector<int> refs_;
    AlignedBuffer<uint32_t> sel_;     // selection vector: batch entries
    AlignedBuffer<int64_t> decoded_;  // encoded columns: one decoded vector per referenced column
    AlignedBuffer<int64_t> scratch_;  // encoded columns: one storage block
    uint64_t sum_ = 0;
};
