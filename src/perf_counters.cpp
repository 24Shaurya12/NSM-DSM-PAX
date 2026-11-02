#include "perf_counters.h"

#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cstdint>
#include <fstream>
#include <sstream>
#include <utility>

#ifndef PERF_PMU_TYPE_SHIFT
#define PERF_PMU_TYPE_SHIFT 32
#endif

namespace {

const std::string kPmuRoot = "/sys/bus/event_source/devices/";

int perf_event_open(perf_event_attr* attr, pid_t pid, int cpu) {
    return static_cast<int>(syscall(SYS_perf_event_open, attr, pid, cpu, -1, 0));
}

std::string read_file(const std::string& path) {
    std::ifstream f(path);
    std::string s;
    std::getline(f, s);
    return s;
}

// PMU type id from sysfs, or -1 if the PMU does not exist.
long pmu_type(const std::string& pmu) {
    std::string s = read_file(kPmuRoot + pmu + "/type");
    return s.empty() ? -1 : std::stol(s);
}

perf_event_attr base_attr(uint32_t type, uint64_t config) {
    perf_event_attr a{};
    a.size = sizeof(a);
    a.type = type;
    a.config = config;
    a.read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
    return a;
}

// Opens a per-process user-space core event. On hybrid CPUs the event is tied to the P-core PMU
// via the extended type bits; older kernels reject those bits, so retry without them.
int open_core_event(uint32_t type, uint64_t config) {
    long core_pmu = pmu_type("cpu_core");
    if (core_pmu > 0) {
        perf_event_attr a = base_attr(type, config | (uint64_t(core_pmu) << PERF_PMU_TYPE_SHIFT));
        a.exclude_kernel = 1;
        a.exclude_hv = 1;
        int fd = perf_event_open(&a, 0, -1);
        if (fd >= 0) return fd;
    }
    perf_event_attr a = base_attr(type, config);
    a.exclude_kernel = 1;
    a.exclude_hv = 1;
    return perf_event_open(&a, 0, -1);
}

// Lowest bit of a sysfs format field such as "config:8-15".
int format_shift(const std::string& pmu, const std::string& field) {
    std::string s = read_file(kPmuRoot + pmu + "/format/" + field);
    std::size_t colon = s.find(':');
    return colon == std::string::npos ? -1 : std::stoi(s.substr(colon + 1));
}

// Opens a system-wide uncore event described in sysfs (e.g. "event=0xff,umask=0x20").
// Writes the event's bytes-per-count to *bytes_per_count. Returns -1 on failure.
int open_uncore_event(const std::string& pmu, const std::string& event, double* bytes_per_count) {
    long type = pmu_type(pmu);
    std::string spec = read_file(kPmuRoot + pmu + "/events/" + event);
    std::string cpumask = read_file(kPmuRoot + pmu + "/cpumask");
    if (type < 0 || spec.empty() || cpumask.empty()) return -1;

    uint64_t config = 0;
    std::stringstream ss(spec);
    std::string term;
    while (std::getline(ss, term, ',')) {
        std::size_t eq = term.find('=');
        if (eq == std::string::npos) return -1;
        int shift = format_shift(pmu, term.substr(0, eq));
        if (shift < 0) return -1;
        config |= std::stoull(term.substr(eq + 1), nullptr, 0) << shift;
    }

    // The scale is in the unit named by the .unit file; for IMC counters that is MiB.
    std::string scale = read_file(kPmuRoot + pmu + "/events/" + event + ".scale");
    std::string unit = read_file(kPmuRoot + pmu + "/events/" + event + ".unit");
    *bytes_per_count = 64.0;
    if (!scale.empty() && unit == "MiB") *bytes_per_count = std::stod(scale) * 1024.0 * 1024.0;

    perf_event_attr a = base_attr(static_cast<uint32_t>(type), config);
    return perf_event_open(&a, -1, std::stoi(cpumask));
}

uint64_t hw_cache(uint64_t cache, uint64_t op, uint64_t result) {
    return cache | (op << 8) | (result << 16);
}

}  // namespace

PerfCounters::PerfCounters() {
    struct CoreSpec {
        const char* name;
        uint32_t type;
        uint64_t config;
    };
    const CoreSpec core[] = {
        {"cycles", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES},
        {"instructions", PERF_TYPE_HARDWARE, PERF_COUNT_HW_INSTRUCTIONS},
        {"branch_misses", PERF_TYPE_HARDWARE, PERF_COUNT_HW_BRANCH_MISSES},
        {"llc_refs", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CACHE_REFERENCES},
        {"llc_misses", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CACHE_MISSES},
        {"l1d_misses", PERF_TYPE_HW_CACHE,
         hw_cache(PERF_COUNT_HW_CACHE_L1D, PERF_COUNT_HW_CACHE_OP_READ,
                  PERF_COUNT_HW_CACHE_RESULT_MISS)},
    };
    for (const CoreSpec& s : core) {
        Event e;
        e.name = s.name;
        int fd = open_core_event(s.type, s.config);
        if (fd >= 0) e.fds.push_back(fd);
        events_.push_back(e);
    }

    // One free-running IMC PMU per memory controller; sum them all.
    const std::pair<const char*, const char*> dram[] = {{"dram_read_bytes", "data_read"},
                                                         {"dram_write_bytes", "data_write"}};
    for (const auto& [name, ev] : dram) {
        Event e;
        e.name = name;
        for (int i = 0; pmu_type("uncore_imc_free_running_" + std::to_string(i)) >= 0; ++i) {
            double bytes_per_count = 64.0;
            int fd = open_uncore_event("uncore_imc_free_running_" + std::to_string(i), ev,
                                       &bytes_per_count);
            if (fd < 0) continue;
            e.fds.push_back(fd);
            e.scale = bytes_per_count;
        }
        events_.push_back(e);
    }

    for (Event& e : events_) e.begin.resize(e.fds.size());
}

PerfCounters::~PerfCounters() {
    for (Event& e : events_)
        for (int fd : e.fds) close(fd);
}

namespace {

bool read_counter(int fd, double* count, double* enabled, double* running) {
    uint64_t buf[3];
    if (read(fd, buf, sizeof(buf)) != static_cast<ssize_t>(sizeof(buf))) return false;
    *count = static_cast<double>(buf[0]);
    *enabled = static_cast<double>(buf[1]);
    *running = static_cast<double>(buf[2]);
    return true;
}

}  // namespace

void PerfCounters::start() {
    for (Event& e : events_)
        for (std::size_t i = 0; i < e.fds.size(); ++i) {
            Reading& r = e.begin[i];
            read_counter(e.fds[i], &r.count, &r.enabled, &r.running);
        }
}

void PerfCounters::stop() {
    for (Event& e : events_) {
        e.value = 0;
        for (std::size_t i = 0; i < e.fds.size(); ++i) {
            Reading now;
            if (!read_counter(e.fds[i], &now.count, &now.enabled, &now.running)) continue;
            double count = now.count - e.begin[i].count;
            double enabled = now.enabled - e.begin[i].enabled;
            double running = now.running - e.begin[i].running;
            // Scale up if the event was multiplexed off the PMU for part of the window.
            if (running > 0 && enabled > running) count *= enabled / running;
            e.value += count * e.scale;
        }
    }
}
