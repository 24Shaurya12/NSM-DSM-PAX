#include "machine.h"

#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <fstream>
#include <sstream>

namespace {

std::string read_line(const std::string& path) {
    std::ifstream f(path);
    std::string s;
    std::getline(f, s);
    return s;
}

// "32K" / "16384K" / "1M" -> bytes.
std::size_t parse_size(const std::string& s) {
    if (s.empty()) return 0;
    std::size_t v = std::stoull(s);
    switch (s.back()) {
        case 'K': return v << 10;
        case 'M': return v << 20;
        case 'G': return v << 30;
        default: return v;
    }
}

bool read_cache(int index, int& level, std::string& type, CacheInfo& c) {
    const std::string dir = "/sys/devices/system/cpu/cpu0/cache/index" + std::to_string(index) + "/";
    const std::string lvl = read_line(dir + "level");
    if (lvl.empty()) return false;
    level = std::stoi(lvl);
    type = read_line(dir + "type");
    c.size = parse_size(read_line(dir + "size"));
    const std::string line = read_line(dir + "coherency_line_size");
    const std::string ways = read_line(dir + "ways_of_associativity");
    const std::string sets = read_line(dir + "number_of_sets");
    if (!line.empty()) c.line = std::stoull(line);
    if (!ways.empty()) c.ways = std::stoull(ways);
    if (!sets.empty()) c.sets = std::stoull(sets);
    return true;
}

MachineInfo detect() {
    MachineInfo m;
    // Fallbacks: a typical recent x86 core.
    m.l1d = {32 << 10, 64, 8, 64};
    m.l2 = {512 << 10, 64, 8, 1024};
    m.l3 = {16 << 20, 64, 16, 16384};
    for (int i = 0; i < 8; ++i) {
        int level;
        std::string type;
        CacheInfo c;
        if (!read_cache(i, level, type, c)) break;
        if (level == 1 && type == "Data") m.l1d = c;
        else if (level == 2) m.l2 = c;
        else if (level == 3) m.l3 = c;
    }
    std::ifstream cpuinfo("/proc/cpuinfo");
    std::string line;
    while (std::getline(cpuinfo, line)) {
        if (line.rfind("model name", 0) == 0) {
            m.cpu = line.substr(line.find(':') + 2);
            break;
        }
    }
    m.page_size = static_cast<std::size_t>(sysconf(_SC_PAGESIZE));
    const std::string thp = read_line("/sys/kernel/mm/transparent_hugepage/enabled");
    const auto open = thp.find('['), close = thp.find(']');
    m.thp = open != std::string::npos ? thp.substr(open + 1, close - open - 1) : "unknown";
    return m;
}

std::string fmt_size(std::size_t b) {
    std::ostringstream s;
    if (b >= (1 << 20) && b % (1 << 20) == 0) s << (b >> 20) << " MB";
    else if (b >= 1024 && b % 1024 == 0) s << (b >> 10) << " KB";
    else s << b << " B";
    return s.str();
}

}  // namespace

const MachineInfo& MachineInfo::get() {
    static const MachineInfo m = detect();
    return m;
}

std::string MachineInfo::describe() const {
    std::ostringstream s;
    s << "CPU: " << (cpu.empty() ? "unknown" : cpu) << "\n";
    const std::pair<const char*, const CacheInfo*> levels[] = {{"L1D", &l1d}, {"L2", &l2}, {"L3", &l3}};
    for (const auto& [name, c] : levels) {
        s << "  " << name << ": " << fmt_size(c->size) << ", " << c->ways << "-way, " << c->line
          << " B lines, " << c->sets << " sets (same-set stride " << fmt_size(c->set_stride())
          << ")\n";
    }
    s << "  page size " << fmt_size(page_size) << ", transparent huge pages: " << thp << "\n";
    return s.str();
}

std::size_t auto_batch(const MachineInfo& m, std::size_t bytes_per_row) {
    const std::size_t budget = m.l2.size / 4;
    std::size_t b = 256;
    while (b < 16384 && 2 * b * bytes_per_row <= budget) b *= 2;  // the next size still fits
    return b;
}

std::size_t auto_dsm_offset(const MachineInfo& m) { return 17 * m.l1d.line; }

double late_threshold(const MachineInfo& m, double value_bytes, double phi, double sigma,
                      std::size_t gathered) {
    if (gathered == 0) return 1.0;  // nothing to skip: late costs the same as early
    const double share = phi * (1.0 - sigma / static_cast<double>(gathered));
    if (share <= 0) return 0.0;  // the selection vector costs more than late could save
    const double v = std::max(1.0, static_cast<double>(m.l1d.line) / std::max(value_bytes, 1e-3));
    return 1.0 - std::pow(1.0 - std::min(share, 1.0), 1.0 / v);
}
