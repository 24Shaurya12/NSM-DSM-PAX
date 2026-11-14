#pragma once

// Chunk iteration for the uncompressed layouts: calls f(chunk, first_row, n) for consecutive
// runs of at most `batch` rows, in row order. The chunk accessors are in kernels.h.

#include <algorithm>
#include <cstddef>
#include <vector>

#include "dsm.h"
#include "kernels.h"
#include "nsm.h"
#include "pax.h"

template <class F>
void for_each_chunk(const NsmLayout& t, std::size_t batch, F&& f) {
    const std::size_t rows = t.rows(), stride = t.cols();
    for (std::size_t s = 0; s < rows; s += batch)
        f(kern::RowChunk{t.row(s), stride}, s, std::min(batch, rows - s));
}

// Only the pointers of `cols` are set in the chunk; kernels must not read other columns.
template <class F>
void for_each_chunk(const DsmLayout& t, const std::vector<int>& cols, std::size_t batch, F&& f) {
    const std::size_t rows = t.rows();
    std::vector<const int64_t*> ptr(t.cols(), nullptr);
    for (std::size_t s = 0; s < rows; s += batch) {
        for (int c : cols) ptr[c] = t.column(c) + s;
        f(kern::ColumnChunk{ptr.data()}, s, std::min(batch, rows - s));
    }
}

// A chunk never crosses a page boundary: each page is processed in pieces of at most
// `batch` tuples, so every chunk's minipages lie inside one page.
template <class F>
void for_each_chunk(const PaxLayout& t, const std::vector<int>& cols, std::size_t batch, F&& f) {
    std::vector<const int64_t*> ptr(t.cols(), nullptr);
    for (std::size_t p = 0; p < t.num_pages(); ++p) {
        const std::size_t first = t.page_first_row(p), count = t.page_tuple_count(p);
        for (std::size_t off = 0; off < count; off += batch) {
            for (int c : cols) ptr[c] = t.minipage(p, c) + off;
            f(kern::ColumnChunk{ptr.data()}, first + off, std::min(batch, count - off));
        }
    }
}
