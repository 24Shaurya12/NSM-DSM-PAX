#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "predicate.h"

class NsmLayout;
class DsmLayout;
class PaxLayout;
class EncodedDsmLayout;

// How a query with a WHERE clause turns qualifying rows into output (see README, "Execution
// strategies"). Without a WHERE clause the two early strategies are the same, and so are
// the two late ones.
//
//   early        Early materialization, tuple at a time. For each row, read every referenced
//                attribute (stitch the tuple), then test the predicates on it. The output
//                is written unconditionally and the output cursor only advances when the row
//                qualifies, so there is no branch on the predicate.
//   early-pipe   Early materialization, pipelined: test the predicate attributes first, and
//                fetch the other attributes only when the row qualifies (a branch per row).
//   late         Late materialization, a vector of `batch` rows at a time. The predicate
//                columns produce a selection vector (positions of qualifying rows, built
//                without branches); then each output column is read only at those positions
//                and written into the output tuples (column at a time).
//   late-branch  Same as late, but the selection vector is built with a branch per row.
//   auto         Chooses early or late for every span of at least 1024 rows (a vector, or
//                several PAX pages), from the selectivity of the span before it and a
//                threshold derived from the cache line size
//                (late_threshold() in machine.h). Without a WHERE clause: late for SUM,
//                early for projection.
enum class Strategy { Early, EarlyPipe, Late, LateBranch, Auto };

const std::vector<Strategy>& all_strategies();
std::string strategy_name(Strategy s);
bool parse_strategy(const std::string& name, Strategy& out);
inline bool is_late(Strategy s) { return s == Strategy::Late || s == Strategy::LateBranch; }

struct QueryParams {
    std::vector<int> columns;      // the SELECT list, or the columns SUM adds up
    std::vector<Predicate> where;  // conjunction; empty = no WHERE clause
    Strategy strategy = Strategy::Late;
    std::size_t batch = 1024;  // rows per vector (late strategies, encoded columns); multiple
                               // of 64. PAX processes each page in pieces of at most this.
    std::size_t rows = 0;      // table rows, to size output buffers
    std::size_t cols = 0;      // table columns
    // Tuning knobs (off by default):
    bool simd = false;        // late: build the selection vector with AVX2 (select_avx2)
    bool stage = false;       // projection: materialise each vector's tuples in a cache-
                              // resident buffer, then copy them to the output sequentially
    bool nt_stores = false;   // ... and copy with non-temporal stores (stream_copy); implies stage
    double auto_phi = 0.55;   // auto: see late_threshold() in machine.h
    double auto_sigma = 2.0;  // auto: fixed cost of late, in column scans (late_threshold())
    // auto: separate constants for tables that fit in L3 (fitted on the 4 MB in-cache runs),
    // used instead of the two above when auto_cache_aware is set.
    bool auto_cache_aware = false;
    double auto_phi_cache = 1.0;
    double auto_sigma_cache = 2.0;
    bool auto_measure = false;  // auto: choose by measured cost per row (see Query::auto_pick)
};

// A query has one run() overload per layout, so all of a query's layout-specific code
// lives together in its own file under src/queries/. Layouts only expose their storage.
//
// Anything a query needs besides the scan itself (e.g. allocating an output buffer)
// belongs in its constructor, which the driver calls outside the timed region.
class Query {
public:
    explicit Query(QueryParams p) : p_(std::move(p)) {}
    virtual ~Query() = default;

    virtual std::string name() const = 0;

    virtual void run(const NsmLayout& t) = 0;
    virtual void run(const DsmLayout& t) = 0;
    virtual void run(const PaxLayout& t) = 0;
    virtual void run(const EncodedDsmLayout& t) = 0;

    // Value summarising the last run's output; must be identical across layouts and
    // strategies. Called outside the timed region (it may read the whole output).
    virtual int64_t result() const = 0;

    // Rows that qualified in the last run.
    std::size_t rows_out() const { return rows_out_; }

    const QueryParams& params() const { return p_; }
    void set_strategy(Strategy s) { p_.strategy = s; }
    // Changes the vector size; may reallocate per-vector buffers (call outside timing).
    virtual void set_batch(std::size_t batch) { p_.batch = batch; }

protected:
    // Columns the scan has to read: the SELECT/SUM list plus the predicate columns, each once.
    std::vector<int> referenced_columns() const;

    // The auto strategy. auto_reset() starts a run: the threshold comes from the cache line
    // size and the bytes one value takes in this layout (late_threshold() in machine.h). With
    // auto_cache_aware, a table of at most the L3 size (footprint) uses the in-cache constants.
    // auto_pick() is the strategy for the next vector or page (`no_where` when there is no
    // WHERE clause); auto_observe() records how many of its rows qualified. A decision holds
    // for a span of at least kAutoSpan rows: a 128-row PAX page at 1% holds 0 to 4 qualifying
    // rows, too few to estimate the selectivity from, and deciding (and timing) every page
    // costs about 1 cycle per row.
    //
    // With auto_measure, the threshold is only the starting point. Every span is timed
    // (rdtsc), and each strategy keeps a moving average of its cycles per row, separately
    // for each power-of-two range of the previous span's selectivity. Once both have been
    // measured in a range, the cheaper one runs, and the other one runs once every
    // kAutoExplore x (its cost / the cheaper one's) spans to refresh its estimate. This
    // adapts to what the fitted constants of the
    // threshold cannot know: the layout (PAX's early is slower than DSM's), and whether the
    // data comes from cache or DRAM. The estimates persist across runs of the same query on the
    // same layout.
    void auto_reset(double value_bytes, const void* layout,  // layout: whose costs to keep
                    std::size_t footprint);
    Strategy auto_pick(Strategy no_where) const;
    void auto_observe(std::size_t cnt, std::size_t n) const;

    QueryParams p_;
    std::size_t rows_out_ = 0;
    double auto_threshold_ = 0;  // late below this selectivity
    mutable double auto_sel_ = 0;  // selectivity of the previous span (0: start with late)
    static constexpr std::size_t kAutoSpan = 1024;  // rows a decision holds for, at least
    mutable Strategy auto_cur_ = Strategy::Late;    // the current span's strategy
    mutable std::size_t auto_span_rows_ = 0, auto_span_cnt_ = 0;  // rows and hits so far

    // auto_measure state: [selectivity range][0 early, 1 late].
    struct AutoArm {
        double cycles_per_row = 0;
        std::uint32_t samples = 0;
    };
    static constexpr int kAutoRanges = 16;  // 0: s = 0; r: s in [2^(r-16), 2^(r-15))
    static constexpr std::uint32_t kAutoExplore = 32;
    mutable AutoArm auto_arms_[kAutoRanges][2] = {};
    mutable int auto_range_ = 0, auto_arm_ = -1;  // what the current span runs (-1: untimed)
    mutable std::uint64_t auto_t0_ = 0;
    mutable std::uint32_t auto_since_explore_[kAutoRanges] = {};
    static constexpr std::size_t kAutoWindow = 2048;  // rows the selectivity is smoothed over
    mutable bool auto_first_ = true;
    const void* auto_layout_ = nullptr;  // the layout the measured costs belong to
};
