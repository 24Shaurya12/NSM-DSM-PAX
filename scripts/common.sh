# Shared helpers for the sweep scripts. Source it, then set OUT and RAW; CPU defaults to
# $(pick_cpu).

# Expands a sysfs CPU list such as "0-3,8,10-11" into one CPU id per line.
expand_cpu_list() {
    local part
    for part in ${1//,/ }; do
        if [[ $part == *-* ]]; then seq "${part%-*}" "${part#*-}"; else echo "$part"; fi
    done
}

# Picks the core to pin benchmarks to, without assuming a particular machine:
#  - hybrid Intel (P- and E-cores): the third CPU of the performance-core PMU, i.e. a P-core
#    other than the one CPU 0 lives on (fewer interrupts);
#  - otherwise: the last online CPU (also away from CPU 0).
# Override with CPU=<id> in the environment.
pick_cpu() {
    local cpus
    if [[ -r /sys/devices/cpu_core/cpus ]]; then
        mapfile -t cpus < <(expand_cpu_list "$(cat /sys/devices/cpu_core/cpus)")
        if (( ${#cpus[@]} > 2 )); then echo "${cpus[2]}"; else echo "${cpus[-1]}"; fi
    else
        mapfile -t cpus < <(expand_cpu_list "$(cat /sys/devices/system/cpu/online)")
        echo "${cpus[-1]}"
    fi
}

# Records the machine state next to the data, for the report's methodology section.
# Extra arguments are written as additional "key=value" description lines.
write_machine_info() {
    {
        echo "date: $(date -Iseconds)"
        echo "git: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
        echo "uname: $(uname -a)"
        echo "cpu pinned: $CPU"
        echo "governor: $(cat /sys/devices/system/cpu/cpu$CPU/cpufreq/scaling_governor 2>/dev/null)"
        echo "energy_performance_preference: $(cat /sys/devices/system/cpu/cpu$CPU/cpufreq/energy_performance_preference 2>/dev/null)"
        echo "perf_event_paranoid: $(cat /proc/sys/kernel/perf_event_paranoid)"
        echo "transparent_hugepage: $(cat /sys/kernel/mm/transparent_hugepage/enabled)"
        echo "on AC power: $(cat /sys/class/power_supply/AC*/online 2>/dev/null || echo unknown)"
        echo "virtualization: $(systemd-detect-virt 2>/dev/null || echo unknown)"
        echo "counters: $(./build/scan_bench --rows 64 --cols 1 --iterations 1 --warmup 0 \
                          2>&1 >/dev/null | sed -n 's/^Hardware counters: //p')"
        for line in "$@"; do echo "$line"; done
        # Exact cache sizes of the pinned core; the plot scripts read these lines.
        for idx in /sys/devices/system/cpu/cpu$CPU/cache/index*; do
            echo "cache: L$(cat "$idx/level") $(cat "$idx/type") $(cat "$idx/size")"
        done
        echo
        lscpu
        echo
        lscpu -C
    } > "$OUT/machine.txt"
}

# Appends the CSV in $1 to $RAW, keeping only the first file's header.
append_csv() {
    if [[ -s "$RAW" ]]; then tail -n +2 "$1" >> "$RAW"; else cat "$1" > "$RAW"; fi
}
