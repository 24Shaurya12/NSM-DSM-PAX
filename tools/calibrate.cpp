// Calibration micro-benchmarks for the layout advisor's hardware model (further_plan.md §4),
// in the spirit of the Calibrator of Manegold, Boncz & Kersten (VLDB 2002).
//
// Prints one JSON object with *raw* measurements; advisor/hardware.py turns them into hw.json
// (cache levels, bandwidths, latencies, TLB, MLP, over-fetch, CPU constants). Nothing here
// assumes a particular machine: region sizes span 4 KB .. --max-size, and the analysis finds
// the levels from the curves (sysfs only provides the nominal sizes to compare against).
// It never runs validation queries: the engine kernels below use small, in-cache tables that
// the ground truth does not contain.
//
// Run pinned to one core:  taskset -c <cpu> build/calibrate [--max-size 1G] [--min-sample-ms 20]
//                                                            [--only seq,chase,...]

#include <sys/mman.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <numeric>
#include <random>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include "layout.h"
#include "layout_factory.h"
#include "perf_counters.h"
#include "predicate.h"
#include "projection.h"
#include "table.h"
#include "util.h"

namespace {

volatile int64_t g_sink = 0;

double now_ns() {
    return std::chrono::duration<double, std::nano>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

// ------------------------------------------------------------------------------ memory

// Page-aligned anonymous memory; `huge` asks for transparent huge pages, otherwise they are
// explicitly disabled so "4 KB pages" means 4 KB pages even where THP is set to "always".
class Region {
public:
    Region(std::size_t bytes, bool huge) : bytes_(round_up(bytes, kHuge)) {
        void* p = mmap(nullptr, bytes_, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) throw std::runtime_error("mmap failed");
        madvise(p, bytes_, huge ? MADV_HUGEPAGE : MADV_NOHUGEPAGE);
        data_ = static_cast<int64_t*>(p);
        for (std::size_t i = 0; i < bytes_ / 8; i += 512) data_[i] = 0;  // fault pages in
    }
    ~Region() { munmap(data_, bytes_); }
    Region(const Region&) = delete;
    Region& operator=(const Region&) = delete;
    int64_t* data() const { return data_; }
    std::size_t bytes() const { return bytes_; }

private:
    static constexpr std::size_t kHuge = 2 << 20;
    static std::size_t round_up(std::size_t n, std::size_t a) { return (n + a - 1) / a * a; }
    std::size_t bytes_;
    int64_t* data_;
};

long anon_huge_kb() {
    std::ifstream f("/proc/self/smaps_rollup");
    std::string key;
    long v;
    while (f >> key) {
        if (key == "AnonHugePages:") {
            f >> v;
            return v;
        }
    }
    return -1;
}

// ------------------------------------------------------------------------------ measuring

struct Measured {
    double ns = 0;                        // per item
    std::map<std::string, double> ctr;    // per item, available counters only
};

class Bench {
public:
    explicit Bench(double min_sample_ms, int samples) : min_ns_(min_sample_ms * 1e6), samples_(samples) {}

    // `work` performs `items` items of work per call and returns a value to keep it alive.
    // Each sample repeats the call until it lasts min_sample_ms; the median sample is kept.
    Measured run(const std::function<int64_t()>& work, double items) {
        g_sink = g_sink + work();  // warm-up
        double t0 = now_ns();
        g_sink = g_sink + work();
        double one = std::max(now_ns() - t0, 1.0);
        long reps = std::max(1L, static_cast<long>(std::ceil(min_ns_ / one)));
        std::vector<Measured> all;
        for (int s = 0; s < samples_; ++s) {
            perf_.start();
            double a = now_ns();
            for (long r = 0; r < reps; ++r) g_sink = g_sink + work();
            double el = now_ns() - a;
            perf_.stop();
            Measured m;
            const double n = items * reps;
            m.ns = el / n;
            for (std::size_t i = 0; i < perf_.size(); ++i)
                if (perf_.available(i)) m.ctr[perf_.name(i)] = perf_.value(i) / n;
            all.push_back(m);
        }
        std::sort(all.begin(), all.end(), [](const Measured& x, const Measured& y) { return x.ns < y.ns; });
        return all[all.size() / 2];
    }

    std::vector<std::string> counters() const {
        std::vector<std::string> v;
        for (std::size_t i = 0; i < perf_.size(); ++i)
            if (perf_.available(i)) v.push_back(perf_.name(i));
        return v;
    }

private:
    double min_ns_;
    int samples_;
    PerfCounters perf_;
};

// ------------------------------------------------------------------------------ JSON output

class Json {
public:
    // A record is a flat object of numbers and strings.
    class Record {
    public:
        Record& num(const std::string& k, double v) {
            std::ostringstream o;
            if (std::isfinite(v)) o.precision(6), o << v;
            else o << "null";
            fields_.push_back("\"" + k + "\": " + o.str());
            return *this;
        }
        Record& str(const std::string& k, const std::string& v) {
            fields_.push_back("\"" + k + "\": \"" + v + "\"");
            return *this;
        }
        Record& measured(const Measured& m, const std::string& ns_key = "ns") {
            num(ns_key, m.ns);
            for (const auto& [k, v] : m.ctr) num(k, v);
            return *this;
        }
        std::string text() const {
            std::string s = "{";
            for (std::size_t i = 0; i < fields_.size(); ++i) s += (i ? ", " : "") + fields_[i];
            return s + "}";
        }

    private:
        std::vector<std::string> fields_;
    };

    Record& add(const std::string& section) {
        sections_[section].emplace_back();
        if (std::find(order_.begin(), order_.end(), section) == order_.end()) order_.push_back(section);
        return sections_[section].back();
    }
    void meta(const std::string& k, const std::string& json_value) { meta_.emplace_back(k, json_value); }

    void print(std::ostream& out) const {
        out << "{\n";
        for (const auto& [k, v] : meta_) out << "  \"" << k << "\": " << v << ",\n";
        for (std::size_t s = 0; s < order_.size(); ++s) {
            const auto& recs = sections_.at(order_[s]);
            out << "  \"" << order_[s] << "\": [\n";
            for (std::size_t i = 0; i < recs.size(); ++i)
                out << "    " << recs[i].text() << (i + 1 < recs.size() ? ",\n" : "\n");
            out << "  ]" << (s + 1 < order_.size() ? ",\n" : "\n");
        }
        out << "}\n";
    }

private:
    std::vector<std::pair<std::string, std::string>> meta_;
    std::map<std::string, std::vector<Record>> sections_;
    std::vector<std::string> order_;
};

// ------------------------------------------------------------------------------ kernels

// Region sizes from 4 KB to max: powers of two and 1.5x steps (to locate capacity knees).
std::vector<std::size_t> region_sizes(std::size_t max_bytes) {
    std::vector<std::size_t> v;
    for (std::size_t s = 4096; s <= max_bytes; s *= 2) {
        v.push_back(s);
        if (s * 3 / 2 <= max_bytes && s >= 8192) v.push_back(s * 3 / 2);
    }
    std::sort(v.begin(), v.end());
    return v;
}

// Sequential read bandwidth per region size: the region is read repeatedly, so it is served
// by the smallest level it fits in. Four accumulators so the add chain is not the limit.
void seq_read(Bench& b, Json& j, const Region& r, std::size_t max_bytes) {
    int64_t* d = r.data();
    for (std::size_t i = 0; i < max_bytes / 8; ++i) d[i] = static_cast<int64_t>(i & 1023);
    for (std::size_t bytes : region_sizes(max_bytes)) {
        const std::size_t n = bytes / 8;
        Measured m = b.run([d, n] {
            int64_t s0 = 0, s1 = 0, s2 = 0, s3 = 0;
            for (std::size_t i = 0; i < n; i += 4) s0 += d[i], s1 += d[i + 1], s2 += d[i + 2], s3 += d[i + 3];
            return s0 + s1 + s2 + s3;
        }, static_cast<double>(bytes));
        j.add("seq_read").num("bytes", bytes).measured(m, "ns_per_byte").num("gb_s", 1.0 / m.ns);
    }
}

// Builds a random cyclic chase over the given int64 positions: d[pos[k]] holds the next position.
void build_chase(int64_t* d, std::vector<std::size_t> pos, uint64_t seed) {
    std::mt19937_64 rng(seed);
    for (std::size_t i = pos.size() - 1; i > 0; --i) {  // Sattolo: one cycle through all
        std::uniform_int_distribution<std::size_t> u(0, i - 1);
        std::swap(pos[i], pos[u(rng)]);
    }
    for (std::size_t i = 0; i < pos.size(); ++i) d[pos[i]] = static_cast<int64_t>(pos[(i + 1) % pos.size()]);
}

Measured chase(Bench& b, int64_t* d, int64_t start, std::size_t steps) {
    int64_t p = start;
    return b.run([d, &p, steps] {
        for (std::size_t s = 0; s < steps; ++s) p = d[p];
        return p;
    }, static_cast<double>(steps));
}

// Dependent random loads, one per cache line, over regions of every size: latency per level.
void random_latency(Bench& b, Json& j, const Region& r, std::size_t max_bytes, const std::string& section) {
    int64_t* d = r.data();
    for (std::size_t bytes : region_sizes(max_bytes)) {
        std::vector<std::size_t> pos(bytes / 64);
        for (std::size_t i = 0; i < pos.size(); ++i) pos[i] = i * 8;
        build_chase(d, pos, 11 + bytes);
        Measured m = chase(b, d, 0, 1 << 16);
        j.add(section).num("bytes", bytes).measured(m);
    }
}

// TLB reach: one line per 4 KB page, pages visited in random order. The lines sit at different
// offsets within their pages so they spread over the cache sets; compare with the dense chase
// over the same number of lines (random_latency) to separate the TLB from the caches.
void tlb_chase(Bench& b, Json& j, const Region& r) {
    int64_t* d = r.data();
    const std::size_t max_pages = std::min<std::size_t>(r.bytes() / 4096, 1 << 16);
    std::vector<std::size_t> counts;
    for (std::size_t n = 4; n <= max_pages; n *= 2) {
        counts.push_back(n);
        if (n * 3 / 2 <= max_pages && n >= 8) counts.push_back(n * 3 / 2);
    }
    std::sort(counts.begin(), counts.end());
    for (std::size_t n : counts) {
        std::vector<std::size_t> pos(n);
        for (std::size_t i = 0; i < n; ++i) pos[i] = i * 512 + ((i * 7) % 64) * 8;
        build_chase(d, pos, 5 + n);
        Measured m = chase(b, d, static_cast<int64_t>(pos[0]), 1 << 16);
        j.add("tlb").num("pages", n).num("lines_bytes", n * 64.0).measured(m);
    }
}

// Memory-level parallelism: independent random line loads vs the dependent chase, same region.
void mlp(Bench& b, Json& j, const Region& r, std::size_t max_bytes, const std::string& page) {
    int64_t* d = r.data();
    for (std::size_t bytes : {std::size_t{4} << 20, std::size_t{64} << 20, max_bytes}) {
        if (bytes > max_bytes) continue;
        const std::size_t lines = bytes / 64, n = 1 << 20;
        std::vector<uint32_t> idx(n);
        std::mt19937_64 rng(3);
        for (auto& x : idx) x = static_cast<uint32_t>(rng() % lines);
        Measured ind = b.run([d, &idx] {
            int64_t s = 0;
            for (uint32_t x : idx) s += d[static_cast<std::size_t>(x) * 8];
            return s;
        }, static_cast<double>(n));
        std::vector<std::size_t> pos(lines);
        for (std::size_t i = 0; i < lines; ++i) pos[i] = i * 8;
        build_chase(d, pos, 17);
        Measured dep = chase(b, d, 0, 1 << 16);
        j.add("mlp").str("page", page).num("bytes", bytes).num("dependent_ns", dep.ns)
            .measured(ind, "independent_ns").num("mlp", dep.ns / ind.ns);
    }
}

// One 8-byte load every `stride` bytes over a DRAM-sized region: DRAM bytes per access show the
// effective transfer unit (a line, or a line plus its prefetched neighbour).
void strided(Bench& b, Json& j, const Region& r, std::size_t bytes) {
    int64_t* d = r.data();
    for (std::size_t stride = 64; stride <= 8192; stride *= 2) {
        const std::size_t step = stride / 8, n = bytes / stride;
        Measured m = b.run([d, step, n] {
            int64_t s = 0;
            for (std::size_t i = 0; i < n; ++i) s += d[i * step];
            return s;
        }, static_cast<double>(n));
        j.add("stride").num("stride", stride).measured(m);
    }
}

// Prefetch over-fetch: read a contiguous run of `run` bytes at offset `offset` of every
// `period`-byte block (PAX: a minipage of each page). DRAM bytes per run minus `run` is what the
// prefetchers fetch beyond the run.
void overfetch(Bench& b, Json& j, const Region& r, std::size_t bytes) {
    int64_t* d = r.data();
    for (std::size_t period = 512; period <= (4u << 20); period *= 2) {
        std::set<std::size_t> runs;
        for (std::size_t div : {64, 16, 4, 2})
            if (period / div >= 64) runs.insert(period / div);
        for (std::size_t run : runs) {
            std::vector<std::size_t> offsets = {0};
            if (period >= 8192 && run <= 2048) offsets.push_back(2048);  // mid-page start
            for (std::size_t off : offsets) {
                const std::size_t blocks = bytes / period, per = run / 8, pstep = period / 8,
                                  o = off / 8;
                Measured m = b.run([d, blocks, per, pstep, o] {
                    int64_t s0 = 0, s1 = 0;
                    for (std::size_t k = 0; k < blocks; ++k) {
                        const int64_t* p = d + k * pstep + o;
                        for (std::size_t i = 0; i < per; i += 2) s0 += p[i], s1 += p[i + 1];
                    }
                    return s0 + s1;
                }, static_cast<double>(blocks));
                j.add("overfetch").num("period", period).num("run", run).num("offset", off)
                    .measured(m, "ns_per_run");
            }
        }
    }
}

// Engine kernels on small in-cache tables: CPU cost per value of the engine's own loops
// (scan, predicate, selection, gather, copy), branch mispredictions, per-batch and per-page
// overheads. advisor/hardware.py derives the constants from differences between these.
void engine_kernels(Bench& b, Json& j) {
    struct Kernel {
        std::string name;
        std::size_t rows, cols;
        std::string layout, op, where;
        int k;
        std::size_t batch;
        FilterMode filter;
    };
    const FilterMode BY = FilterMode::Branchy, BL = FilterMode::Branchless;
    const std::vector<Kernel> kernels = {
        // DSM, one column
        {"dsm_scan_l1", 2048, 1, "dsm", "sum", "", 1, 1024, BY},
        {"dsm_scan_l2", 65536, 1, "dsm", "sum", "", 1, 1024, BY},
        {"dsm_pred0_branchy", 65536, 1, "dsm", "sum", "c0<0", 1, 1024, BY},
        {"dsm_pred0_branchless", 65536, 1, "dsm", "sum", "c0<0", 1, 1024, BL},
        {"dsm_pred100_branchy", 65536, 1, "dsm", "sum", "c0>=0", 1, 1024, BY},
        {"dsm_pred100_branchless", 65536, 1, "dsm", "sum", "c0>=0", 1, 1024, BL},
        {"dsm_pred0_batch8", 65536, 1, "dsm", "sum", "c0<0", 1, 8, BY},
        {"dsm_pred0_batch64", 65536, 1, "dsm", "sum", "c0<0", 1, 64, BY},
        {"dsm_mat100_k1", 65536, 1, "dsm", "materialize", "c0>=0", 1, 1024, BY},
        // Branch mispredictions: 8 MB of random values, too many for the predictor to learn
        {"dsm_pred0_big", 1 << 20, 1, "dsm", "sum", "c0<0", 1, 1024, BY},
        {"dsm_pred50_big", 1 << 20, 1, "dsm", "sum", "c0<50", 1, 1024, BY},
        {"dsm_pred10_big", 1 << 20, 1, "dsm", "sum", "c0<10", 1, 1024, BY},
        {"dsm_pred50_big_branchless", 1 << 20, 1, "dsm", "sum", "c0<50", 1, 1024, BL},
        // 16 columns, 512 KB: gather from k columns, NSM row work, PAX page overheads
        {"dsm_mat100_k16", 4096, 16, "dsm", "materialize", "c0>=0", 16, 1024, BY},
        {"dsm_sum100_k16", 4096, 16, "dsm", "sum", "c0>=0", 16, 1024, BY},
        {"nsm_scan_k1", 4096, 16, "nsm", "sum", "", 1, 0, BY},
        {"nsm_scan_k16", 4096, 16, "nsm", "sum", "", 16, 0, BY},
        {"nsm_pred0", 4096, 16, "nsm", "sum", "c0<0", 1, 0, BY},
        {"nsm_mat100_k16", 4096, 16, "nsm", "materialize", "c0>=0", 16, 0, BY},
        {"nsm_mat100_k1", 4096, 16, "nsm", "materialize", "c0>=0", 1, 0, BY},
    };
    std::vector<Kernel> all = kernels;
    for (std::size_t page : {1024, 4096, 16384, 65536}) {
        const std::string p = "pax:page=" + std::to_string(page), s = std::to_string(page);
        all.push_back({"pax_scan_k1_" + s, 4096, 16, p, "sum", "", 1, 0, BY});
        all.push_back({"pax_pred0_" + s, 4096, 16, p, "sum", "c0<0", 1, 0, BY});
        all.push_back({"pax_mat100_k16_" + s, 4096, 16, p, "materialize", "c0>=0", 16, 0, BY});
    }
    std::map<std::pair<std::size_t, std::size_t>, Table> tables;
    for (const Kernel& k : all) {
        auto key = std::make_pair(k.rows, k.cols);
        if (!tables.count(key)) tables[key] = generate_table(k.rows, k.cols, 99);
        auto layout = make_layout(parse_layout_spec(k.layout), tables[key]);
        Conjunction where = k.where.empty() ? Conjunction{} : parse_conjunction(k.where);
        Projection proj = prefix_projection(k.k);
        ExecOptions o;
        o.dsm_batch = k.batch ? k.batch : 1024;
        o.filter = k.filter;
        const Layout& l = *layout;
        std::function<int64_t()> work;
        if (k.op == "sum" && where.empty()) work = [&] { return l.scan_sum(proj).sum; };
        else if (k.op == "sum") work = [&] { return l.filter_sum(where, proj, o).sum; };
        else work = [&] { return l.filter_materialize(where, proj, o).sum; };
        Measured m = b.run(work, static_cast<double>(k.rows));
        j.add("kernels").str("name", k.name).str("layout", k.layout).num("rows", k.rows)
            .num("cols", k.cols).num("k", k.k).str("where", k.where.empty() ? "none" : k.where)
            .str("filter", filter_mode_name(k.filter)).num("batch", k.batch)
            .measured(m, "ns_per_row");
    }
}

}  // namespace

int main(int argc, char** argv) {
    std::size_t max_bytes = 1ull << 30;
    double min_ms = 20;
    int samples = 5;
    std::set<std::string> only;
    for (int i = 1; i + 1 < argc; i += 2) {
        std::string a = argv[i], v = argv[i + 1];
        if (a == "--max-size") max_bytes = parse_size(v);
        else if (a == "--min-sample-ms") min_ms = std::stod(v);
        else if (a == "--samples") samples = static_cast<int>(parse_int(v, "samples"));
        else if (a == "--only") for (const std::string& s : split(v, ',')) only.insert(s);
        else {
            std::cerr << "usage: calibrate [--max-size 1G] [--min-sample-ms 20] [--samples 5]\n"
                         "                 [--only seq,chase,chase_huge,tlb,mlp,stride,overfetch,kernels]\n";
            return 1;
        }
    }
    if ((argc - 1) % 2) return 1;
    auto want = [&](const char* s) { return only.empty() || only.count(s); };

    Bench b(min_ms, samples);
    Json j;
    std::string ctr = "[";
    for (const std::string& c : b.counters()) ctr += (ctr.size() > 1 ? ", \"" : "\"") + c + "\"";
    j.meta("counters", ctr + "]");
    j.meta("max_size", std::to_string(max_bytes));
    j.meta("os_page_size", std::to_string(sysconf(_SC_PAGESIZE)));

    double t0 = now_ns();
    {
        Region r(max_bytes, false);
        if (want("seq")) seq_read(b, j, r, max_bytes), std::cerr << "seq done\n";
        if (want("chase")) random_latency(b, j, r, max_bytes, "chase"), std::cerr << "chase done\n";
        if (want("tlb")) tlb_chase(b, j, r), std::cerr << "tlb done\n";
        if (want("mlp")) mlp(b, j, r, max_bytes, "4K"), std::cerr << "mlp done\n";
        const std::size_t big = std::min<std::size_t>(max_bytes, 512u << 20);
        if (want("stride")) strided(b, j, r, big), std::cerr << "stride done\n";
        if (want("overfetch")) overfetch(b, j, r, big), std::cerr << "overfetch done\n";
    }
    if (want("chase_huge") || want("mlp")) {
        Region h(max_bytes, true);
        j.meta("anon_huge_kb", std::to_string(anon_huge_kb()));
        if (want("chase_huge")) random_latency(b, j, h, max_bytes, "chase_huge");
        if (want("mlp")) mlp(b, j, h, max_bytes, "2M");
        std::cerr << "huge-page chase done\n";
    }
    if (want("kernels")) engine_kernels(b, j), std::cerr << "kernels done\n";
    j.meta("seconds", std::to_string((now_ns() - t0) / 1e9));
    j.print(std::cout);
    return 0;
}
