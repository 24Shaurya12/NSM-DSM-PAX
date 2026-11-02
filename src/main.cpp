#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <functional>
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#include "dsm.h"
#include "layout.h"
#include "nsm.h"
#include "pax.h"
#include "queries/projection.h"
#include "queries/sum.h"
#include "query.h"
#include "table.h"

namespace {

constexpr uint64_t kSeed = 42;
constexpr int kWarmupIterations = 2;
constexpr int kTimedIterations = 10;

// Every query result is folded into this so the compiler cannot drop the work.
volatile int64_t g_sink = 0;

// Builds a query over the given columns. Called outside the timed region, so a query
// can allocate whatever buffers it needs here.
using QueryFactory =
    std::function<std::unique_ptr<Query>(std::vector<int> columns, std::size_t rows)>;

struct QueryEntry {
    std::string name;
    QueryFactory make;
};

// To add a query: write src/queries/<name>.{h,cpp} and add one entry here.
const std::vector<QueryEntry> kQueries = {
    {"sum", [](std::vector<int> cols, std::size_t) { return std::make_unique<SumQuery>(cols); }},
    {"projection",
     [](std::vector<int> cols, std::size_t rows) {
         return std::make_unique<ProjectionQuery>(cols, rows);
     }},
};

struct Options {
    std::size_t rows = 1'000'000;
    std::size_t cols = 16;
    std::size_t page_size = 16 * 1024;
    std::vector<int> ks;  // empty -> 1, 2, 4, ..., cols
    std::vector<const QueryEntry*> queries;  // empty -> all
    std::string output;   // empty -> stdout
};

[[noreturn]] void usage(const char* prog) {
    std::cerr << "Usage: " << prog << " [options]\n"
              << "  --rows R         number of rows (default 1000000)\n"
              << "  --cols N         number of int64 columns (default 16)\n"
              << "  --page-size B    PAX page size in bytes, multiple of 64 (default 16384)\n"
              << "  --k K1,K2,...    number of leading columns each query reads\n"
              << "                   (default 1,2,4,...,N)\n"
              << "  --query Q1,Q2    queries to run (default: all of";
    for (const auto& q : kQueries) std::cerr << ' ' << q.name;
    std::cerr << ")\n"
              << "  --output FILE    write CSV to FILE instead of stdout\n";
    std::exit(1);
}

std::vector<std::string> split_commas(const std::string& s) {
    std::vector<std::string> items;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, ',')) items.push_back(item);
    return items;
}

const QueryEntry* find_query(const std::string& name) {
    for (const auto& q : kQueries)
        if (q.name == name) return &q;
    return nullptr;
}

Options parse_args(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (i + 1 >= argc) usage(argv[0]);
        std::string val = argv[++i];
        if (arg == "--rows") o.rows = std::stoull(val);
        else if (arg == "--cols") o.cols = std::stoull(val);
        else if (arg == "--page-size") o.page_size = std::stoull(val);
        else if (arg == "--k") {
            for (const auto& k : split_commas(val)) o.ks.push_back(std::stoi(k));
        } else if (arg == "--query") {
            for (const auto& name : split_commas(val)) {
                const QueryEntry* q = find_query(name);
                if (q == nullptr) {
                    std::cerr << "error: unknown query '" << name << "'\n";
                    usage(argv[0]);
                }
                o.queries.push_back(q);
            }
        }
        else if (arg == "--output") o.output = val;
        else usage(argv[0]);
    }
    if (o.rows == 0 || o.cols == 0) usage(argv[0]);
    if (o.ks.empty()) {
        for (std::size_t k = 1; k < o.cols; k *= 2) o.ks.push_back(static_cast<int>(k));
        o.ks.push_back(static_cast<int>(o.cols));
    }
    if (o.queries.empty())
        for (const auto& q : kQueries) o.queries.push_back(&q);
    for (int k : o.ks) {
        if (k < 1 || static_cast<std::size_t>(k) > o.cols) {
            std::cerr << "error: k=" << k << " outside [1, " << o.cols << "]\n";
            std::exit(1);
        }
    }
    return o;
}

// Columns a query with parameter k reads: the first k.
std::vector<int> first_k_columns(int k) {
    std::vector<int> cols(k);
    for (int c = 0; c < k; ++c) cols[c] = c;
    return cols;
}

}  // namespace

int main(int argc, char** argv) {
    Options opt = parse_args(argc, argv);

    // --- Setup (untimed): generate data once, build every layout from it. ---
    std::vector<std::unique_ptr<Layout>> layouts;
    try {
        Table table = generate_table(opt.rows, opt.cols, kSeed);
        layouts.push_back(std::make_unique<NsmLayout>(table));
        layouts.push_back(std::make_unique<DsmLayout>(table));
        layouts.push_back(std::make_unique<PaxLayout>(table, opt.page_size));
    } catch (const std::exception& e) {  // logical table is freed on scope exit either way
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }

    const double mb = 1024.0 * 1024.0;
    std::cerr << "Table: " << opt.rows << " rows x " << opt.cols << " cols, "
              << (opt.rows * opt.cols * sizeof(int64_t)) / mb << " MB of data\n";
    for (const auto& l : layouts)
        std::cerr << "  " << l->name() << " footprint: " << l->footprint_bytes() / mb << " MB\n";

    // --- Correctness: for every query and k, all layouts must produce the same result. ---
    // Each layout gets a fresh query so it cannot pass by reusing another layout's output.
    for (const QueryEntry* qe : opt.queries) {
        for (int k : opt.ks) {
            int64_t expected = 0;
            for (std::size_t li = 0; li < layouts.size(); ++li) {
                auto q = qe->make(first_k_columns(k), opt.rows);
                layouts[li]->run(*q);
                int64_t got = q->result();
                if (li == 0) {
                    expected = got;
                } else if (got != expected) {
                    std::cerr << "error: " << qe->name << " mismatch for k=" << k << ": "
                              << layouts[0]->name() << "=" << expected << " "
                              << layouts[li]->name() << "=" << got << "\n";
                    return 1;
                }
            }
        }
    }
    std::cerr << "Correctness check passed for queries {";
    for (std::size_t i = 0; i < opt.queries.size(); ++i)
        std::cerr << (i ? "," : "") << opt.queries[i]->name;
    std::cerr << "}, k in {";
    for (std::size_t i = 0; i < opt.ks.size(); ++i) std::cerr << (i ? "," : "") << opt.ks[i];
    std::cerr << "}\n";

    // --- Benchmark ---
    std::ofstream file;
    if (!opt.output.empty()) {
        file.open(opt.output);
        if (!file) {
            std::cerr << "error: cannot open " << opt.output << "\n";
            return 1;
        }
    }
    std::ostream& out = opt.output.empty() ? std::cout : file;
    out << "query,layout,rows,cols,k,page_size,iteration,latency_ns,result\n";

    for (const QueryEntry* qe : opt.queries) {
        for (int k : opt.ks) {
            auto q = qe->make(first_k_columns(k), opt.rows);  // untimed setup
            for (const auto& l : layouts) {
                // page_size is only meaningful for PAX; report 0 for the other layouts.
                std::size_t page_size =
                    dynamic_cast<const PaxLayout*>(l.get()) ? opt.page_size : 0;

                for (int i = 0; i < kWarmupIterations; ++i) {
                    l->run(*q);
                    g_sink = g_sink + q->result();
                }

                for (int i = 0; i < kTimedIterations; ++i) {
                    auto start = std::chrono::steady_clock::now();
                    l->run(*q);
                    auto end = std::chrono::steady_clock::now();
                    int64_t result = q->result();
                    g_sink = g_sink + result;
                    auto ns =
                        std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
                    out << q->name() << ',' << l->name() << ',' << opt.rows << ',' << opt.cols
                        << ',' << k << ',' << page_size << ',' << i << ',' << ns << ','
                        << result << '\n';
                }
            }
        }
    }
    return 0;
}
