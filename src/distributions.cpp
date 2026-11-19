#include "distributions.h"

#include <random>
#include <stdexcept>

#include "util.h"

namespace {

std::map<std::string, DistFactory>& registry() {
    static std::map<std::string, DistFactory> r;
    return r;
}

// Reads an integer argument, with a default; rejects keys not in `allowed`.
int64_t arg(const DistArgs& args, const std::string& key, int64_t def) {
    auto it = args.find(key);
    return it == args.end() ? def : parse_int(it->second, "distribution argument " + key);
}

void check_keys(const std::string& name, const DistArgs& args,
                std::initializer_list<const char*> allowed) {
    for (const auto& [k, v] : args) {
        bool ok = false;
        for (const char* a : allowed) ok = ok || k == a;
        if (!ok) throw std::invalid_argument("distribution '" + name + "' has no argument '" + k + "'");
    }
}

__extension__ using u128 = unsigned __int128;  // GCC/Clang; avoids overflow in r * range

uint64_t splitmix64(uint64_t x) {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}

// Independent values, uniform in [lo, hi].
class UniformGen : public ColumnGenerator {
public:
    UniformGen(int64_t lo, int64_t hi, uint64_t seed) : rng_(seed), dist_(lo, hi) {}
    void fill(int64_t* out, std::size_t stride, std::size_t, std::size_t n) override {
        for (std::size_t i = 0; i < n; ++i) out[i * stride] = dist_(rng_);
    }

private:
    std::mt19937_64 rng_;
    std::uniform_int_distribution<int64_t> dist_;
};

// Ascending values covering [lo, hi] with equal frequency: row r gets lo + r*(hi-lo+1)/rows.
class SortedGen : public ColumnGenerator {
public:
    SortedGen(int64_t lo, int64_t hi, std::size_t rows) : lo_(lo), range_(hi - lo + 1), rows_(rows) {}
    void fill(int64_t* out, std::size_t stride, std::size_t first, std::size_t n) override {
        for (std::size_t i = 0; i < n; ++i) {
            u128 r = first + i;
            out[i * stride] = lo_ + static_cast<int64_t>(r * range_ / rows_);
        }
    }

private:
    int64_t lo_, range_;
    std::size_t rows_;
};

// Runs of `run` consecutive rows share one value, drawn uniformly from [lo, hi] per run.
// Qualifying rows therefore come in runs, unlike `uniform`.
class ClusteredGen : public ColumnGenerator {
public:
    ClusteredGen(int64_t lo, int64_t hi, std::size_t run, uint64_t seed)
        : lo_(lo), range_(static_cast<uint64_t>(hi - lo + 1)), run_(run), seed_(seed) {}
    void fill(int64_t* out, std::size_t stride, std::size_t first, std::size_t n) override {
        for (std::size_t i = 0; i < n; ++i) {
            uint64_t run_id = (first + i) / run_;
            out[i * stride] = lo_ + static_cast<int64_t>(splitmix64(seed_ ^ run_id) % range_);
        }
    }

private:
    int64_t lo_;
    uint64_t range_;
    std::size_t run_;
    uint64_t seed_;
};

void check_range(const std::string& name, int64_t lo, int64_t hi) {
    if (lo > hi) throw std::invalid_argument("distribution '" + name + "': lo > hi");
}

// Built-in distributions register themselves here.
const bool kBuiltins = [] {
    register_distribution("uniform", [](const DistArgs& a, std::size_t, uint64_t seed) {
        check_keys("uniform", a, {"lo", "hi"});
        int64_t lo = arg(a, "lo", 0), hi = arg(a, "hi", 99);
        check_range("uniform", lo, hi);
        return std::unique_ptr<ColumnGenerator>(new UniformGen(lo, hi, seed));
    });
    register_distribution("sorted", [](const DistArgs& a, std::size_t rows, uint64_t) {
        check_keys("sorted", a, {"lo", "hi"});
        int64_t lo = arg(a, "lo", 0), hi = arg(a, "hi", 99);
        check_range("sorted", lo, hi);
        return std::unique_ptr<ColumnGenerator>(new SortedGen(lo, hi, rows));
    });
    register_distribution("clustered", [](const DistArgs& a, std::size_t, uint64_t seed) {
        check_keys("clustered", a, {"lo", "hi", "run"});
        int64_t lo = arg(a, "lo", 0), hi = arg(a, "hi", 99), run = arg(a, "run", 1024);
        check_range("clustered", lo, hi);
        if (run < 1) throw std::invalid_argument("clustered: run must be >= 1");
        return std::unique_ptr<ColumnGenerator>(
            new ClusteredGen(lo, hi, static_cast<std::size_t>(run), seed));
    });
    return true;
}();

}  // namespace

void register_distribution(const std::string& name, DistFactory factory) {
    registry()[name] = std::move(factory);
}

std::vector<std::string> registered_distributions() {
    (void)kBuiltins;
    std::vector<std::string> names;
    for (const auto& [n, f] : registry()) names.push_back(n);
    return names;
}

std::unique_ptr<ColumnGenerator> make_generator(const std::string& spec, std::size_t rows,
                                                uint64_t seed) {
    (void)kBuiltins;
    std::vector<std::string> parts = split(spec, ':');
    if (parts.empty() || parts[0].empty()) throw std::invalid_argument("empty distribution spec");
    auto it = registry().find(parts[0]);
    if (it == registry().end())
        throw std::invalid_argument("unknown distribution '" + parts[0] + "'");
    DistArgs args;
    for (std::size_t i = 1; i < parts.size(); ++i) {
        std::size_t eq = parts[i].find('=');
        if (eq == std::string::npos)
            throw std::invalid_argument("expected key=value in distribution '" + spec + "'");
        args[parts[i].substr(0, eq)] = parts[i].substr(eq + 1);
    }
    return it->second(args, rows, seed);
}

void DistSpec::add(const std::string& assignment) {
    std::size_t eq = assignment.find('=');
    if (eq == std::string::npos)
        throw std::invalid_argument("--dist expects default=SPEC or cN=SPEC, got '" + assignment + "'");
    std::string target = assignment.substr(0, eq), spec = assignment.substr(eq + 1);
    make_generator(spec, 1, 0);  // validate now, not at table build time
    if (target == "default") {
        default_spec = spec;
    } else if (target.size() > 1 && target[0] == 'c') {
        per_column[static_cast<int>(parse_int(target.substr(1), "column"))] = spec;
    } else {
        throw std::invalid_argument("--dist target must be 'default' or cN, got '" + target + "'");
    }
}

std::string DistSpec::label() const {
    std::string s = default_spec;
    for (const auto& [c, spec] : per_column) s += "|c" + std::to_string(c) + "=" + spec;
    return s;
}
