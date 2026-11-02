# Shared helpers for the sweep scripts. Source it after setting CPU, OUT and RAW.

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
