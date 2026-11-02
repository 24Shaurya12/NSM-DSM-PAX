#pragma once

#include <cstdint>
#include <string>
#include <vector>

// A group of hardware counters (via perf_event_open) that is started and stopped around
// a single query run, so only the scan itself is counted. Events are user-space only.
//
// Event sets. Each has at most 5 events: Zen 3 has 6 counters, but only 5 are free
// under WSL2, and a group larger than the free counters is never scheduled at all.
//   core   cycles, instructions, L1D loads/misses, dTLB misses        (portable)
//   mem    where demand loads and L1 prefetches were filled from    (AMD Zen 3)
//   stall  dispatch stalls on a full load / store queue             (AMD Zen 3)
class PerfCounters {
public:
    // Throws std::runtime_error if the set is unknown or the counters cannot be opened.
    explicit PerfCounters(const std::string& set);
    ~PerfCounters();
    PerfCounters(const PerfCounters&) = delete;
    PerfCounters& operator=(const PerfCounters&) = delete;

    static std::vector<std::string> set_names();

    const std::vector<std::string>& names() const { return names_; }

    void start();
    void stop();

    // Counts from the last start()/stop() pair, in names() order.
    const std::vector<uint64_t>& values() const { return values_; }

    // True if any run so far was not counted the whole time (counters were shared with
    // something else and the values were scaled up from a partial count).
    bool multiplexed() const { return multiplexed_; }

private:
    std::vector<int> fds_;  // fds_[0] is the group leader
    std::vector<std::string> names_;
    std::vector<uint64_t> values_;
    bool multiplexed_ = false;
};
