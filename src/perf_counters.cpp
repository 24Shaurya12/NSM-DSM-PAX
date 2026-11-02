#include "perf_counters.h"

#include <linux/perf_event.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <fstream>
#include <stdexcept>

namespace {

struct EventSpec {
    const char* name;
    uint32_t type;
    uint64_t config;
};

constexpr uint64_t cache_event(uint64_t cache, uint64_t op, uint64_t result) {
    return cache | (op << 8) | (result << 16);
}

// AMD raw event encoding: event select in bits 0-7, unit mask in bits 8-15.
constexpr uint64_t amd_raw(uint64_t event, uint64_t umask) { return event | (umask << 8); }

const EventSpec kCycles = {"cycles", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES};
const EventSpec kInstructions = {"instructions", PERF_TYPE_HARDWARE, PERF_COUNT_HW_INSTRUCTIONS};

struct EventSet {
    const char* name;
    bool amd_only;
    std::vector<EventSpec> events;
};

const std::vector<EventSet> kSets = {
    {"core", false,
     {kCycles,
      kInstructions,
      {"l1d_loads", PERF_TYPE_HW_CACHE,
       cache_event(PERF_COUNT_HW_CACHE_L1D, PERF_COUNT_HW_CACHE_OP_READ,
                   PERF_COUNT_HW_CACHE_RESULT_ACCESS)},
      {"l1d_misses", PERF_TYPE_HW_CACHE,
       cache_event(PERF_COUNT_HW_CACHE_L1D, PERF_COUNT_HW_CACHE_OP_READ,
                   PERF_COUNT_HW_CACHE_RESULT_MISS)},
      {"dtlb_misses", PERF_TYPE_HW_CACHE,
       cache_event(PERF_COUNT_HW_CACHE_DTLB, PERF_COUNT_HW_CACHE_OP_READ,
                   PERF_COUNT_HW_CACHE_RESULT_MISS)}}},
    // ls_dmnd_fills_from_sys (0x43): L1 fills caused by demand loads, by source.
    // ls_hw_pf_dc_fills (0x5a): L1 fills caused by the hardware prefetcher, by source.
    {"mem", true,
     {kCycles,
      {"dmnd_from_l2", PERF_TYPE_RAW, amd_raw(0x43, 0x01)},
      {"dmnd_from_l3", PERF_TYPE_RAW, amd_raw(0x43, 0x02)},
      {"dmnd_from_dram", PERF_TYPE_RAW, amd_raw(0x43, 0x08)},
      {"pf_from_dram", PERF_TYPE_RAW, amd_raw(0x5a, 0x08)}}},
    // de_dis_dispatch_token_stalls1 (0xae): cycles dispatch stalled on a full queue.
    {"stall", true,
     {kCycles,
      kInstructions,
      {"load_queue_stall", PERF_TYPE_RAW, amd_raw(0xae, 0x02)},
      {"store_queue_stall", PERF_TYPE_RAW, amd_raw(0xae, 0x04)}}},
};

bool is_amd() {
    std::ifstream cpuinfo("/proc/cpuinfo");
    std::string line;
    while (std::getline(cpuinfo, line))
        if (line.rfind("vendor_id", 0) == 0) return line.find("AuthenticAMD") != std::string::npos;
    return false;
}

int open_event(const EventSpec& e, int group_fd) {
    perf_event_attr attr{};
    attr.size = sizeof(attr);
    attr.type = e.type;
    attr.config = e.config;
    attr.disabled = group_fd == -1 ? 1 : 0;  // the leader starts/stops the whole group
    attr.exclude_kernel = 1;
    attr.exclude_hv = 1;
    attr.read_format =
        PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
    return static_cast<int>(syscall(SYS_perf_event_open, &attr, 0, -1, group_fd, 0));
}

}  // namespace

std::vector<std::string> PerfCounters::set_names() {
    std::vector<std::string> names;
    for (const auto& s : kSets) names.push_back(s.name);
    return names;
}

PerfCounters::PerfCounters(const std::string& set) {
    const EventSet* chosen = nullptr;
    for (const auto& s : kSets)
        if (set == s.name) chosen = &s;
    if (chosen == nullptr) throw std::runtime_error("unknown perf event set '" + set + "'");
    if (chosen->amd_only && !is_amd())
        throw std::runtime_error("perf event set '" + set + "' uses AMD Zen 3 events");

    for (const auto& e : chosen->events) {
        int fd = open_event(e, fds_.empty() ? -1 : fds_[0]);
        if (fd < 0) {
            std::string err = std::strerror(errno);
            for (int open_fd : fds_) close(open_fd);
            throw std::runtime_error(std::string("perf_event_open failed for ") + e.name + ": " +
                                     err);
        }
        fds_.push_back(fd);
        names_.push_back(e.name);
    }
    values_.assign(names_.size(), 0);
}

PerfCounters::~PerfCounters() {
    for (int fd : fds_) close(fd);
}

void PerfCounters::start() {
    ioctl(fds_[0], PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP);
    ioctl(fds_[0], PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP);
}

void PerfCounters::stop() {
    ioctl(fds_[0], PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP);

    // Group read layout: nr, time_enabled, time_running, then one value per event.
    std::vector<uint64_t> buf(3 + names_.size());
    if (read(fds_[0], buf.data(), buf.size() * sizeof(uint64_t)) < 0)
        throw std::runtime_error(std::string("reading perf counters failed: ") +
                                 std::strerror(errno));
    uint64_t enabled = buf[1], running = buf[2];
    if (running == 0)
        throw std::runtime_error(
            "perf event group was never scheduled (more events than free counters?)");
    double scale = 1.0;
    if (running < enabled) {
        multiplexed_ = true;
        scale = static_cast<double>(enabled) / running;
    }
    for (std::size_t i = 0; i < names_.size(); ++i)
        values_[i] = static_cast<uint64_t>(buf[3 + i] * scale);
}
