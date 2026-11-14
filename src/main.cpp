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
#include "encoded_dsm.h"
#include "encodings/encoded_column.h"
#include "layout.h"
#include "nsm.h"
#include "pax.h"
#include "perf_counters.h"
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
    std::string perf_set;  // empty -> no hardware counters
    Distribution dist = Distribution::Uniform;
    std::size_t run_length = 64;
    bool nsm = true, dsm = true, pax = true;  // uncompressed layouts to run
    std::vector<Encoding> encodings;          // encoded DSM layouts; empty -> none
    std::vector<ScanMode> modes{ScanMode::Value, ScanMode::Vector};
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
              << "  --output FILE    write CSV to FILE instead of stdout\n"
              << "  --perf SET       also record hardware counters per run; SET is one of";
    for (const auto& s : PerfCounters::set_names()) std::cerr << ' ' << s;
    std::cerr << "\n"
              << "  --dist D         data distribution (default uniform); one of";
    for (Distribution d : all_distributions()) std::cerr << ' ' << distribution_name(d);
    std::cerr << "\n"
              << "  --run-length L   mean run length for --dist runs (default 64)\n"
              << "  --layout L1,L2   uncompressed layouts to run: nsm,dsm,pax or none (default all)\n"
              << "  --encoding E1,E2 also run DSM with each column encoded; 'all' or any of";
    for (Encoding e : all_encodings()) std::cerr << ' ' << encoding_name(e);
    std::cerr << "\n"
              << "  --mode M1,M2     scan modes for encoded layouts: value,vector (default both)\n";
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
        else if (arg == "--perf") o.perf_set = val;
        else if (arg == "--dist") {
            if (!parse_distribution(val, o.dist)) {
                std::cerr << "error: unknown distribution '" << val << "'\n";
                usage(argv[0]);
            }
        } else if (arg == "--run-length") o.run_length = std::stoull(val);
        else if (arg == "--layout") {
            o.nsm = o.dsm = o.pax = false;
            for (const auto& name : split_commas(val)) {
                if (name == "nsm") o.nsm = true;
                else if (name == "dsm") o.dsm = true;
                else if (name == "pax") o.pax = true;
                else if (name != "none") {
                    std::cerr << "error: unknown layout '" << name << "'\n";
                    usage(argv[0]);
                }
            }
        } else if (arg == "--encoding") {
            o.encodings.clear();
            if (val == "all") {
                o.encodings = all_encodings();
            } else {
                for (const auto& name : split_commas(val)) {
                    Encoding e;
                    if (!parse_encoding(name, e)) {
                        std::cerr << "error: unknown encoding '" << name << "'\n";
                        usage(argv[0]);
                    }
                    o.encodings.push_back(e);
                }
            }
        } else if (arg == "--mode") {
            o.modes.clear();
            for (const auto& name : split_commas(val)) {
                ScanMode m;
                if (!parse_scan_mode(name, m)) {
                    std::cerr << "error: unknown mode '" << name << "'\n";
                    usage(argv[0]);
                }
                o.modes.push_back(m);
            }
        }
        else usage(argv[0]);
    }
    if (o.rows == 0 || o.cols == 0) usage(argv[0]);
    if (!o.nsm && !o.dsm && !o.pax && (o.encodings.empty() || o.modes.empty())) {
        std::cerr << "error: no layouts selected\n";
        usage(argv[0]);
    }
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
        Table table = generate_table(opt.rows, opt.cols, kSeed, opt.dist, opt.run_length);
        if (opt.nsm) layouts.push_back(std::make_unique<NsmLayout>(table));
        if (opt.dsm) layouts.push_back(std::make_unique<DsmLayout>(table));
        if (opt.pax) layouts.push_back(std::make_unique<PaxLayout>(table, opt.page_size));
        if (!opt.modes.empty()) {
            for (Encoding e : opt.encodings) {
                // Encoded once; the value and vector layouts share the same columns.
                auto data = std::make_shared<const EncodedColumns>(table, e);
                for (ScanMode m : opt.modes)
                    layouts.push_back(std::make_unique<EncodedDsmLayout>(data, m));
            }
        }
    } catch (const std::exception& e) {  // logical table is freed on scope exit either way
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }

    const double mb = 1024.0 * 1024.0;
    std::cerr << "Table: " << opt.rows << " rows x " << opt.cols << " cols, "
              << (opt.rows * opt.cols * sizeof(int64_t)) / mb << " MB of data, "
              << distribution_name(opt.dist) << " distribution\n";
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
    std::unique_ptr<PerfCounters> counters;
    if (!opt.perf_set.empty()) {
        try {
            counters = std::make_unique<PerfCounters>(opt.perf_set);
        } catch (const std::exception& e) {
            std::cerr << "error: " << e.what() << "\n";
            return 1;
        }
    }

    std::ofstream file;
    if (!opt.output.empty()) {
        file.open(opt.output);
        if (!file) {
            std::cerr << "error: cannot open " << opt.output << "\n";
            return 1;
        }
    }
    std::ostream& out = opt.output.empty() ? std::cout : file;
    out << "query,layout,dist,rows,cols,k,page_size,footprint_bytes,iteration,latency_ns,result";
    if (counters)
        for (const auto& name : counters->names()) out << ',' << name;
    out << '\n';

    const std::string dist = distribution_name(opt.dist);
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
                    // Counters wrap the clock reads so their ioctls stay out of latency_ns.
                    if (counters) counters->start();
                    auto start = std::chrono::steady_clock::now();
                    l->run(*q);
                    auto end = std::chrono::steady_clock::now();
                    if (counters) counters->stop();
                    int64_t result = q->result();
                    g_sink = g_sink + result;
                    auto ns =
                        std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
                    out << q->name() << ',' << l->name() << ',' << dist << ',' << opt.rows
                        << ',' << opt.cols << ',' << k << ',' << page_size << ','
                        << l->footprint_bytes() << ',' << i << ',' << ns << ',' << result;
                    if (counters)
                        for (uint64_t v : counters->values()) out << ',' << v;
                    out << '\n';
                }
            }
        }
    }
    if (counters && counters->multiplexed())
        std::cerr << "warning: perf counters were multiplexed; counts are scaled estimates\n";
    return 0;
}
