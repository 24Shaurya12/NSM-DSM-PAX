#include "query.h"

#include <x86intrin.h>

#include <algorithm>
#include <cmath>

#include "machine.h"

namespace {

const std::pair<Strategy, const char*> kNames[] = {
    {Strategy::Early, "early"},
    {Strategy::EarlyPipe, "early-pipe"},
    {Strategy::Late, "late"},
    {Strategy::LateBranch, "late-branch"},
    {Strategy::Auto, "auto"},
};

}  // namespace

const std::vector<Strategy>& all_strategies() {
    static const std::vector<Strategy> all = [] {
        std::vector<Strategy> v;
        for (const auto& [s, _] : kNames) v.push_back(s);
        return v;
    }();
    return all;
}

std::string strategy_name(Strategy s) {
    for (const auto& [st, name] : kNames)
        if (st == s) return name;
    return "?";
}

bool parse_strategy(const std::string& name, Strategy& out) {
    for (const auto& [s, n] : kNames) {
        if (name == n) {
            out = s;
            return true;
        }
    }
    return false;
}

std::vector<int> Query::referenced_columns() const {
    std::vector<int> cols = p_.columns;
    for (const auto& pr : p_.where) cols.push_back(pr.col);
    std::sort(cols.begin(), cols.end());
    cols.erase(std::unique(cols.begin(), cols.end()), cols.end());
    return cols;
}

void Query::auto_reset(double value_bytes, const void* layout, std::size_t footprint) {
    if (layout != auto_layout_) {  // measured costs are per layout
        auto_layout_ = layout;
        for (auto& range : auto_arms_) range[0] = range[1] = AutoArm{};
        for (auto& c : auto_since_explore_) c = 0;
    }
    const std::size_t refs = referenced_columns().size();
    const MachineInfo& m = MachineInfo::get();
    const bool cached = p_.auto_cache_aware && m.l3.size > 0 && footprint <= m.l3.size;
    auto_threshold_ = late_threshold(m, value_bytes, cached ? p_.auto_phi_cache : p_.auto_phi,
                                     cached ? p_.auto_sigma_cache : p_.auto_sigma,
                                     refs > 0 ? refs - 1 : 0);
    auto_sel_ = 0;
    auto_arm_ = -1;
    auto_first_ = true;
    auto_span_rows_ = auto_span_cnt_ = 0;
}

namespace {
int selectivity_range(double s, int ranges) {
    if (s <= 0) return 0;
    const int r = ranges + static_cast<int>(std::floor(std::log2(s)));  // s = 1 -> ranges
    return std::clamp(r, 1, ranges - 1);
}
}  // namespace

Strategy Query::auto_pick(Strategy no_where) const {
    // Without a WHERE clause the threshold does not apply. The model then uses `no_where`;
    // measuring still compares both (for SUM of 16 DSM columns, row at a time is 1.2x faster
    // than column at a time, for one column it is the other way round).
    if (p_.where.empty() && !p_.auto_measure) return no_where;
    if (auto_span_rows_ > 0) return auto_cur_;  // the current span goes on
    auto_arm_ = -1;
    const bool model_late = p_.where.empty() ? is_late(no_where) : auto_sel_ < auto_threshold_;
    if (!p_.auto_measure) return auto_cur_ = model_late ? Strategy::Late : Strategy::Early;
    const int r = selectivity_range(p_.where.empty() ? 1.0 : auto_sel_, kAutoRanges);
    const AutoArm& early = auto_arms_[r][0];
    const AutoArm& late = auto_arms_[r][1];
    int arm;
    if (early.samples == 0 && late.samples == 0) arm = model_late;
    else if (early.samples == 0) arm = 0;  // measure the other one once
    else if (late.samples == 0) arm = 1;
    else {
        arm = late.cycles_per_row < early.cycles_per_row;
        // Re-measure the other strategy now and then. The interval grows with how much slower
        // it was, so exploring costs about 1/kAutoExplore of the time whatever the ratio.
        const double ratio = arm ? early.cycles_per_row / late.cycles_per_row
                                 : late.cycles_per_row / early.cycles_per_row;
        const double interval = kAutoExplore * std::min(ratio, 64.0);
        if (++auto_since_explore_[r] >= interval) {
            auto_since_explore_[r] = 0;
            arm ^= 1;
        }
    }
    auto_range_ = r;
    auto_arm_ = arm;
    auto_t0_ = __rdtsc();
    return auto_cur_ = arm ? Strategy::Late : Strategy::Early;
}

void Query::auto_observe(std::size_t cnt, std::size_t n) const {
    auto_span_rows_ += n;
    auto_span_cnt_ += cnt;
    if (auto_span_rows_ < kAutoSpan) return;
    n = auto_span_rows_;
    cnt = auto_span_cnt_;
    auto_span_rows_ = auto_span_cnt_ = 0;
    if (auto_arm_ >= 0) {
        const double c = static_cast<double>(__rdtsc() - auto_t0_) / static_cast<double>(n);
        AutoArm& a = auto_arms_[auto_range_][auto_arm_];
        // A span's time is its cost plus whatever interrupted it (timer ticks, page faults):
        // about 1 vector in 250 takes 3-30x its cost. Clipping samples at twice the estimate
        // keeps one such spike from flipping the choice for hundreds of spans.
        a.cycles_per_row =
            a.samples ? 0.75 * a.cycles_per_row + 0.25 * std::min(c, 2 * a.cycles_per_row) : c;
        ++a.samples;
        auto_arm_ = -1;
    }
    const double s = static_cast<double>(cnt) / static_cast<double>(n);
    if (!p_.auto_measure) {
        auto_sel_ = s;
        return;
    }
    // Smooth over about kAutoWindow rows: a 1024-row span at 1% holds 10 +- 3 qualifying
    // rows, which would scatter the estimates over two ranges.
    const double w = static_cast<double>(n) / static_cast<double>(n + kAutoWindow);
    auto_sel_ = auto_first_ ? s : auto_sel_ + w * (s - auto_sel_);
    auto_first_ = false;
}
