// rle (run-length encoding): one (value, run end) pair per run of equal consecutive
// values. Run ends are cumulative, so the run holding any row can be binary-searched.
// Compresses well only when values repeat in long runs.
#include <immintrin.h>

#include <algorithm>

#include "encodings/bits.h"
#include "encodings/encoded_column.h"

namespace {

// Sum of values[r] * (ends[r] - ends[r - 1]) for r in [a, b), a >= 1: the runs that lie
// entirely inside the summed range.
uint64_t sum_whole_runs(const int64_t* values, const uint64_t* ends, std::size_t a,
                        std::size_t b) {
    uint64_t total = 0;
    for (std::size_t r = a; r < b; ++r)
        total += static_cast<uint64_t>(values[r]) * (ends[r] - ends[r - 1]);
    return total;
}

// The same with AVX2, 4 runs per step. AVX2 has no 64 x 64-bit multiply (that is AVX-512's
// vpmullq), only vpmuludq, which multiplies the low 32 bits of each 64-bit lane into a 64-bit
// product; so this is exact only when every value and every run length is below 2^32, which
// the column checks once when it is built.
__attribute__((target("avx2"))) uint64_t sum_whole_runs_avx2(const int64_t* values,
                                                             const uint64_t* ends,
                                                             std::size_t a, std::size_t b) {
    __m256i acc0 = _mm256_setzero_si256(), acc1 = _mm256_setzero_si256();
    std::size_t r = a;
    for (; r + 8 <= b; r += 8) {
        const auto ld = [](const void* p) {
            return _mm256_loadu_si256(static_cast<const __m256i*>(p));
        };
        const __m256i len0 = _mm256_sub_epi64(ld(ends + r), ld(ends + r - 1));
        const __m256i len1 = _mm256_sub_epi64(ld(ends + r + 4), ld(ends + r + 3));
        acc0 = _mm256_add_epi64(acc0, _mm256_mul_epu32(ld(values + r), len0));
        acc1 = _mm256_add_epi64(acc1, _mm256_mul_epu32(ld(values + r + 4), len1));
    }
    alignas(32) uint64_t lanes[4];
    _mm256_store_si256(reinterpret_cast<__m256i*>(lanes), _mm256_add_epi64(acc0, acc1));
    return lanes[0] + lanes[1] + lanes[2] + lanes[3] + sum_whole_runs(values, ends, r, b);
}

class RleCursor final : public ColumnCursor {
public:
    RleCursor(const int64_t* values, const uint64_t* ends)
        : values_(values), ends_(ends), left_(ends[0]) {}
    int64_t next() override {
        if (left_ == 0) {
            ++run_;
            left_ = ends_[run_] - ends_[run_ - 1];
        }
        --left_;
        return values_[run_];
    }

private:
    const int64_t* values_;
    const uint64_t* ends_;
    std::size_t run_ = 0;
    uint64_t left_;  // rows still to return from the current run
};

class RleColumn final : public EncodedColumn {
public:
    explicit RleColumn(const std::vector<int64_t>& values) : n_(values.size()) {
        for (std::size_t i = 0; i < values.size(); ++i) {
            if (i == 0 || values[i] != values[i - 1]) {
                values_.push_back(values[i]);
                ends_.push_back(i + 1);
            } else {
                ends_.back() = i + 1;
            }
        }
        narrow_ = true;
        for (std::size_t r = 0; r < values_.size(); ++r)
            narrow_ &= values_[r] >= 0 && values_[r] <= int64_t{0xFFFFFFFF} &&
                       ends_[r] - (r ? ends_[r - 1] : 0) <= 0xFFFFFFFFu;
    }

    std::size_t size() const override { return n_; }
    std::size_t footprint_bytes() const override {
        return values_.size() * (sizeof(int64_t) + sizeof(uint64_t));
    }

    void decode(std::size_t start, std::size_t n, int64_t* out) const override {
        std::size_t run = first_run(start);
        std::size_t pos = 0;
        while (pos < n) {
            const std::size_t stop = std::min<std::size_t>(ends_[run] - start, n);
            std::fill(out + pos, out + stop, values_[run]);
            pos = stop;
            ++run;
        }
        hint_ = run - 1;
    }

    std::unique_ptr<ColumnCursor> cursor() const override {
        return std::make_unique<RleCursor>(values_.data(), ends_.data());
    }

    // One predicate test per run: a qualifying run selects all of its rows in the range.
    std::size_t select(std::size_t start, std::size_t n, const Predicate& p, uint32_t* sel,
                       int64_t*) const override {
        std::size_t run = first_run(start);
        std::size_t cnt = 0;
        for (std::size_t pos = 0; pos < n; ++run) {
            const std::size_t stop = std::min<std::size_t>(ends_[run] - start, n);
            if (p.test(values_[run]))
                for (std::size_t i = pos; i < stop; ++i) sel[cnt++] = static_cast<uint32_t>(i);
            pos = stop;
        }
        hint_ = run - 1;
        return cnt;
    }

    std::size_t refine(std::size_t start, uint32_t* sel, std::size_t cnt, const Predicate& p,
                       int64_t*) const override {
        if (cnt == 0) return 0;
        std::size_t run = first_run(start + sel[0]);
        std::size_t kept = 0;
        for (std::size_t t = 0; t < cnt; ++t) {
            const uint32_t pos = sel[t];
            while (ends_[run] <= start + pos) ++run;
            sel[kept] = pos;
            kept += p.test(values_[run]);
        }
        hint_ = run;
        return kept;
    }

    void gather(std::size_t start, const uint32_t* sel, std::size_t cnt, int64_t* out,
                std::size_t stride, int64_t*) const override {
        if (cnt == 0) return;
        std::size_t run = first_run(start + sel[0]);
        for (std::size_t t = 0; t < cnt; ++t) {
            while (ends_[run] <= start + sel[t]) ++run;
            out[t * stride] = values_[run];
        }
        hint_ = run;
    }

    // Each run contributes value x (rows of it inside the range); nothing is expanded. The
    // first and last run may be cut by the range; the runs between them are whole, and are
    // summed by a loop that takes each length from the run ends (with --simd, 4 per step).
    uint64_t sum(std::size_t start, std::size_t n, int64_t*) const override {
        if (n == 0) return 0;
        const std::size_t end = start + n;
        const std::size_t first = first_run(start);
        if (ends_[first] >= end) return static_cast<uint64_t>(values_[first]) * n;
        // The run holding end - 1: a few runs after `first` for a vector, the last run for a
        // whole column.
        std::size_t last = first + 1;
        while (last < first + kHintScan && ends_[last] < end) ++last;
        if (ends_[last] < end)
            last = std::upper_bound(ends_.begin() + last, ends_.end(), end - 1) - ends_.begin();
        hint_ = last;
        const uint64_t cut = static_cast<uint64_t>(values_[first]) * (ends_[first] - start) +
                             static_cast<uint64_t>(values_[last]) * (end - ends_[last - 1]);
        const bool simd = bits::simd() && narrow_ && last - first > 16;
        return cut + (simd ? sum_whole_runs_avx2 : sum_whole_runs)(values_.data(), ends_.data(),
                                                                   first + 1, last);
    }

private:
    // The run holding `row`. Scans call this once per vector with rows in increasing order,
    // and every function that walks runs leaves hint_ at the last run it touched, so the run
    // is usually that one or the next: check a few from there first and fall back to a
    // binary search. The search over all run ends (about
    // 500 KB per column at 64 rows per run, so L2 misses) was 60% of SUM's time.
    // Not thread-safe; the engine runs one scan at a time.
    std::size_t first_run(std::size_t row) const {
        std::size_t r = hint_;
        if (r < ends_.size() && (r == 0 || ends_[r - 1] <= row)) {
            for (const std::size_t stop = std::min(r + kHintScan, ends_.size()); r < stop; ++r)
                if (ends_[r] > row) return hint_ = r;
        }
        return hint_ = std::upper_bound(ends_.begin(), ends_.end(), row) - ends_.begin();
    }

    static constexpr std::size_t kHintScan = 8;
    mutable std::size_t hint_ = 0;

    std::size_t n_;
    bool narrow_ = false;  // every value and run length < 2^32 (see sum_whole_runs_avx2)
    std::vector<int64_t> values_;
    std::vector<uint64_t> ends_;  // ends_[r] = one past the last row of run r
};

}  // namespace

std::unique_ptr<EncodedColumn> make_rle_column(const std::vector<int64_t>& values) {
    return std::make_unique<RleColumn>(values);
}
