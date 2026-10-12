#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <exception>
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
#include "table.h"

namespace {

constexpr uint64_t kSeed = 42;
constexpr int kWarmupIterations = 2;
constexpr int kTimedIterations = 10;

// Every scan result is folded into this so the compiler cannot drop the scan.
volatile int64_t g_sink = 0;

struct Options {
    std::size_t rows = 1'000'000;
    std::size_t cols = 16;
    std::size_t page_size = 16 * 1024;
    std::vector<int> ks;  // empty -> 1, 2, 4, ..., cols
    std::string output;   // empty -> stdout
};

[[noreturn]] void usage(const char* prog) {
    std::cerr << "Usage: " << prog << " [options]\n"
              << "  --rows R         number of rows (default 1000000)\n"
              << "  --cols N         number of int64 columns (default 16)\n"
              << "  --page-size B    PAX page size in bytes, multiple of 64 (default 16384)\n"
              << "  --k K1,K2,...    columns to sum (default 1,2,4,...,N)\n"
              << "  --output FILE    write CSV to FILE instead of stdout\n";
    std::exit(1);
}

std::vector<int> parse_k_list(const std::string& s) {
    std::vector<int> ks;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, ',')) ks.push_back(std::stoi(item));
    return ks;
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
        else if (arg == "--k") o.ks = parse_k_list(val);
        else if (arg == "--output") o.output = val;
        else usage(argv[0]);
    }
    if (o.rows == 0 || o.cols == 0) usage(argv[0]);
    if (o.ks.empty()) {
        for (std::size_t k = 1; k < o.cols; k *= 2) o.ks.push_back(static_cast<int>(k));
        o.ks.push_back(static_cast<int>(o.cols));
    }
    for (int k : o.ks) {
        if (k < 1 || static_cast<std::size_t>(k) > o.cols) {
            std::cerr << "error: k=" << k << " outside [1, " << o.cols << "]\n";
            std::exit(1);
        }
    }
    return o;
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

    // --- Correctness: all layouts must agree for every k. ---
    for (int k : opt.ks) {
        int64_t expected = layouts[0]->sum_first_k(k);
        for (const auto& l : layouts) {
            int64_t got = l->sum_first_k(k);
            if (got != expected) {
                std::cerr << "error: sum mismatch for k=" << k << ": " << layouts[0]->name()
                          << "=" << expected << " " << l->name() << "=" << got << "\n";
                return 1;
            }
        }
    }
    std::cerr << "Correctness check passed for k in {";
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
    out << "layout,rows,cols,k,page_size,iteration,latency_ns,result\n";

    for (const auto& l : layouts) {
        // page_size is only meaningful for PAX; report 0 for the other layouts.
        std::size_t page_size = dynamic_cast<const PaxLayout*>(l.get()) ? opt.page_size : 0;
        for (int k : opt.ks) {
            for (int i = 0; i < kWarmupIterations; ++i) g_sink = g_sink + l->sum_first_k(k);

            for (int i = 0; i < kTimedIterations; ++i) {
                auto start = std::chrono::steady_clock::now();
                int64_t result = l->sum_first_k(k);
                g_sink = g_sink + result;
                auto end = std::chrono::steady_clock::now();
                auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
                out << l->name() << ',' << opt.rows << ',' << opt.cols << ',' << k << ','
                    << page_size << ',' << i << ',' << ns << ',' << result << '\n';
            }
        }
    }
    return 0;
}
