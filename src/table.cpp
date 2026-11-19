#include "table.h"

#include <algorithm>
#include <random>

namespace {

constexpr std::size_t kZipfDistinct = 10'000;

// Spreads Zipf ranks over the full int64 range, so a column's values have few distinct
// values but no narrow range (dictionary encoding helps; bit-packing does not).
uint64_t splitmix64(uint64_t x) {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}

const std::pair<Distribution, const char*> kNames[] = {
    {Distribution::Uniform, "uniform"}, {Distribution::Wide, "wide"},
    {Distribution::Sorted, "sorted"},   {Distribution::Runs, "runs"},
    {Distribution::Zipf, "zipf"},
};

}  // namespace

const std::vector<Distribution>& all_distributions() {
    static const std::vector<Distribution> all = [] {
        std::vector<Distribution> v;
        for (const auto& [d, _] : kNames) v.push_back(d);
        return v;
    }();
    return all;
}

std::string distribution_name(Distribution d) {
    for (const auto& [dist, name] : kNames)
        if (dist == d) return name;
    return "?";
}

bool parse_distribution(const std::string& name, Distribution& out) {
    for (const auto& [dist, n] : kNames) {
        if (name == n) {
            out = dist;
            return true;
        }
    }
    return false;
}

Table generate_table(std::size_t rows, std::size_t cols, uint64_t seed, Distribution dist,
                     std::size_t run_length, int64_t domain) {
    Table t;
    t.rows = rows;
    t.cols = cols;
    t.values.resize(rows * cols);

    std::mt19937_64 rng(seed);
    switch (dist) {
        case Distribution::Uniform: {
            std::uniform_int_distribution<int64_t> u(0, domain - 1);
            for (auto& v : t.values) v = u(rng);
            break;
        }
        case Distribution::Wide:
            for (auto& v : t.values) v = static_cast<int64_t>(rng());
            break;
        case Distribution::Sorted: {
            std::uniform_int_distribution<int64_t> start(0, 999), step(0, 7);
            std::vector<int64_t> prev(cols);
            for (auto& p : prev) p = start(rng);
            for (std::size_t r = 0; r < rows; ++r)
                for (std::size_t c = 0; c < cols; ++c) t.values[r * cols + c] = prev[c] += step(rng);
            break;
        }
        case Distribution::Runs: {
            std::uniform_int_distribution<int64_t> value(0, (int64_t{1} << 20) - 1);
            std::uniform_int_distribution<std::size_t> length(1, 2 * std::max<std::size_t>(run_length, 1) - 1);
            std::vector<int64_t> cur(cols);
            std::vector<std::size_t> left(cols, 0);
            for (std::size_t r = 0; r < rows; ++r) {
                for (std::size_t c = 0; c < cols; ++c) {
                    if (left[c] == 0) {
                        cur[c] = value(rng);
                        left[c] = length(rng);
                    }
                    --left[c];
                    t.values[r * cols + c] = cur[c];
                }
            }
            break;
        }
        case Distribution::Zipf: {
            // P(rank k) proportional to 1 / k, sampled by binary search over the CDF.
            std::vector<double> cdf(kZipfDistinct);
            double total = 0;
            for (std::size_t k = 0; k < kZipfDistinct; ++k) cdf[k] = total += 1.0 / (k + 1);
            for (auto& x : cdf) x /= total;
            std::uniform_real_distribution<double> u(0.0, 1.0);
            for (auto& v : t.values) {
                std::size_t rank = std::lower_bound(cdf.begin(), cdf.end(), u(rng)) - cdf.begin();
                rank = std::min(rank, kZipfDistinct - 1);
                v = static_cast<int64_t>(splitmix64(rank));
            }
            break;
        }
    }
    return t;
}
