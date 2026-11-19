#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <functional>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#include "dsm.h"
#include "encoded_dsm.h"
#include "encodings/bits.h"
#include "encodings/encoded_column.h"
#include "layout.h"
#include "machine.h"
#include "nsm.h"
#include "pax.h"
#include "perf_counters.h"
#include "predicate.h"
#include "queries/projection.h"
#include "queries/sum.h"
#include "query.h"
#include "table.h"

namespace {

constexpr uint64_t kSeed = 42;

// Every query result is folded into this so the compiler cannot drop the work.
volatile int64_t g_sink = 0;

// Builds a query. Called outside the timed region, so a query can allocate whatever
// buffers it needs here.
using QueryFactory = std::function<std::unique_ptr<Query>(QueryParams)>;

struct QueryEntry {
    std::string name;
    QueryFactory make;
    // The expected result(), from a naive loop over the logical table.
    int64_t (*reference)(const Table&, const QueryParams&);
    // Whether the query writes output tuples. For --batch auto, a vector keeps live per row:
    // 4 bytes of selection vector, 8 per referenced column (decoded values), and for a
    // query that writes tuples 8 per output column (the part of the output a late
    // projection fills one column at a time).
    bool writes_tuples;
};

// To add a query: write src/queries/<name>.{h,cpp} and add one entry here.
const std::vector<QueryEntry> kQueries = {
    {"sum", [](QueryParams p) { return std::make_unique<SumQuery>(std::move(p)); },
     &SumQuery::reference, false},
    {"projection", [](QueryParams p) { return std::make_unique<ProjectionQuery>(std::move(p)); },
     &ProjectionQuery::reference, true},
};

struct Options {
    std::size_t rows = 1'000'000;
    std::size_t cols = 16;
    std::vector<std::size_t> page_sizes{16 * 1024};
    std::size_t pax_pad = 0;
    std::vector<int> ks;                     // empty -> 1, 2, 4, ..., cols
    std::vector<const QueryEntry*> queries;  // empty -> all
    std::string output;                      // empty -> stdout
    std::string perf_set;                    // empty -> no hardware counters
    Distribution dist = Distribution::Uniform;
    std::size_t run_length = 64;
    int64_t domain = 100;
    bool nsm = true, dsm = true, pax = true;  // uncompressed layouts to run
    std::vector<Encoding> encodings;          // encoded DSM layouts; empty -> none
    std::vector<ScanMode> modes{ScanMode::Value, ScanMode::Vector};
    std::vector<Strategy> strategies;  // empty -> all (deduplicated without a WHERE clause)
    std::vector<std::size_t> batches;  // 0 = auto; empty -> 1024
    std::vector<double> sels;          // selectivities of the generated predicate
    int pred_col = 0;
    std::vector<Predicate> where;  // fixed predicates, ANDed with the generated one
    int iters = 10, warmup = 2;
    int rounds = 1;
    double min_time_ms = 0;
    std::string dsm_offset = "0";
    bool hugepages = false;
    bool simd = false;
    bool nt_stores = false;
    bool stage = false;
    double auto_phi = 0.55;
    double auto_sigma = 2.0;
    bool auto_measure = false;
    bool auto_cache_aware = false;
    double auto_phi_cache = 1.0;
    double auto_sigma_cache = 2.0;
    bool check = true;
};

[[noreturn]] void usage(const char* prog) {
    std::cerr
        << "Usage: " << prog << " [options]\n"
        << "Table and layouts:\n"
        << "  --rows R           number of rows (default 1000000)\n"
        << "  --cols N           number of int64 columns (default 16)\n"
        << "  --dist D           data distribution (default uniform); one of";
    for (Distribution d : all_distributions()) std::cerr << ' ' << distribution_name(d);
    std::cerr
        << "\n"
        << "  --domain D         uniform data is uniform in [0, D) (default 100)\n"
        << "  --run-length L     mean run length for --dist runs (default 64)\n"
        << "  --layout L1,L2     uncompressed layouts: nsm,dsm,pax or none (default all)\n"
        << "  --page-size B,...  PAX page size(s) in bytes, multiples of 64 (default 16384);\n"
        << "                     several sizes build one PAX layout each\n"
        << "  --pax-pad B        padding after each PAX minipage, bytes (default 0), or 'auto':\n"
        << "                     where more minipages of a page would share an L1 set than\n"
        << "                     it has ways, size them to an odd number of cache lines\n"
        << "  --dsm-offset B     DSM column c starts c*B bytes (mod L2 set stride) into its\n"
        << "                     allocation; 'auto' = 17 cache lines (default 0)\n"
        << "  --hugepages        back large buffers with transparent huge pages\n"
        << "  --simd             AVX2 selection vectors (late strategies) and AVX2\n"
        << "                     bit-unpacking (encoded layouts, widths up to 25 bits)\n"
        << "  --stage            projection builds each vector's tuples in a cache-resident\n"
        << "                     buffer, then copies them to the output in order\n"
        << "  --nt-stores        ... and copies with non-temporal stores (implies --stage)\n"
        << "  --encoding E1,E2   also run DSM with each column encoded; 'all' or any of";
    for (Encoding e : all_encodings()) std::cerr << ' ' << encoding_name(e);
    std::cerr
        << "\n"
        << "  --mode M1,M2       scan modes for encoded layouts: value,vector (default both)\n"
        << "Queries:\n"
        << "  --query Q1,Q2      queries to run (default: all of";
    for (const auto& q : kQueries) std::cerr << ' ' << q.name;
    std::cerr
        << ")\n"
        << "  --k K1,K2,...      number of leading columns each query reads\n"
        << "                     (default 1,2,4,...,N)\n"
        << "  --sel S1,S2,...    add the predicate c<P> < x, x chosen so a fraction S of rows\n"
        << "                     qualifies; one run per S (column P from --pred-col)\n"
        << "  --pred-col P       column of the --sel predicate (default 0)\n"
        << "  --where W          fixed predicates, e.g. 'c1<500&c2=7&c3=10..20' (ANDed)\n"
        << "  --strategy S1,S2   execution strategies (default: all but auto):";
    for (Strategy s : all_strategies()) std::cerr << ' ' << strategy_name(s);
    std::cerr
        << "\n"
        << "  --auto-phi F       auto strategy: late while the gathered columns' touched cache\n"
        << "                     lines stay below F * (1 - sigma / gathered) (default 0.55)\n"
        << "  --auto-sigma S     auto strategy: late's fixed cost, in column scans (default 2;\n"
        << "                     see late_threshold() in machine.h)\n"
        << "  --auto-cache       auto strategy: if the table fits in L3, use the in-cache\n"
        << "                     constants below instead of --auto-phi/--auto-sigma\n"
        << "  --auto-cache-phi F, --auto-cache-sigma S\n"
        << "                     in-cache threshold constants (default 1.0, 2)\n"
        << "  --auto-measure     auto strategy: time every span of >= 1024 rows and run\n"
        << "                     whichever of early and late is cheaper per row at the\n"
        << "                     current selectivity\n"
        << "                     (the threshold only decides until both are measured)\n"
        << "  --batch B1,B2      rows per vector, multiples of 64, or 'auto' to size it from\n"
        << "                     the L2 size (default 1024)\n"
        << "Measurement:\n"
        << "  --iters N          timed runs per configuration (default 10)\n"
        << "  --warmup N         untimed runs before them (default 2)\n"
        << "  --rounds R         repeat the whole benchmark R times, interleaving every\n"
        << "                     configuration, so slow periods hit all of them (default 1);\n"
        << "                     each round does --warmup + --iters runs per configuration\n"
        << "  --min-time-ms T    repeat run() inside each timed sample until it lasts T ms\n"
        << "                     (for small, cache-resident tables; default 0 = once)\n"
        << "  --perf SET         also record hardware counters per run; SET is one of";
    for (const auto& s : PerfCounters::set_names()) std::cerr << ' ' << s;
    std::cerr << "\n"
              << "  --no-check         skip the cross-layout correctness check\n"
              << "  --output FILE      write CSV to FILE instead of stdout\n"
              << "  --machine          print the detected cache hierarchy and exit\n";
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

[[noreturn]] void fail(const std::string& msg, const char* prog) {
    std::cerr << "error: " << msg << "\n";
    usage(prog);
}

Options parse_args(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        // Flags without a value.
        if (arg == "--hugepages") {
            o.hugepages = true;
            continue;
        } else if (arg == "--no-check") {
            o.check = false;
            continue;
        } else if (arg == "--simd") {
            o.simd = true;
            continue;
        } else if (arg == "--auto-measure") {
            o.auto_measure = true;
            continue;
        } else if (arg == "--auto-cache") {
            o.auto_cache_aware = true;
            continue;
        } else if (arg == "--nt-stores") {
            o.nt_stores = true;
            continue;
        } else if (arg == "--stage") {
            o.stage = true;
            continue;
        } else if (arg == "--machine") {
            std::cout << MachineInfo::get().describe();
            std::exit(0);
        }
        if (i + 1 >= argc) usage(argv[0]);
        std::string val = argv[++i];
        if (arg == "--rows") o.rows = std::stoull(val);
        else if (arg == "--cols") o.cols = std::stoull(val);
        else if (arg == "--page-size") {
            o.page_sizes.clear();
            for (const auto& b : split_commas(val)) o.page_sizes.push_back(std::stoull(b));
        } else if (arg == "--pax-pad") {
            o.pax_pad = val == "auto" ? PaxLayout::kOddStride : std::stoull(val);
        } else if (arg == "--k") {
            for (const auto& k : split_commas(val)) o.ks.push_back(std::stoi(k));
        } else if (arg == "--query") {
            for (const auto& name : split_commas(val)) {
                const QueryEntry* q = find_query(name);
                if (q == nullptr) fail("unknown query '" + name + "'", argv[0]);
                o.queries.push_back(q);
            }
        } else if (arg == "--output") o.output = val;
        else if (arg == "--perf") o.perf_set = val;
        else if (arg == "--dist") {
            if (!parse_distribution(val, o.dist)) fail("unknown distribution '" + val + "'", argv[0]);
        } else if (arg == "--run-length") o.run_length = std::stoull(val);
        else if (arg == "--domain") o.domain = std::stoll(val);
        else if (arg == "--layout") {
            o.nsm = o.dsm = o.pax = false;
            for (const auto& name : split_commas(val)) {
                if (name == "nsm") o.nsm = true;
                else if (name == "dsm") o.dsm = true;
                else if (name == "pax") o.pax = true;
                else if (name != "none") fail("unknown layout '" + name + "'", argv[0]);
            }
        } else if (arg == "--encoding") {
            o.encodings.clear();
            if (val == "all") {
                o.encodings = all_encodings();
            } else {
                for (const auto& name : split_commas(val)) {
                    Encoding e;
                    if (!parse_encoding(name, e)) fail("unknown encoding '" + name + "'", argv[0]);
                    o.encodings.push_back(e);
                }
            }
        } else if (arg == "--mode") {
            o.modes.clear();
            for (const auto& name : split_commas(val)) {
                ScanMode m;
                if (!parse_scan_mode(name, m)) fail("unknown mode '" + name + "'", argv[0]);
                o.modes.push_back(m);
            }
        } else if (arg == "--strategy") {
            o.strategies.clear();
            for (const auto& name : split_commas(val)) {
                Strategy s;
                if (!parse_strategy(name, s)) fail("unknown strategy '" + name + "'", argv[0]);
                o.strategies.push_back(s);
            }
        } else if (arg == "--batch") {
            o.batches.clear();
            for (const auto& b : split_commas(val)) {
                const std::size_t v = b == "auto" ? 0 : std::stoull(b);
                if (v % 64 != 0 || (b != "auto" && v == 0))
                    fail("batch sizes must be positive multiples of 64", argv[0]);
                o.batches.push_back(v);
            }
        } else if (arg == "--sel") {
            for (const auto& s : split_commas(val)) o.sels.push_back(std::stod(s));
        } else if (arg == "--pred-col") o.pred_col = std::stoi(val);
        else if (arg == "--where") {
            std::string err;
            if (!parse_where(val, o.where, err)) fail(err, argv[0]);
        } else if (arg == "--iters") o.iters = std::stoi(val);
        else if (arg == "--warmup") o.warmup = std::stoi(val);
        else if (arg == "--rounds") o.rounds = std::stoi(val);
        else if (arg == "--min-time-ms") o.min_time_ms = std::stod(val);
        else if (arg == "--dsm-offset") o.dsm_offset = val;
        else if (arg == "--auto-phi") o.auto_phi = std::stod(val);
        else if (arg == "--auto-sigma") o.auto_sigma = std::stod(val);
        else if (arg == "--auto-cache-phi") o.auto_phi_cache = std::stod(val);
        else if (arg == "--auto-cache-sigma") o.auto_sigma_cache = std::stod(val);
        else usage(argv[0]);
    }
    if (o.rows == 0 || o.cols == 0) usage(argv[0]);
    if (!o.nsm && !o.dsm && !o.pax && (o.encodings.empty() || o.modes.empty()))
        fail("no layouts selected", argv[0]);
    if (o.ks.empty()) {
        for (std::size_t k = 1; k < o.cols; k *= 2) o.ks.push_back(static_cast<int>(k));
        o.ks.push_back(static_cast<int>(o.cols));
    }
    if (o.queries.empty())
        for (const auto& q : kQueries) o.queries.push_back(&q);
    if (o.strategies.empty())  // every fixed strategy; auto only when asked for
        for (Strategy s : all_strategies())
            if (s != Strategy::Auto) o.strategies.push_back(s);
    if (o.batches.empty()) o.batches.push_back(1024);
    if (o.iters < 1 || o.warmup < 0 || o.rounds < 1)
        fail("--iters and --rounds must be >= 1, --warmup >= 0", argv[0]);
    for (int k : o.ks)
        if (k < 1 || static_cast<std::size_t>(k) > o.cols)
            fail("k=" + std::to_string(k) + " outside [1, " + std::to_string(o.cols) + "]", argv[0]);
    auto check_col = [&](int c) {
        if (c < 0 || static_cast<std::size_t>(c) >= o.cols)
            fail("predicate column c" + std::to_string(c) + " does not exist", argv[0]);
    };
    if (!o.sels.empty()) check_col(o.pred_col);
    for (const auto& p : o.where) check_col(p.col);
    for (double s : o.sels)
        if (s < 0 || s > 1) fail("selectivities must be in [0, 1]", argv[0]);
    return o;
}

// Columns a query with parameter k reads: the first k.
std::vector<int> first_k_columns(int k) {
    std::vector<int> cols(k);
    for (int c = 0; c < k; ++c) cols[c] = c;
    return cols;
}

// Predicate `c < x` on column `col` that a fraction `sel` of the rows passes (as nearly as
// ties allow): x is the sel-quantile of the column. sel = 1 keeps every row (c <= max).
Predicate predicate_for_selectivity(const Table& t, int col, double sel) {
    std::vector<int64_t> v(t.rows);
    for (std::size_t r = 0; r < t.rows; ++r) v[r] = t.at(r, col);
    std::sort(v.begin(), v.end());
    Predicate p;
    p.col = col;
    p.lo = std::numeric_limits<int64_t>::min();
    const std::size_t idx = static_cast<std::size_t>(std::llround(sel * static_cast<double>(t.rows)));
    if (idx >= t.rows) p.hi = v.back();
    else p.hi = v[idx] == p.lo ? p.lo : v[idx] - 1;  // (an INT64_MIN quantile keeps those rows)
    return p;
}

// Strategies worth running: without a WHERE clause early-pipe equals early and late-branch
// equals late, so those duplicates are dropped.
std::vector<Strategy> effective_strategies(const std::vector<Strategy>& wanted, bool has_where) {
    std::vector<Strategy> out;
    for (Strategy s : wanted) {
        Strategy eff = s;
        if (!has_where && s == Strategy::EarlyPipe) eff = Strategy::Early;
        if (!has_where && s == Strategy::LateBranch) eff = Strategy::Late;
        if (std::find(out.begin(), out.end(), eff) == out.end()) out.push_back(eff);
    }
    return out;
}

std::size_t resolve_batch(std::size_t batch, const QueryEntry& qe, const QueryParams& p) {
    if (batch != 0) return batch;
    std::vector<int> refs = p.columns;
    for (const auto& pr : p.where) refs.push_back(pr.col);
    std::sort(refs.begin(), refs.end());
    refs.erase(std::unique(refs.begin(), refs.end()), refs.end());
    const std::size_t bytes = 4 + 8 * refs.size() + (qe.writes_tuples ? 8 * p.columns.size() : 0);
    return auto_batch(MachineInfo::get(), bytes);
}

std::string fmt_sel(double s) {
    std::ostringstream o;
    o << std::setprecision(6) << s;
    return o.str();
}

}  // namespace

int main(int argc, char** argv) {
    Options opt = parse_args(argc, argv);
    const MachineInfo& machine = MachineInfo::get();
    alloc_policy().hugepages = opt.hugepages;
    bits::set_simd(opt.simd);  // AVX2 bit-unpacking for the encoded layouts

    std::size_t dsm_offset = 0;
    if (opt.dsm_offset == "auto") dsm_offset = auto_dsm_offset(machine);
    else dsm_offset = std::stoull(opt.dsm_offset);

    // --- Setup (untimed): generate data once, build every layout from it. ---
    std::vector<std::unique_ptr<Layout>> layouts;
    struct WhereSet {
        std::vector<Predicate> preds;
    };
    std::vector<WhereSet> where_sets;

    // Every configuration the benchmark runs: (query, k, WHERE clause, batch size).
    struct Config {
        const QueryEntry* qe;
        int k;
        std::size_t where;  // index into where_sets
        std::size_t batch;  // resolved
        int64_t expected;   // reference result
    };
    std::vector<Config> configs;
    auto params_for = [&](const Config& c, Strategy s) {
        QueryParams p;
        p.columns = first_k_columns(c.k);
        p.where = where_sets[c.where].preds;
        p.strategy = s;
        p.batch = c.batch;
        p.rows = opt.rows;
        p.cols = opt.cols;
        p.simd = opt.simd;
        p.nt_stores = opt.nt_stores;
        p.stage = opt.stage;
        p.auto_phi = opt.auto_phi;
        p.auto_sigma = opt.auto_sigma;
        p.auto_measure = opt.auto_measure;
        p.auto_cache_aware = opt.auto_cache_aware;
        p.auto_phi_cache = opt.auto_phi_cache;
        p.auto_sigma_cache = opt.auto_sigma_cache;
        return p;
    };
    try {
        Table table =
            generate_table(opt.rows, opt.cols, kSeed, opt.dist, opt.run_length, opt.domain);
        if (opt.sels.empty()) {
            where_sets.push_back({opt.where});
        } else {
            for (double s : opt.sels) {
                WhereSet w{{predicate_for_selectivity(table, opt.pred_col, s)}};
                w.preds.insert(w.preds.end(), opt.where.begin(), opt.where.end());
                where_sets.push_back(w);
            }
        }
        for (const QueryEntry* qe : opt.queries)
            for (int k : opt.ks)
                for (std::size_t w = 0; w < where_sets.size(); ++w)
                    for (std::size_t b : opt.batches) {
                        Config c{qe, k, w, 0, 0};
                        c.batch = resolve_batch(b, *qe, params_for(c, Strategy::Late));
                        if (opt.check) c.expected = qe->reference(table, params_for(c, Strategy::Late));
                        configs.push_back(c);
                    }

        if (opt.nsm) layouts.push_back(std::make_unique<NsmLayout>(table));
        if (opt.dsm)
            layouts.push_back(
                std::make_unique<DsmLayout>(table, dsm_offset, machine.l2.set_stride()));
        if (opt.pax)
            for (std::size_t ps : opt.page_sizes)
                layouts.push_back(std::make_unique<PaxLayout>(table, ps, opt.pax_pad,
                                                              machine.l1d.set_stride(),
                                                              machine.l1d.ways));
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
    std::cerr << machine.describe();
    std::cerr << "Table: " << opt.rows << " rows x " << opt.cols << " cols, "
              << (opt.rows * opt.cols * sizeof(int64_t)) / mb << " MB of data, "
              << distribution_name(opt.dist) << " distribution"
              << (opt.hugepages ? ", huge pages" : "") << ", DSM column offset step "
              << dsm_offset << " B\n";
    if (opt.hugepages) {
        // Confirms the kernel really backed the buffers with huge pages.
        std::ifstream smaps("/proc/self/smaps_rollup");
        std::string line;
        while (std::getline(smaps, line))
            if (line.rfind("AnonHugePages:", 0) == 0) std::cerr << "  " << line << "\n";
    }
    for (const auto& l : layouts) {
        std::cerr << "  " << l->name() << " footprint: " << l->footprint_bytes() / mb << " MB";
        if (auto* p = dynamic_cast<const PaxLayout*>(l.get()))
            std::cerr << " (page " << p->page_size() << " B, " << p->tuples_per_page()
                      << " tuples/page)";
        std::cerr << "\n";
    }

    auto page_size_of = [](const Layout& l) -> std::size_t {
        // page_size is only meaningful for PAX; report 0 for the other layouts.
        auto* p = dynamic_cast<const PaxLayout*>(&l);
        return p ? p->page_size() : 0;
    };
    auto label = [&](const Layout& l, Strategy s) {
        std::string n = l.name();
        if (std::size_t ps = page_size_of(l)) n += "(" + std::to_string(ps) + ")";
        return n + "/" + strategy_name(s);
    };


    // --- Correctness: every layout and strategy must produce the same result. ---
    // Each run gets a fresh query so it cannot pass by reusing another run's output.
    // The reference is a naive loop over the logical table, independent of every layout.
    std::vector<double> selectivity(configs.size(), 1.0);
    for (std::size_t ci = 0; ci < configs.size(); ++ci) {
        const Config& c = configs[ci];
        const auto& preds = where_sets[c.where].preds;
        const auto strategies = effective_strategies(opt.strategies, !preds.empty());
        bool first = true;
        for (const auto& l : layouts) {
            for (Strategy s : strategies) {
                if (!l->supports(s)) continue;
                if (!first && !opt.check) continue;
                auto q = c.qe->make(params_for(c, s));
                l->run(*q);
                if (first) selectivity[ci] = static_cast<double>(q->rows_out()) / opt.rows;
                first = false;
                if (opt.check && q->result() != c.expected) {
                    std::cerr << "error: " << c.qe->name << " mismatch for k=" << c.k
                              << " where=" << where_str(preds) << " batch=" << c.batch << ": "
                              << label(*l, s) << "=" << q->result()
                              << ", reference=" << c.expected << "\n";
                    return 1;
                }
            }
        }
    }
    std::cerr << (opt.check ? "Correctness check passed for " : "Correctness check skipped; ")
              << configs.size() << " configurations\n";

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
    out << "query,layout,strategy,dist,rows,cols,k,where,sel,page_size,batch,knobs,"
           "footprint_bytes,iteration,latency_ns,rows_out";
    if (counters)
        for (const auto& name : counters->names()) out << ',' << name;
    out << '\n';

    const std::string dist = distribution_name(opt.dist);
    // Tuning knobs in effect, e.g. "off1088+huge+simd" ("-" for none).
    std::string knobs;
    auto add_knob = [&](const std::string& k) { knobs += (knobs.empty() ? "" : "+") + k; };
    if (dsm_offset) add_knob("off" + std::to_string(dsm_offset));
    if (opt.pax_pad == PaxLayout::kOddStride) add_knob("oddstride");
    else if (opt.pax_pad) add_knob("pad" + std::to_string(opt.pax_pad));
    if (opt.hugepages) add_knob("huge");
    if (opt.simd) add_knob("simd");
    if (opt.auto_measure) add_knob("measure");
    if (opt.auto_cache_aware) add_knob("autocache");
    if (opt.nt_stores) add_knob("nt");
    else if (opt.stage) add_knob("stage");
    if (knobs.empty()) knobs = "-";
    const double min_ns = opt.min_time_ms * 1e6;
    for (int round = 0; round < opt.rounds; ++round)
    for (std::size_t ci = 0; ci < configs.size(); ++ci) {
        const Config& c = configs[ci];
        const auto& preds = where_sets[c.where].preds;
        const auto strategies = effective_strategies(opt.strategies, !preds.empty());
        auto q = c.qe->make(params_for(c, strategies.front()));  // untimed setup
        const std::string where = where_str(preds), sel = fmt_sel(selectivity[ci]);
        for (const auto& l : layouts) {
            for (Strategy s : strategies) {
                if (!l->supports(s)) continue;
                q->set_strategy(s);

                for (int i = 0; i < opt.warmup; ++i) {
                    l->run(*q);
                    g_sink = g_sink + static_cast<int64_t>(q->rows_out());
                }
                // Small tables finish in microseconds: repeat run() `inner` times per sample
                // so each sample lasts at least min_ns, and report the mean per run.
                long inner = 1;
                if (min_ns > 0) {
                    auto t0 = std::chrono::steady_clock::now();
                    l->run(*q);
                    auto t1 = std::chrono::steady_clock::now();
                    const double once =
                        std::max<double>(1.0, std::chrono::duration<double, std::nano>(t1 - t0).count());
                    inner = std::clamp<long>(static_cast<long>(std::ceil(min_ns / once)), 1, 1'000'000);
                }

                for (int i = 0; i < opt.iters; ++i) {
                    // Counters wrap the clock reads so their ioctls stay out of latency_ns.
                    if (counters) counters->start();
                    auto start = std::chrono::steady_clock::now();
                    for (long r = 0; r < inner; ++r) l->run(*q);
                    auto end = std::chrono::steady_clock::now();
                    if (counters) counters->stop();
                    g_sink = g_sink + static_cast<int64_t>(q->rows_out());
                    const auto ns =
                        std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count() /
                        inner;
                    out << q->name() << ',' << l->name() << ',' << strategy_name(s) << ',' << dist
                        << ',' << opt.rows << ',' << opt.cols << ',' << c.k << ',' << where << ','
                        << sel << ',' << page_size_of(*l) << ',' << c.batch << ',' << knobs
                        << ',' << l->footprint_bytes() << ',' << round * opt.iters + i << ',' << ns
                        << ',' << q->rows_out();
                    if (counters)
                        for (uint64_t v : counters->values()) out << ',' << v / inner;
                    out << '\n';
                }
            }
        }
        out.flush();
    }
    if (counters && counters->multiplexed())
        std::cerr << "warning: perf counters were multiplexed; counts are scaled estimates\n";
    return 0;
}
