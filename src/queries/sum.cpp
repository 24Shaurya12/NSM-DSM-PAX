#include "queries/sum.h"

#include <algorithm>
#include <memory>

#include "chunks.h"
#include "encoded_dsm.h"
#include "kernels.h"

SumQuery::SumQuery(QueryParams p) : Query(std::move(p)), refs_(referenced_columns()), scratch_(kBlock) {
    allocate_vectors();
}

void SumQuery::set_batch(std::size_t batch) {
    Query::set_batch(batch);
    allocate_vectors();
}

void SumQuery::allocate_vectors() {
    sel_ = AlignedBuffer<uint32_t>(p_.batch + 8);  // + 8: select_avx2 stores 8 at a time
    decoded_ = AlignedBuffer<int64_t>(refs_.size() * (p_.batch + 64));
}

template <class Chunk>
SumQuery::Partial SumQuery::chunk(const Chunk& ch, std::size_t n) {
    if (p_.strategy != Strategy::Auto) return chunk(ch, n, p_.strategy);
    const Partial r = chunk(ch, n, auto_pick(Strategy::Late));
    auto_observe(r.count, n);
    return r;
}

template <class Chunk>
SumQuery::Partial SumQuery::chunk(const Chunk& ch, std::size_t n, Strategy s) {
    const Predicate* pr = p_.where.data();
    const std::size_t np = p_.where.size();
    const int* cols = p_.columns.data();
    const std::size_t m = p_.columns.size();
    switch (s) {
        case Strategy::Auto:  // resolved by chunk(ch, n); not reached
        case Strategy::Early: {
            auto r = kern::with_counts(m, np, [&](auto M, auto NP) {
                return kern::early_sum<M(), NP()>(ch, pr, np, cols, m, n);
            });
            return {r.sum, r.count};
        }
        case Strategy::EarlyPipe: {
            auto r = kern::with_counts(m, np, [&](auto M, auto NP) {
                return kern::early_pipe_sum<M(), NP()>(ch, pr, np, cols, m, n);
            });
            return {r.sum, r.count};
        }
        case Strategy::Late:
        case Strategy::LateBranch: {
            auto gather = [&](const uint32_t* sel, std::size_t cnt) {
                return kern::with_counts(m, 0, [&](auto M, auto) {
                    return kern::gather_sum<M()>(ch, cols, m, sel, cnt);
                });
            };
            if (np == 0) return {gather(nullptr, n), n};
            uint32_t* sel = sel_.data();
            const std::size_t cnt = s == Strategy::LateBranch
                                        ? kern::select<false>(ch, pr, np, n, sel)
                                    : p_.simd ? kern::select_simd(ch, pr, np, n, sel)
                                              : kern::select<true>(ch, pr, np, n, sel);
            return {gather(sel, cnt), cnt};
        }
    }
    return {0, 0};
}

void SumQuery::run(const NsmLayout& t) {
    // A "value" is a whole tuple.
    auto_reset(static_cast<double>(t.cols() * sizeof(int64_t)), &t, t.footprint_bytes());
    uint64_t sum = 0;
    std::size_t count = 0;
    for_each_chunk(t, p_.batch, [&](const kern::RowChunk& ch, std::size_t, std::size_t n) {
        Partial r = chunk(ch, n);
        sum += r.sum;
        count += r.count;
    });
    sum_ = sum;
    rows_out_ = count;
}

void SumQuery::run(const DsmLayout& t) {
    auto_reset(sizeof(int64_t), &t, t.footprint_bytes());
    uint64_t sum = 0;
    std::size_t count = 0;
    for_each_chunk(t, refs_, p_.batch,
                   [&](const kern::ColumnChunk& ch, std::size_t, std::size_t n) {
                       Partial r = chunk(ch, n);
                       sum += r.sum;
                       count += r.count;
                   });
    sum_ = sum;
    rows_out_ = count;
}

void SumQuery::run(const PaxLayout& t) {
    auto_reset(sizeof(int64_t), &t, t.footprint_bytes());
    uint64_t sum = 0;
    std::size_t count = 0;
    for_each_chunk(t, refs_, p_.batch,
                   [&](const kern::ColumnChunk& ch, std::size_t, std::size_t n) {
                       Partial r = chunk(ch, n);
                       sum += r.sum;
                       count += r.count;
                   });
    sum_ = sum;
    rows_out_ = count;
}

void SumQuery::run(const EncodedDsmLayout& t) {
    if (t.mode() == ScanMode::Value) {
        run_value_mode(t);
        return;
    }
    const std::size_t rows = t.rows(), batch = p_.batch;
    auto_reset(t.value_bytes(p_.columns), &t, t.footprint_bytes());
    std::vector<const int64_t*> ptr(t.cols(), nullptr);
    for (std::size_t i = 0; i < refs_.size(); ++i)
        ptr[refs_[i]] = decoded_.data() + i * (batch + 64);
    uint32_t* sel = sel_.data();
    int64_t* scratch = scratch_.data();
    int64_t* vals = decoded_.data();
    uint64_t sum = 0;
    std::size_t count = 0;

    // Late without a WHERE clause, for the whole scan: nothing is decoded or selected, so
    // vectors have no purpose; one EncodedColumn::sum call per column instead. This saves a
    // virtual call and a search for the start (rle) per vector and column: rle SUM has about
    // 16 runs per 1024-row vector, so that overhead was a large share of its time. Auto keeps
    // its vectors, since measuring needs them.
    const bool whole_late = p_.where.empty() &&
        (p_.strategy == Strategy::Late || (p_.strategy == Strategy::Auto && !p_.auto_measure));
    if (whole_late) {
        for (int c : p_.columns) sum += t.column(c).sum(0, rows, scratch);
        sum_ = sum;
        rows_out_ = rows;
        return;
    }

    for (std::size_t s = 0; s < rows; s += batch) {
        const std::size_t n = std::min(batch, rows - s);
        const Strategy st = p_.strategy == Strategy::Auto ? auto_pick(Strategy::Late) : p_.strategy;
        std::size_t cnt;
        if (is_late(st) && !p_.where.empty()) {
            // Late: predicates on the encoded columns, then only the selected values of each
            // summed column are fetched (into one decoded vector) and added up.
            const Predicate& first = p_.where[0];
            cnt = t.column(first.col).select(s, n, first, sel, scratch);
            for (std::size_t q = 1; q < p_.where.size() && cnt > 0; ++q)
                cnt = t.column(p_.where[q].col).refine(s, sel, cnt, p_.where[q], scratch);
            for (int c : p_.columns) {
                t.column(c).gather(s, sel, cnt, vals, 1, scratch);
                uint64_t part = 0;
                for (std::size_t i = 0; i < cnt; ++i) part += static_cast<uint64_t>(vals[i]);
                sum += part;
            }
        } else if (is_late(st) && p_.where.empty()) {
            // Late without a WHERE clause: aggregate each column straight on its compressed
            // form (EncodedColumn::sum: fused unpack-and-add, or value x run length).
            for (int c : p_.columns) sum += t.column(c).sum(s, n, scratch);
            cnt = n;
        } else {
            // Decompress every referenced column of the vector, then aggregate the decoded
            // values (row at a time for early, column at a time for late).
            for (std::size_t i = 0; i < refs_.size(); ++i)
                decode_range(t.column(refs_[i]), s, n, decoded_.data() + i * (batch + 64));
            const Partial r = chunk(kern::ColumnChunk{ptr.data()}, n, st);
            sum += r.sum;
            cnt = r.count;
        }
        if (p_.strategy == Strategy::Auto) auto_observe(cnt, n);
        count += cnt;
    }
    sum_ = sum;
    rows_out_ = count;
}

void SumQuery::run_value_mode(const EncodedDsmLayout& t) {
    // Tuple at a time through one cursor per referenced column (a virtual call per value).
    const std::size_t rows = t.rows();
    uint64_t sum = 0;
    std::size_t count = 0;
    if (p_.where.empty()) {
        // Column at a time, as the original value-mode scan: one cursor drained per column.
        for (int c : p_.columns) {
            auto cur = t.column(c).cursor();
            for (std::size_t r = 0; r < rows; ++r) sum += static_cast<uint64_t>(cur->next());
        }
        sum_ = sum;
        rows_out_ = rows;
        return;
    }
    std::vector<std::unique_ptr<ColumnCursor>> cursors;
    for (int c : refs_) cursors.push_back(t.column(c).cursor());
    std::vector<int64_t> tuple(t.cols());
    for (std::size_t r = 0; r < rows; ++r) {
        for (std::size_t i = 0; i < refs_.size(); ++i) tuple[refs_[i]] = cursors[i]->next();
        bool ok = true;
        for (const Predicate& pr : p_.where) ok &= pr.test(tuple[pr.col]);
        uint64_t s = 0;
        for (int c : p_.columns) s += static_cast<uint64_t>(tuple[c]);
        sum += s & (0 - static_cast<uint64_t>(ok));
        count += ok;
    }
    sum_ = sum;
    rows_out_ = count;
}

int64_t SumQuery::reference(const Table& t, const QueryParams& p) {
    uint64_t sum = 0;
    std::size_t count = 0;
    for (std::size_t r = 0; r < t.rows; ++r) {
        bool ok = true;
        for (const Predicate& pr : p.where) {
            const int64_t v = t.at(r, pr.col);
            ok = ok && v >= pr.lo && v <= pr.hi;
        }
        if (!ok) continue;
        for (int c : p.columns) sum += static_cast<uint64_t>(t.at(r, c));
        ++count;
    }
    return combine(sum, count);
}
