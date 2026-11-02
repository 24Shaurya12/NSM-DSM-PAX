#pragma once

#include <cstddef>
#include <string>
#include <vector>

// Hardware counters read through perf_event_open(2).
//
// Core events (cycles, instructions, branch misses, LLC references/misses, L1D read misses)
// count only this process, in user space. On a hybrid Intel CPU they are opened on the P-core
// PMU, so the benchmark must be pinned to a P-core. DRAM read/write bytes come from the integrated
// memory controller's free-running uncore counters; these are system-wide, so other activity
// on the machine is included.
//
// The kernel may refuse an event (most often because of kernel.perf_event_paranoid). That event
// is then reported as unavailable, and the benchmark writes an empty CSV field for it.
class PerfCounters {
public:
    PerfCounters();
    ~PerfCounters();
    PerfCounters(const PerfCounters&) = delete;
    PerfCounters& operator=(const PerfCounters&) = delete;

    // Marks the start and end of a measurement window. Counters run all the time; a window
    // is the difference between two snapshots, scaled up if the kernel multiplexed the event.
    void start();
    void stop();

    std::size_t size() const { return events_.size(); }
    const std::string& name(std::size_t i) const { return events_[i].name; }
    bool available(std::size_t i) const { return !events_[i].fds.empty(); }
    // Value in the last start()/stop() window, already multiplied by the event's unit scale.
    double value(std::size_t i) const { return events_[i].value; }

private:
    struct Reading {
        double count = 0, enabled = 0, running = 0;
    };
    struct Event {
        std::string name;
        std::vector<int> fds;           // several fds are summed (one per memory controller)
        double scale = 1.0;             // multiplies raw counts, e.g. 64 bytes per IMC count
        std::vector<Reading> begin;     // snapshot taken by start()
        double value = 0;
    };

    std::vector<Event> events_;
};
