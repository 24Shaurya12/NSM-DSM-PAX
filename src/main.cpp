#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#include "dsm.h"
#include "layout.h"
#include "nsm.h"
#include "pax.h"
#include "perf_counters.h"
#include "predicate.h"
#include "table.h"

namespace {

constexpr uint64_t kSeed = 42;
constexpr long kMaxReps = 1'000'000;

// Every scan result is folded into this so the compiler cannot drop the scan.
volatile int64_t g_sink = 0;

// One WHERE clause from the command line. An empty conjunction is the unfiltered scan.
struct Query {
    std::string label;  // as written on the command line, with ',' shown as " & " for the CSV
    Conjunction where;
};

// What the query does with the qualifying rows.
//   sum:         SELECT COUNT(*), SUM(c0 + ... + c{k-1}) FROM T WHERE ...
//   materialize: SELECT c0, ..., c{k-1} FROM T WHERE ...   (tuple reconstruction)
enum class Op { Sum, Materialize };

const char* op_name(Op op) { return op == Op::Sum ? "sum" : "materialize"; }

// Runs one query on one layout. A sum without predicates is the plain full scan.
QueryResult run_query(const Layout& l, Op op, const Query& q, int k) {
    if (op == Op::Materialize) return l.filter_materialize(q.where, k);
    if (q.where.empty()) return {static_cast<int64_t>(l.rows()), l.sum_first_k(k)};
    return l.filter_sum(q.where, k);
}

struct Options {
    std::size_t rows = 0;        // 0 -> derived from size_bytes, else 1000000
    std::size_t size_bytes = 0;  // requested table size; rows = size / (cols * 8)
    std::size_t cols = 16;
    std::size_t page_size = 16 * 1024;
    std::vector<int> ks;  // empty -> 1, 2, 4, ..., cols
    std::vector<Query> queries;  // empty -> one query without predicates
    std::vector<Op> ops = {Op::Sum};
    std::vector<std::string> layouts = {"nsm", "dsm", "pax"};
    std::vector<std::string> modes = {"warm"};
    int warmup = 2;
    int iterations = 10;
    double min_sample_ms = 20.0;
    std::string output;  // empty -> stdout
};

[[noreturn]] void usage(const char* prog) {
    std::cerr
        << "Usage: " << prog << " [options]\n"
        << "  --rows R            number of rows (default 1000000)\n"
        << "  --size BYTES        table size instead of --rows, suffix K/M/G allowed (e.g. 64M);\n"
        << "                      rows = BYTES / (cols * 8), rounded down\n"
        << "  --cols N            number of int64 columns (default 16)\n"
        << "  --page-size B       PAX page size in bytes, multiple of 64 (default 16384)\n"
        << "  --k K1,K2,...       columns to sum (default 1,2,4,...,N)\n"
        << "  --where PREDS       filter rows; repeat the flag to run several queries.\n"
        << "                      PREDS is a comma-separated conjunction of terms:\n"
        << "                        c3=42 (equality), c0<10, c0<=10, c0>90, c0>=90,\n"
        << "                        c2=10..19 (inclusive range). Values are in [0, 100).\n"
        << "                      PREDS = none means no predicate (full scan), which lets\n"
        << "                      one run mix filtered and unfiltered queries.\n"
        << "                      Default: no predicate (full scan)\n"
        << "  --op O1,O2          what to do with qualifying rows (default sum):\n"
        << "                        sum:         SELECT COUNT(*), SUM(c0+...+c{k-1})\n"
        << "                        materialize: SELECT c0,...,c{k-1} (tuple reconstruction)\n"
        << "  --layout L1,L2,...  layouts to build and run: nsm, dsm, pax (default all)\n"
        << "  --modes M1,M2       warm and/or cold (default warm). cold flushes the table\n"
        << "                      from all caches before every timed scan\n"
        << "  --warmup W          untimed scans before measuring each k (default 2)\n"
        << "  --iterations I      timed samples per (layout, op, query, mode, k) (default 10)\n"
        << "  --min-sample-ms T   warm mode repeats the scan until a sample lasts >= T ms\n"
        << "                      (default 20)\n"
        << "  --output FILE       write CSV to FILE instead of stdout\n";
    std::exit(1);
}

std::vector<std::string> split(const std::string& s) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, ',')) out.push_back(item);
    return out;
}

std::vector<int> parse_k_list(const std::string& s) {
    std::vector<int> ks;
    for (const std::string& item : split(s)) ks.push_back(std::stoi(item));
    return ks;
}

// "64M" -> 64 * 2^20. Suffixes K, M, G (binary, case-insensitive); no suffix means bytes.
std::size_t parse_size(const std::string& s) {
    std::size_t pos = 0;
    std::size_t n = std::stoull(s, &pos);
    std::string suffix = s.substr(pos);
    if (suffix.empty()) return n;
    if (suffix.size() == 1) {
        switch (std::toupper(static_cast<unsigned char>(suffix[0]))) {
            case 'K': return n << 10;
            case 'M': return n << 20;
            case 'G': return n << 30;
        }
    }
    throw std::invalid_argument("bad size suffix in '" + s + "'");
}

Query parse_query(const std::string& text) {
    if (text == "none") return {"none", {}};
    Query q;
    try {
        q.where = parse_conjunction(text);
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        std::exit(1);
    }
    for (char ch : text) {
        if (ch == ',') q.label += " & ";
        else q.label += ch;
    }
    return q;
}

Options parse_args(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (i + 1 >= argc) usage(argv[0]);
        std::string val = argv[++i];
        if (arg == "--rows") o.rows = std::stoull(val);
        else if (arg == "--size") o.size_bytes = parse_size(val);
        else if (arg == "--cols") o.cols = std::stoull(val);
        else if (arg == "--page-size") o.page_size = std::stoull(val);
        else if (arg == "--k") o.ks = parse_k_list(val);
        else if (arg == "--where") o.queries.push_back(parse_query(val));
        else if (arg == "--op") {
            o.ops.clear();
            for (const std::string& name : split(val)) {
                if (name == "sum") o.ops.push_back(Op::Sum);
                else if (name == "materialize") o.ops.push_back(Op::Materialize);
                else {
                    std::cerr << "error: unknown op '" << name << "'\n";
                    std::exit(1);
                }
            }
        }
        else if (arg == "--layout") o.layouts = split(val);
        else if (arg == "--modes") o.modes = split(val);
        else if (arg == "--warmup") o.warmup = std::stoi(val);
        else if (arg == "--iterations") o.iterations = std::stoi(val);
        else if (arg == "--min-sample-ms") o.min_sample_ms = std::stod(val);
        else if (arg == "--output") o.output = val;
        else usage(argv[0]);
    }
    if (o.cols == 0) usage(argv[0]);
    if (o.rows != 0 && o.size_bytes != 0) {
        std::cerr << "error: give --rows or --size, not both\n";
        std::exit(1);
    }
    if (o.size_bytes != 0) o.rows = o.size_bytes / (o.cols * sizeof(int64_t));
    else if (o.rows == 0) o.rows = 1'000'000;
    if (o.rows == 0) {
        std::cerr << "error: --size is smaller than one row\n";
        std::exit(1);
    }
    for (const std::string& l : o.layouts) {
        if (l != "nsm" && l != "dsm" && l != "pax") {
            std::cerr << "error: unknown layout '" << l << "'\n";
            std::exit(1);
        }
    }
    for (const std::string& m : o.modes) {
        if (m != "warm" && m != "cold") {
            std::cerr << "error: unknown mode '" << m << "'\n";
            std::exit(1);
        }
    }
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
    if (o.queries.empty()) o.queries.push_back({"none", {}});
    for (const Query& q : o.queries) {
        for (const Predicate& p : q.where) {
            if (p.col < 0 || static_cast<std::size_t>(p.col) >= o.cols) {
                std::cerr << "error: predicate '" << q.label << "' uses c" << p.col
                          << ", but the table has columns c0..c" << o.cols - 1 << "\n";
                std::exit(1);
            }
        }
    }
    return o;
}

double now_ns() {
    return std::chrono::duration<double, std::nano>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

}  // namespace

int main(int argc, char** argv) {
    Options opt = parse_args(argc, argv);

    // --- Setup (untimed): generate data once, build the requested layouts from it, and
    // compute the expected sums. The logical table is freed before measuring. ---
    std::vector<std::unique_ptr<Layout>> layouts;
    // expected[q][k] = reference result of query q with op sum over columns [0, k);
    // expected_mat[q][k] = the same for op materialize (count and the TupleSink checksum).
    std::vector<std::vector<QueryResult>> expected(opt.queries.size());
    std::vector<std::vector<QueryResult>> expected_mat(opt.queries.size());
    try {
        Table table = generate_table(opt.rows, opt.cols, kSeed);
        for (const std::string& l : opt.layouts) {
            if (l == "nsm") layouts.push_back(std::make_unique<NsmLayout>(table));
            if (l == "dsm") layouts.push_back(std::make_unique<DsmLayout>(table));
            if (l == "pax") layouts.push_back(std::make_unique<PaxLayout>(table, opt.page_size));
        }
        for (std::size_t q = 0; q < opt.queries.size(); ++q) {
            int64_t count = 0;
            std::vector<int64_t> col_sum(opt.cols, 0);
            for (std::size_t r = 0; r < table.rows; ++r) {
                bool qualifies = true;
                for (const Predicate& p : opt.queries[q].where)
                    qualifies = qualifies && p.matches(table.at(r, p.col));
                if (!qualifies) continue;
                ++count;
                for (std::size_t c = 0; c < table.cols; ++c) col_sum[c] += table.at(r, c);
            }
            expected[q].assign(opt.cols + 1, QueryResult{count, 0});
            expected_mat[q].assign(opt.cols + 1, QueryResult{count, 0});
            for (std::size_t c = 1; c <= opt.cols; ++c) {
                expected[q][c].sum = expected[q][c - 1].sum + col_sum[c - 1];
                expected_mat[q][c].sum = expected_mat[q][c - 1].sum +
                                         static_cast<int64_t>(c) * col_sum[c - 1];
            }
        }
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }

    const std::size_t row_bytes = opt.cols * sizeof(int64_t);
    const std::size_t data_bytes = opt.rows * row_bytes;
    const double mb = 1024.0 * 1024.0;
    std::cerr << "Table: " << opt.rows << " rows x " << opt.cols << " cols, " << data_bytes / mb
              << " MB of data\n";
    for (const auto& l : layouts)
        std::cerr << "  " << l->name() << " footprint: " << l->footprint_bytes() / mb << " MB\n";

    // --- Correctness: every layout must match the results computed from the logical table. ---
    for (const auto& l : layouts) {
        for (Op op : opt.ops) {
            for (std::size_t q = 0; q < opt.queries.size(); ++q) {
                for (int k : opt.ks) {
                    QueryResult got = run_query(*l, op, opt.queries[q], k);
                    const QueryResult& want =
                        op == Op::Sum ? expected[q][k] : expected_mat[q][k];
                    if (got != want) {
                        std::cerr << "error: mismatch for op=" << op_name(op)
                                  << " where=" << opt.queries[q].label << " k=" << k
                                  << ": expected count=" << want.count << " sum=" << want.sum
                                  << ", " << l->name() << " returned count=" << got.count
                                  << " sum=" << got.sum << "\n";
                        return 1;
                    }
                }
            }
        }
    }
    std::cerr << "Correctness check passed for k in {";
    for (std::size_t i = 0; i < opt.ks.size(); ++i) std::cerr << (i ? "," : "") << opt.ks[i];
    std::cerr << "}\n";
    for (std::size_t q = 0; q < opt.queries.size(); ++q)
        std::cerr << "  where " << opt.queries[q].label << ": " << expected[q][0].count << " of "
                  << opt.rows << " rows qualify ("
                  << 100.0 * static_cast<double>(expected[q][0].count) / opt.rows << "%)\n";

    PerfCounters perf;
    std::cerr << "Hardware counters:";
    for (std::size_t i = 0; i < perf.size(); ++i)
        std::cerr << " " << perf.name(i) << (perf.available(i) ? "" : "(unavailable)");
    std::cerr << "\n";

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
    out << "layout,rows,cols,row_bytes,data_bytes,footprint_bytes,k,op,predicate,page_size,mode,"
           "iteration,reps,latency_ns,matches,selectivity,result";
    for (std::size_t i = 0; i < perf.size(); ++i) out << ',' << perf.name(i);
    out << '\n';
    out << std::fixed << std::setprecision(1);

    const double min_sample_ns = opt.min_sample_ms * 1e6;
    for (const auto& l : layouts) {
        // page_size is only meaningful for PAX; report 0 for the other layouts.
        std::size_t page_size = dynamic_cast<const PaxLayout*>(l.get()) ? opt.page_size : 0;
        for (Op op : opt.ops) {
            for (const Query& q : opt.queries) {
                for (const std::string& mode : opt.modes) {
                    const bool cold = mode == "cold";
                    for (int k : opt.ks) {
                        for (int i = 0; i < opt.warmup; ++i)
                            g_sink = g_sink + run_query(*l, op, q, k).sum;

                        // Warm: repeat the query so each sample is long enough to time
                        // precisely; the data stays cached between repetitions if it fits.
                        // Cold: one query per sample, right after flushing the table from every
                        // cache level.
                        long reps = 1;
                        if (!cold) {
                            double t0 = now_ns();
                            g_sink = g_sink + run_query(*l, op, q, k).sum;
                            double one = std::max(now_ns() - t0, 1.0);
                            reps = std::clamp(static_cast<long>(std::ceil(min_sample_ns / one)),
                                              1L, kMaxReps);
                        }

                        for (int it = 0; it < opt.iterations; ++it) {
                            if (cold) l->flush_cache();
                            QueryResult result;
                            perf.start();
                            double t0 = now_ns();
                            for (long r = 0; r < reps; ++r) {
                                result = run_query(*l, op, q, k);
                                g_sink = g_sink + result.sum;
                            }
                            double elapsed = now_ns() - t0;
                            perf.stop();

                            // Latency and counters are reported per query.
                            out << l->name() << ',' << opt.rows << ',' << opt.cols << ','
                                << row_bytes << ',' << data_bytes << ',' << l->footprint_bytes()
                                << ',' << k << ',' << op_name(op) << ',' << q.label << ','
                                << page_size << ',' << mode << ',' << it << ',' << reps << ','
                                << elapsed / reps << ',' << result.count << ','
                                << std::setprecision(6)
                                << static_cast<double>(result.count) / opt.rows
                                << std::setprecision(1) << ',' << result.sum;
                            for (std::size_t i = 0; i < perf.size(); ++i) {
                                out << ',';
                                if (perf.available(i)) out << perf.value(i) / reps;
                            }
                            out << '\n';
                        }
                    }
                }
            }
        }
    }
    return 0;
}
