#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "distributions.h"
#include "exec_options.h"
#include "layout.h"
#include "layout_factory.h"
#include "perf_counters.h"
#include "query_spec.h"
#include "reference.h"
#include "table.h"
#include "util.h"

namespace {

constexpr uint64_t kSeed = 42;
constexpr long kMaxReps = 1'000'000;

// Every query result is folded into this so the compiler cannot drop the work.
volatile int64_t g_sink = 0;

struct Options {
    std::size_t rows = 0;        // 0 -> derived from size_bytes, else 1000000
    std::size_t size_bytes = 0;  // requested table size; rows = size / (cols * 8)
    std::size_t cols = 16;
    std::size_t page_size = 16 * 1024;  // default PAX page when a pax spec gives none
    std::vector<std::string> layout_texts = {"nsm", "dsm", "pax"};
    DistSpec dists;

    // Queries: explicit --query specs, and/or the legacy --op x --where x --k cross product.
    std::vector<std::string> query_texts;
    std::vector<int> ks;
    std::vector<std::string> where_texts;
    std::vector<Op> ops = {Op::Sum};
    bool legacy_flags = false;

    // Execution options to sweep (only where they change anything, see variants()).
    std::vector<std::size_t> dsm_batches = {1024};
    std::vector<FilterMode> filters = {FilterMode::Branchy};
    int threads = 1;

    std::vector<std::string> modes = {"warm"};
    int warmup = 2;
    int iterations = 10;
    double min_sample_ms = 20.0;
    std::string output;  // empty -> stdout
};

[[noreturn]] void usage(const char* prog) {
    std::cerr
        << "Usage: " << prog << " [options]\n"
        << "Table\n"
        << "  --rows R            number of rows (default 1000000)\n"
        << "  --size BYTES        table size instead of --rows, suffix K/M/G allowed (e.g. 64M);\n"
        << "                      rows = BYTES / (cols * 8), rounded down\n"
        << "  --cols N            number of int64 columns (default 16)\n"
        << "  --dist T=SPEC       value distribution; T is 'default' or a column cN; repeatable.\n"
        << "                      SPEC: uniform[:lo=0:hi=99] | sorted[:lo=..:hi=..] |\n"
        << "                            clustered[:run=1024:lo=..:hi=..]\n"
        << "Layouts\n"
        << "  --layout L1,L2,...  layouts to build: nsm, dsm, pax[:page=BYTES] (default all)\n"
        << "  --page-size B       default PAX page size, multiple of 64 (default 16384)\n"
        << "Queries\n"
        << "  --query SPEC        one query, repeatable: op=sum|materialize;cols=all|0-3|0,4,8;\n"
        << "                      where=none|c0<5,c12<50[;label=NAME]\n"
        << "                      or op=lookup;cols=...;n=COUNT[;dist=uniform|sorted][;seed=S]\n"
        << "  Legacy query flags (cross product, used when given or when no --query is set):\n"
        << "  --op O1,O2          sum and/or materialize (default sum)\n"
        << "  --where PREDS       filter, repeatable: comma-separated conjunction of c3=42,\n"
        << "                      c0<10, c0<=10, c0>90, c0>=90, c2=10..19, or 'none'\n"
        << "  --k K1,K2,...       project columns [0, k) (default 1,2,4,...,N)\n"
        << "Execution options (swept only where they matter)\n"
        << "  --dsm-batch B1,...  DSM batch rows for filters (default 1024; K/M suffix allowed)\n"
        << "  --filter F1,F2      branchy and/or branchless predicate evaluation (default branchy)\n"
        << "  --threads N         reserved; only 1 is supported\n"
        << "Measurement\n"
        << "  --modes M1,M2       warm and/or cold (default warm). cold flushes the table\n"
        << "                      from all caches before every timed query\n"
        << "  --warmup W          untimed runs before measuring each configuration (default 2)\n"
        << "  --iterations I      timed samples per configuration (default 10)\n"
        << "  --min-sample-ms T   warm mode repeats the query until a sample lasts >= T ms\n"
        << "                      (default 20)\n"
        << "  --output FILE       write CSV to FILE instead of stdout\n";
    std::exit(1);
}

[[noreturn]] void fail(const std::string& msg) {
    std::cerr << "error: " << msg << "\n";
    std::exit(1);
}

Op parse_op_name(const std::string& s) {
    if (s == "sum") return Op::Sum;
    if (s == "materialize") return Op::Materialize;
    fail("unknown op '" + s + "' (sum, materialize)");
}

Options parse_args(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (i + 1 >= argc) usage(argv[0]);
        std::string val = argv[++i];
        try {
            if (arg == "--rows") o.rows = static_cast<std::size_t>(parse_int(val, "rows"));
            else if (arg == "--size") o.size_bytes = parse_size(val);
            else if (arg == "--cols") o.cols = static_cast<std::size_t>(parse_int(val, "cols"));
            else if (arg == "--page-size") o.page_size = parse_size(val);
            else if (arg == "--layout") o.layout_texts = split(val, ',');
            else if (arg == "--dist") o.dists.add(val);
            else if (arg == "--query") o.query_texts.push_back(val);
            else if (arg == "--k") {
                o.ks.clear();
                for (const std::string& k : split(val, ','))
                    o.ks.push_back(static_cast<int>(parse_int(k, "k")));
                o.legacy_flags = true;
            } else if (arg == "--where") {
                o.where_texts.push_back(val);
                o.legacy_flags = true;
            } else if (arg == "--op") {
                o.ops.clear();
                for (const std::string& name : split(val, ',')) o.ops.push_back(parse_op_name(name));
                o.legacy_flags = true;
            } else if (arg == "--dsm-batch") {
                o.dsm_batches.clear();
                for (const std::string& b : split(val, ',')) o.dsm_batches.push_back(parse_size(b));
            } else if (arg == "--filter") {
                o.filters.clear();
                for (const std::string& f : split(val, ',')) {
                    if (f == "branchy") o.filters.push_back(FilterMode::Branchy);
                    else if (f == "branchless") o.filters.push_back(FilterMode::Branchless);
                    else fail("unknown filter mode '" + f + "' (branchy, branchless)");
                }
            } else if (arg == "--threads") o.threads = static_cast<int>(parse_int(val, "threads"));
            else if (arg == "--modes") o.modes = split(val, ',');
            else if (arg == "--warmup") o.warmup = static_cast<int>(parse_int(val, "warmup"));
            else if (arg == "--iterations") o.iterations = static_cast<int>(parse_int(val, "iterations"));
            else if (arg == "--min-sample-ms") o.min_sample_ms = std::stod(val);
            else if (arg == "--output") o.output = val;
            else usage(argv[0]);
        } catch (const std::invalid_argument& e) {
            fail(e.what());
        }
    }

    if (o.cols == 0) usage(argv[0]);
    if (o.rows != 0 && o.size_bytes != 0) fail("give --rows or --size, not both");
    if (o.size_bytes != 0) o.rows = o.size_bytes / (o.cols * sizeof(int64_t));
    else if (o.rows == 0) o.rows = 1'000'000;
    if (o.rows == 0) fail("--size is smaller than one row");
    if (o.threads != 1) fail("--threads: only 1 is supported for now");
    for (std::size_t b : o.dsm_batches)
        if (b == 0) fail("--dsm-batch values must be at least 1");
    for (const std::string& m : o.modes)
        if (m != "warm" && m != "cold") fail("unknown mode '" + m + "'");
    for (const auto& [c, spec] : o.dists.per_column)
        if (c < 0 || static_cast<std::size_t>(c) >= o.cols)
            fail("--dist names c" + std::to_string(c) + ", but the table has c0..c" +
                 std::to_string(o.cols - 1));
    return o;
}

// The queries to run: legacy cross product (if its flags were given, or nothing else was),
// followed by every --query.
std::vector<QuerySpec> build_queries(Options& o) {
    const int cols = static_cast<int>(o.cols);
    std::vector<QuerySpec> queries;
    try {
        if (o.legacy_flags || o.query_texts.empty()) {
            if (o.ks.empty()) {
                for (int k = 1; k < cols; k *= 2) o.ks.push_back(k);
                o.ks.push_back(cols);
            }
            for (int k : o.ks)
                if (k < 1 || k > cols)
                    fail("k=" + std::to_string(k) + " outside [1, " + std::to_string(cols) + "]");
            if (o.where_texts.empty()) o.where_texts.push_back("none");
            for (Op op : o.ops)
                for (const std::string& w : o.where_texts)
                    for (int k : o.ks) {
                        QuerySpec q;
                        q.op = op;
                        q.project = prefix_projection(k);
                        parse_where(w, cols, q);
                        queries.push_back(q);
                    }
        }
        for (const std::string& text : o.query_texts) {
            queries.push_back(parse_query_spec(text, cols));
        }
    } catch (const std::invalid_argument& e) {
        fail(e.what());
    }
    return queries;
}

// Runs one query on one layout. `ids` are the query's row ids (lookups only).
QueryResult run_query(const Layout& l, const QuerySpec& q, const RowIds& ids,
                      const ExecOptions& opts) {
    switch (q.op) {
        case Op::Sum:
            return q.where.empty() ? l.scan_sum(q.project) : l.filter_sum(q.where, q.project, opts);
        case Op::Materialize:
            return l.filter_materialize(q.where, q.project, opts);
        case Op::Lookup:
            return l.lookup(ids, q.project);
    }
    throw std::logic_error("unknown op");
}

// Rows a query processes: the table for scans, the number of lookups for lookups.
std::size_t items_of(const QuerySpec& q, std::size_t rows) {
    return q.op == Op::Lookup ? q.lookups.count : rows;
}

// One combination of execution options for a (layout, query). Options that cannot change the
// result's cost are not swept, and are reported as 0 / "none" in the CSV.
struct Variant {
    ExecOptions opts;
    bool batch_used;
    bool filter_used;
};

std::vector<Variant> variants(const Layout& l, const QuerySpec& q, const Options& o) {
    const bool batch_used = l.uses_batch_rows() && q.op != Op::Lookup &&
                            !(q.op == Op::Sum && q.where.empty());
    const bool filter_used = !q.where.empty();
    std::vector<std::size_t> batches =
        batch_used ? o.dsm_batches : std::vector<std::size_t>{o.dsm_batches.front()};
    std::vector<FilterMode> filters =
        filter_used ? o.filters : std::vector<FilterMode>{o.filters.front()};
    std::vector<Variant> out;
    for (std::size_t b : batches)
        for (FilterMode f : filters) {
            Variant v{ExecOptions{}, batch_used, filter_used};
            v.opts.dsm_batch = b;
            v.opts.filter = f;
            v.opts.threads = o.threads;
            out.push_back(v);
        }
    return out;
}

std::string params_label(const LayoutParams& p) {
    if (p.empty()) return "-";
    std::string s;
    for (const auto& [k, v] : p) s += (s.empty() ? "" : ":") + k + "=" + v;
    return s;
}

double now_ns() {
    return std::chrono::duration<double, std::nano>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

}  // namespace

int main(int argc, char** argv) {
    Options opt = parse_args(argc, argv);
    std::vector<QuerySpec> queries = build_queries(opt);

    std::vector<LayoutSpec> specs;
    try {
        for (const std::string& text : opt.layout_texts) {
            LayoutSpec spec = parse_layout_spec(text);
            if (spec.name == "pax" && !spec.params.count("page"))
                spec.params["page"] = std::to_string(opt.page_size);
            specs.push_back(spec);
        }
    } catch (const std::invalid_argument& e) {
        fail(e.what());
    }

    // --- Setup (untimed): generate data once, build the requested layouts, and compute the
    // expected results. The logical table is freed before measuring. ---
    std::vector<std::unique_ptr<Layout>> layouts;
    std::vector<QueryResult> expected(queries.size());
    std::vector<int64_t> qualifying(queries.size());
    std::vector<RowIds> ids(queries.size());  // lookups: generated once, reused by every run
    try {
        Table table = generate_table(opt.rows, opt.cols, kSeed, opt.dists);
        for (const LayoutSpec& spec : specs) layouts.push_back(make_layout(spec, table));
        std::map<std::string, ReferenceStats> stats;  // one table pass per distinct WHERE
        for (std::size_t q = 0; q < queries.size(); ++q) {
            if (queries[q].op == Op::Lookup) {
                ids[q] = lookup_ids(queries[q], opt.rows);
                expected[q] = reference_lookup(table, ids[q], queries[q].project);
                qualifying[q] = expected[q].count;
                continue;
            }
            const std::string& key = queries[q].where_label;
            if (!stats.count(key)) stats[key] = reference_stats(table, queries[q].where);
            expected[q] = reference_result(stats[key], queries[q].op, queries[q].project);
            qualifying[q] = stats[key].count;
        }
    } catch (const std::exception& e) {
        fail(e.what());
    }

    const std::size_t row_bytes = opt.cols * sizeof(int64_t);
    const std::size_t data_bytes = opt.rows * row_bytes;
    const double mb = 1024.0 * 1024.0;
    std::cerr << "Table: " << opt.rows << " rows x " << opt.cols << " cols, " << data_bytes / mb
              << " MB of data, dist " << opt.dists.label() << "\n";
    for (const auto& l : layouts)
        std::cerr << "  " << l->name() << " (" << params_label(l->params())
                  << ") footprint: " << l->footprint_bytes() / mb << " MB\n";

    // --- Correctness: every layout, query and execution option must match the reference. ---
    for (const auto& l : layouts)
        for (std::size_t q = 0; q < queries.size(); ++q)
            for (const Variant& v : variants(*l, queries[q], opt)) {
                QueryResult got = run_query(*l, queries[q], ids[q], v.opts);
                if (got != expected[q])
                    fail("mismatch for " + queries[q].label() + " on " + l->name() + " (" +
                         params_label(l->params()) + ", batch " +
                         std::to_string(v.opts.dsm_batch) + ", " +
                         filter_mode_name(v.opts.filter) + "): expected count=" +
                         std::to_string(expected[q].count) + " sum=" +
                         std::to_string(expected[q].sum) + ", got count=" +
                         std::to_string(got.count) + " sum=" + std::to_string(got.sum));
            }
    std::cerr << "Correctness check passed for " << queries.size() << " queries\n";
    for (std::size_t q = 0; q < queries.size(); ++q) {
        if (queries[q].op == Op::Lookup) {
            std::cerr << "  " << queries[q].label() << ": " << qualifying[q] << " lookups\n";
            continue;
        }
        std::cerr << "  " << queries[q].label() << ": " << qualifying[q] << " of " << opt.rows
                  << " rows qualify (" << 100.0 * static_cast<double>(qualifying[q]) / opt.rows
                  << "%)\n";
    }

    PerfCounters perf;
    std::cerr << "Hardware counters:";
    for (std::size_t i = 0; i < perf.size(); ++i)
        std::cerr << " " << perf.name(i) << (perf.available(i) ? "" : "(unavailable)");
    std::cerr << "\n";

    // --- Benchmark ---
    std::ofstream file;
    if (!opt.output.empty()) {
        file.open(opt.output);
        if (!file) fail("cannot open " + opt.output);
    }
    std::ostream& out = opt.output.empty() ? std::cout : file;
    out << "layout,layout_params,rows,cols,row_bytes,data_bytes,footprint_bytes,dist,query,op,"
           "project,k,predicate,n_items,page_size,dsm_batch,filter,threads,mode,iteration,reps,"
           "latency_ns,matches,selectivity,result";
    for (std::size_t i = 0; i < perf.size(); ++i) out << ',' << perf.name(i);
    out << '\n';
    out << std::fixed << std::setprecision(1);

    const double min_sample_ns = opt.min_sample_ms * 1e6;
    for (const auto& l : layouts) {
        const LayoutParams params = l->params();
        const std::string page = params.count("page") ? params.at("page") : "0";
        for (std::size_t qi = 0; qi < queries.size(); ++qi) {
            const QuerySpec& q = queries[qi];
            const RowIds& q_ids = ids[qi];
            const std::size_t n_items = items_of(q, opt.rows);
            for (const Variant& v : variants(*l, q, opt)) {
                for (const std::string& mode : opt.modes) {
                    const bool cold = mode == "cold";
                    for (int i = 0; i < opt.warmup; ++i)
                        g_sink = g_sink + run_query(*l, q, q_ids, v.opts).sum;

                    // Warm: repeat the query so each sample is long enough to time
                    // precisely; the data stays cached between repetitions if it fits.
                    // Cold: one query per sample, right after flushing the table from every
                    // cache level.
                    long reps = 1;
                    if (!cold) {
                        double t0 = now_ns();
                        g_sink = g_sink + run_query(*l, q, q_ids, v.opts).sum;
                        double one = std::max(now_ns() - t0, 1.0);
                        reps = std::clamp(static_cast<long>(std::ceil(min_sample_ns / one)), 1L,
                                          kMaxReps);
                    }

                    for (int it = 0; it < opt.iterations; ++it) {
                        if (cold) l->flush_cache();
                        QueryResult result;
                        perf.start();
                        double t0 = now_ns();
                        for (long r = 0; r < reps; ++r) {
                            result = run_query(*l, q, q_ids, v.opts);
                            g_sink = g_sink + result.sum;
                        }
                        double elapsed = now_ns() - t0;
                        perf.stop();

                        // Latency and counters are reported per query execution.
                        out << l->name() << ',' << params_label(params) << ',' << opt.rows
                            << ',' << opt.cols << ',' << row_bytes << ',' << data_bytes << ','
                            << l->footprint_bytes() << ',' << opt.dists.label() << ','
                            << q.label() << ',' << op_name(q.op) << ',' << q.project.label()
                            << ',' << q.project.size() << ',' << q.where_label << ','
                            << n_items << ',' << page << ','
                            << (v.batch_used ? v.opts.dsm_batch : 0) << ','
                            << (v.filter_used ? filter_mode_name(v.opts.filter) : "none") << ','
                            << v.opts.threads << ',' << mode << ',' << it << ',' << reps << ','
                            << elapsed / reps << ',' << result.count << ','
                            << std::setprecision(6)
                            << static_cast<double>(result.count) / n_items
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
    return 0;
}
