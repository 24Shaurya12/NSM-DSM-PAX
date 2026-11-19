#include "queries/projection.h"

#include <immintrin.h>

#include <algorithm>
#include <cstring>
#include <memory>

#include "chunks.h"
#include "encoded_dsm.h"
#include "kernels.h"

ProjectionQuery::ProjectionQuery(QueryParams p)
    : Query(std::move(p)),
      refs_(referenced_columns()),
      out_(std::max<std::size_t>(p_.rows * p_.columns.size(), 1)),
      scratch_(kBlock) {
    allocate_vectors();
}

void ProjectionQuery::set_batch(std::size_t batch) {
    Query::set_batch(batch);
    allocate_vectors();
}

void ProjectionQuery::allocate_vectors() {
    sel_ = AlignedBuffer<uint32_t>(p_.batch + 8);  // + 8: select_avx2 stores 8 at a time
    // decode() may write up to the next multiple of 64 past a vector, hence the + 64.
    decoded_ = AlignedBuffer<int64_t>(refs_.size() * (p_.batch + 64));
    if (p_.stage || p_.nt_stores) stage_ = AlignedBuffer<int64_t>(p_.batch * p_.columns.size() + 1);
}

template <class Chunk>
std::size_t ProjectionQuery::chunk(const Chunk& ch, std::size_t n, int64_t* out) {
    if (p_.strategy != Strategy::Auto) return chunk(ch, n, out, p_.strategy);
    const std::size_t cnt = chunk(ch, n, out, auto_pick(Strategy::Early));
    auto_observe(cnt, n);
    return cnt;
}

template <class Chunk>
std::size_t ProjectionQuery::chunk(const Chunk& ch, std::size_t n, int64_t* out, Strategy s) {
    if (!p_.stage && !p_.nt_stores) return chunk_to(ch, n, out, s);
    // Build the vector's tuples in the cache-resident stage, then copy them out in order
    // (streaming stores with nt_stores).
    const std::size_t cnt = chunk_to(ch, n, stage_.data(), s);
    if (p_.nt_stores) kern::stream_copy(out, stage_.data(), cnt * p_.columns.size());
    else std::memcpy(out, stage_.data(), cnt * p_.columns.size() * sizeof(int64_t));
    return cnt;
}

template <class Chunk>
std::size_t ProjectionQuery::chunk_to(const Chunk& ch, std::size_t n, int64_t* out, Strategy s) {
    const Predicate* pr = p_.where.data();
    const std::size_t np = p_.where.size();
    const int* cols = p_.columns.data();
    const std::size_t m = p_.columns.size();
    switch (s) {
        case Strategy::Auto:  // resolved by chunk(); not reached
        case Strategy::Early:
            return kern::with_counts(m, np, [&](auto M, auto NP) {
                return kern::early_project<M(), NP()>(ch, pr, np, cols, m, n, out);
            });
        case Strategy::EarlyPipe:
            return kern::with_counts(m, np, [&](auto M, auto NP) {
                return kern::early_pipe_project<M(), NP()>(ch, pr, np, cols, m, n, out);
            });
        case Strategy::Late:
        case Strategy::LateBranch: {
            auto gather = [&](const uint32_t* sel, std::size_t cnt) {
                kern::with_counts(m, 0, [&](auto M, auto) {
                    kern::gather_project<M()>(ch, cols, m, sel, cnt, out);
                });
            };
            if (np == 0) {
                gather(nullptr, n);
                return n;
            }
            uint32_t* sel = sel_.data();
            const std::size_t cnt = s == Strategy::LateBranch
                                        ? kern::select<false>(ch, pr, np, n, sel)
                                    : p_.simd ? kern::select_simd(ch, pr, np, n, sel)
                                              : kern::select<true>(ch, pr, np, n, sel);
            gather(sel, cnt);
            return cnt;
        }
    }
    return 0;
}

void ProjectionQuery::run(const NsmLayout& t) {
    // A "value" is a whole tuple.
    auto_reset(static_cast<double>(t.cols() * sizeof(int64_t)), &t, t.footprint_bytes());
    const std::size_t m = p_.columns.size();
    int64_t* out = out_.data();
    std::size_t total = 0;
    for_each_chunk(t, p_.batch, [&](const kern::RowChunk& ch, std::size_t, std::size_t n) {
        total += chunk(ch, n, out + total * m);
    });
    finish(total);
}

void ProjectionQuery::run(const DsmLayout& t) {
    auto_reset(sizeof(int64_t), &t, t.footprint_bytes());
    const std::size_t m = p_.columns.size();
    int64_t* out = out_.data();
    std::size_t total = 0;
    for_each_chunk(t, refs_, p_.batch,
                   [&](const kern::ColumnChunk& ch, std::size_t, std::size_t n) {
                       total += chunk(ch, n, out + total * m);
                   });
    finish(total);
}

void ProjectionQuery::run(const PaxLayout& t) {
    auto_reset(sizeof(int64_t), &t, t.footprint_bytes());
    const std::size_t m = p_.columns.size();
    int64_t* out = out_.data();
    std::size_t total = 0;
    for_each_chunk(t, refs_, p_.batch,
                   [&](const kern::ColumnChunk& ch, std::size_t, std::size_t n) {
                       total += chunk(ch, n, out + total * m);
                   });
    finish(total);
}

void ProjectionQuery::finish(std::size_t total) {
    if (p_.nt_stores) _mm_sfence();  // order the streaming stores before anything after run()
    rows_out_ = total;
}

void ProjectionQuery::run(const EncodedDsmLayout& t) {
    if (t.mode() == ScanMode::Value) {
        run_value_mode(t);
        return;
    }
    const std::size_t rows = t.rows(), m = p_.columns.size(), batch = p_.batch;
    auto_reset(t.value_bytes(p_.columns), &t, t.footprint_bytes());
    int64_t* out = out_.data();
    std::vector<const int64_t*> ptr(t.cols(), nullptr);
    for (std::size_t i = 0; i < refs_.size(); ++i)
        ptr[refs_[i]] = decoded_.data() + i * (batch + 64);
    uint32_t* sel = sel_.data();
    int64_t* scratch = scratch_.data();
    std::size_t total = 0;

    for (std::size_t s = 0; s < rows; s += batch) {
        const std::size_t n = std::min(batch, rows - s);
        const Strategy st = p_.strategy == Strategy::Auto ? auto_pick(Strategy::Early) : p_.strategy;
        std::size_t cnt;
        if (is_late(st) && !p_.where.empty()) {
            // Late: evaluate the predicates on the encoded predicate columns (select, then
            // refine for each further predicate), then fetch only the selected values of
            // each output column straight from its encoded form into the output tuples.
            const Predicate& first = p_.where[0];
            cnt = t.column(first.col).select(s, n, first, sel, scratch);
            for (std::size_t q = 1; q < p_.where.size() && cnt > 0; ++q)
                cnt = t.column(p_.where[q].col).refine(s, sel, cnt, p_.where[q], scratch);
            int64_t* dst = out + total * m;
            for (std::size_t j = 0; j < m; ++j)
                t.column(p_.columns[j]).gather(s, sel, cnt, dst + j, m, scratch);
        } else {
            // Decompress every referenced column of the vector, then run the uncompressed
            // kernel on the decoded values. Without a WHERE clause, late and early differ
            // only in how the decoded vectors are stitched (column vs tuple at a time).
            for (std::size_t i = 0; i < refs_.size(); ++i)
                decode_range(t.column(refs_[i]), s, n, decoded_.data() + i * (batch + 64));
            cnt = chunk(kern::ColumnChunk{ptr.data()}, n, out + total * m, st);
        }
        if (p_.strategy == Strategy::Auto) auto_observe(cnt, n);
        total += cnt;
    }
    finish(total);
}

void ProjectionQuery::run_value_mode(const EncodedDsmLayout& t) {
    // Tuple at a time: every row pulls one value from each referenced column's cursor (a
    // virtual call each), then the predicates are tested on the stitched tuple.
    const std::size_t rows = t.rows(), m = p_.columns.size();
    std::vector<std::unique_ptr<ColumnCursor>> cursors;
    for (int c : refs_) cursors.push_back(t.column(c).cursor());
    std::vector<int64_t> tuple(t.cols());
    int64_t* out = out_.data();
    std::size_t total = 0;
    for (std::size_t r = 0; r < rows; ++r) {
        for (std::size_t i = 0; i < refs_.size(); ++i) tuple[refs_[i]] = cursors[i]->next();
        bool ok = true;
        for (const Predicate& pr : p_.where) ok &= pr.test(tuple[pr.col]);
        int64_t* dst = out + total * m;
        for (std::size_t j = 0; j < m; ++j) dst[j] = tuple[p_.columns[j]];
        total += ok;
    }
    rows_out_ = total;
}

int64_t ProjectionQuery::result() const {
    // Unsigned arithmetic so overflow wraps instead of being undefined.
    uint64_t checksum = rows_out_;
    const std::size_t n = rows_out_ * p_.columns.size();
    for (std::size_t i = 0; i < n; ++i) checksum += static_cast<uint64_t>(out_[i]) * (i + 1);
    return static_cast<int64_t>(checksum);
}

int64_t ProjectionQuery::reference(const Table& t, const QueryParams& p) {
    uint64_t checksum = 0;
    std::size_t count = 0, i = 0;
    for (std::size_t r = 0; r < t.rows; ++r) {
        bool ok = true;
        for (const Predicate& pr : p.where) {
            const int64_t v = t.at(r, pr.col);
            ok = ok && v >= pr.lo && v <= pr.hi;
        }
        if (!ok) continue;
        for (int c : p.columns) checksum += static_cast<uint64_t>(t.at(r, c)) * ++i;
        ++count;
    }
    return static_cast<int64_t>(checksum + count);
}
