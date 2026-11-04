"""How much of the computer the recorder uses: CPU and memory of a process and everything it started
(parec, ffmpeg), plus whole-system load for context. Reads /proc directly, so it needs no extra packages.
"""
import os
import time

TICK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
CPUS = os.cpu_count() or 1


def _stat(pid):
    """(parent pid, CPU ticks used so far) for a process, or None if it is gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        fields = data[data.rfind(b")") + 2:].split()     # skip "pid (name)"; fields[0] is field 3 (state)
        return int(fields[1]), int(fields[11]) + int(fields[12])     # ppid, utime + stime
    except (OSError, ValueError, IndexError):
        return None


def _memory(pid):
    """Memory a process really uses: PSS (shared pages split between their users), else resident size."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * PAGE
    except (OSError, ValueError, IndexError):
        return 0


def tree(root):
    """{pid: CPU ticks} for root and all of its descendants."""
    stats = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            s = _stat(int(name))
            if s:
                stats[int(name)] = s
    children = {}
    for pid, (ppid, _) in stats.items():
        children.setdefault(ppid, []).append(pid)
    found, todo = {}, [root]
    while todo:
        pid = todo.pop()
        if pid in stats and pid not in found:
            found[pid] = stats[pid][1]
            todo += children.get(pid, [])
    return found


def system_cpu():
    """(total ticks, idle ticks) for the whole computer since boot."""
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:9]]   # user nice system idle iowait irq softirq steal
    return sum(vals), vals[3] + vals[4]


def system_memory():
    """(total bytes, available bytes)."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                info[key] = int(rest.split()[0]) * 1024
    return info.get("MemTotal", 0), info.get("MemAvailable", 0)


def size(n):
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


class Usage:
    """Call sample() about once a second; it returns a dict of figures (None on the first call)."""

    def __init__(self, pid):
        self.pid = pid
        self.prev = None
        self.peak = 0
        self.cpu_seconds = 0.0
        self.started = None

    def sample(self):
        now = time.monotonic()
        try:
            ticks = tree(self.pid)
            sys_total, sys_idle = system_cpu()
            mem_total, mem_avail = system_memory()
        except OSError:
            return None
        if not ticks:
            return None
        mem = sum(_memory(p) for p in ticks)
        self.peak = max(self.peak, mem)
        prev, self.prev = self.prev, (now, ticks, sys_total, sys_idle)
        if prev is None:
            self.started = now      # the average counts from the first measured interval
            return None
        t0, old, old_total, old_idle = prev
        # processes that weren't there last time are new, so all their ticks belong to this interval
        used = sum(max(0, t - old.get(pid, 0)) for pid, t in ticks.items()) / TICK
        self.cpu_seconds += used
        dt = max(now - t0, 1e-3)
        d_total = sys_total - old_total
        return {
            "cores": used / dt,                     # 1.0 = one CPU core fully busy
            "cpus": CPUS,
            "memory": mem,
            "peak": self.peak,
            "mem_total": mem_total,
            "mem_avail": mem_avail,
            "system_cpu": 1 - (sys_idle - old_idle) / d_total if d_total > 0 else 0.0,
            "processes": len(ticks),
            "avg_cores": self.cpu_seconds / max(now - self.started, 1e-3),
        }
