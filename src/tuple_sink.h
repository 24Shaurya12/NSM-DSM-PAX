#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

#include "predicate.h"

// Receives reconstructed tuples (rows of `width` int64 values), as a query's consumer would.
// Tuples are written row-major into a fixed batch buffer that stays cache-resident; whenever it
// fills up, the batch is consumed by folding it into a checksum, then reused. The consumer does
// the same work for every layout, so differences come from how each layout gathers a tuple.
//
// The checksum weights attribute c by (c + 1), so writing a value into the wrong attribute
// changes the result and fails the correctness check.
class TupleSink {
public:
    static constexpr std::size_t kBatchTuples = 1024;

    explicit TupleSink(int width) : width_(width), buf_(kBatchTuples * width) {}

    // Space for the next output tuple.
    int64_t* next() {
        if (n_ == kBatchTuples) consume();
        return buf_.data() + (n_++) * width_;
    }

    // Consumes the remaining tuples and returns {number of tuples, checksum}.
    QueryResult finish() {
        consume();
        return {count_, checksum_};
    }

private:
    void consume() {
        for (std::size_t i = 0; i < n_; ++i) {
            const int64_t* t = buf_.data() + i * width_;
            for (int c = 0; c < width_; ++c) checksum_ += t[c] * (c + 1);
        }
        count_ += static_cast<int64_t>(n_);
        n_ = 0;
    }

    int width_;
    std::vector<int64_t> buf_;
    std::size_t n_ = 0;
    int64_t count_ = 0;
    int64_t checksum_ = 0;
};
