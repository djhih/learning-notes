#!/usr/bin/env python3
"""cgroup v2 Prometheus exporter — long-running HTTP server, stdlib only.

Each GET /metrics reads /sys/fs/cgroup live and emits Prometheus text format.
Does NOT write to disk. Runs alongside the v1 collector (which writes SQLite).

Besides the per-cgroup schema, user slices get three attribution extras:
 - cgroup_user_manager_* — the user@uid.service subtree (slice minus this =
   interactive login sessions)
 - cgroup_process_*      — live processes aggregated by command; keeps the
   union of the top-N by memory, CPU, IO and swap (what are they running?)
 - cgroup_top_process_*  — per-PID worst offenders with pid + full command
   line, htop-style (absorbed from the now-retired procwatch.py)

Listen address: env CGROUP_EXPORTER_LISTEN (default 127.0.0.1:9753).
Top-N per user slice: env CGROUP_EXPORTER_TOP_PROCS (default 10, 0 disables).
Worst-offender rows per user: env CGROUP_EXPORTER_TOP_PIDS (default 10, so a
host-wide top-10 stays exact even when one user owns every big process).
Intended to be reached via SSH reverse tunnel from a laptop running Prometheus.
"""
import os
import pwd
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def env_int(name, default):
    v = os.environ.get(name, default)
    try:
        return int(v)
    except ValueError:
        raise SystemExit(f"invalid {name}={v!r}, expected an integer")


CGROUP_ROOT = Path("/sys/fs/cgroup")
LISTEN = os.environ.get("CGROUP_EXPORTER_LISTEN", "127.0.0.1:9753")
TOP_PROCS = env_int("CGROUP_EXPORTER_TOP_PROCS", "10")
TOP_PIDS = env_int("CGROUP_EXPORTER_TOP_PIDS", "10")
CLK_TCK = os.sysconf("SC_CLK_TCK")
CMD_MAXLEN = 160

# Kept in sync with collector/collect.py to avoid v1/v2 scope drift.
INCLUDE_PATTERNS = [
    "system.slice/*.service",
    "system.slice/*.scope",
    "user.slice/user-*.slice",
]
# Drop transient units to prevent cardinality blow-up: session-* (login sessions),
# run-* (systemd-run transients with random suffix).
EXCLUDE_PREFIXES = ("session-", "run-")


# === BEGIN COPY FROM collector/collect.py @ 81f6d21cf583345efec19a105a2887b6521c3f45 ===
# If parsing in collect.py changes, re-evaluate this block.

def read_int(path):
    try:
        v = path.read_text().strip()
        if v == "max":
            return None
        return int(v)
    except (OSError, ValueError):
        return None


def read_kv(path):
    out = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                try:
                    out[parts[0]] = int(parts[1])
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def read_pressure(path):
    """Parse PSI file. Lines: 'some avg10=0.00 avg60=0.00 avg300=0.00 total=N'."""
    out = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if not parts:
                continue
            scope = parts[0]
            for kv in parts[1:]:
                k, _, v = kv.partition("=")
                if not k or not v:
                    continue
                try:
                    out[f"{scope}_{k}"] = float(v) if "." in v else int(v)
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def discover_cgroups():
    for pat in INCLUDE_PATTERNS:
        for p in CGROUP_ROOT.glob(pat):
            if p.is_dir() and not p.name.startswith(EXCLUDE_PREFIXES):
                yield p

# === END COPY ===


# --- per-device io -----------------------------------------------------------
# io.stat has one line per device: 'MAJ:MIN rbytes=N wbytes=N rios=N ...'.
# Emitted per device (not summed): when a device disappears (loop/dm teardown,
# hot-unplug) its own series just ends, whereas a summed counter would go
# backwards and rate() would misread that as a counter reset — a giant
# spurious spike. The file is missing when the io controller is not enabled
# for this cgroup (e.g. user.slice needs IOAccounting=yes), so no rows then.

IO_COUNTERS = [
    ("cgroup_io_read_bytes_total", "io.stat rbytes per device (absent when the io controller is off)."),
    ("cgroup_io_written_bytes_total", "io.stat wbytes per device (absent when the io controller is off)."),
    ("cgroup_io_reads_total", "io.stat rios per device (absent when the io controller is off)."),
    ("cgroup_io_writes_total", "io.stat wios per device (absent when the io controller is off)."),
]

_DEVNAME_CACHE = {}


def device_name(majmin):
    """Resolve MAJ:MIN to a block device name via /sys/dev/block; the MAJ:MIN
    string itself is the fallback when the symlink is gone."""
    if majmin not in _DEVNAME_CACHE:
        _DEVNAME_CACHE[majmin] = os.path.basename(os.path.realpath(f"/sys/dev/block/{majmin}"))
    return _DEVNAME_CACHE[majmin]


def sample_io(cg, labels):
    """One row per io.stat device line, labels = the cgroup's labels + device."""
    rows = []
    try:
        lines = (cg / "io.stat").read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        vals = {}
        for kv in parts[1:]:
            k, _, v = kv.partition("=")
            try:
                vals[k] = int(v)
            except ValueError:
                pass
        rows.append({
            "_labels": {**labels, "device": device_name(parts[0])},
            "cgroup_io_read_bytes_total": vals.get("rbytes"),
            "cgroup_io_written_bytes_total": vals.get("wbytes"),
            "cgroup_io_reads_total": vals.get("rios"),
            "cgroup_io_writes_total": vals.get("wios"),
        })
    return rows


# user.slice children are named "user-<uid>.slice". Resolve the UID to a login
# name for display; fall back to "user-<uid>" when the account has no passwd entry
# (e.g. a logged-out UID still lingering, or an LDAP user not resolvable here).
USER_SLICE_RE = re.compile(r"^user-(\d+)\.slice$")


def user_slice_uid(cg_name):
    """Return the int UID if cg_name is a user-<uid>.slice, else None."""
    m = USER_SLICE_RE.match(cg_name)
    return int(m.group(1)) if m else None


def display_service(cg_name):
    """Map a cgroup leaf name to its display label.

    user-<uid>.slice -> the account's login name (or "user-<uid>" if unresolvable).
    Everything else (system.slice services/scopes) is returned unchanged.
    """
    uid = user_slice_uid(cg_name)
    if uid is None:
        return cg_name
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"user-{uid}"


def count_login_sessions(cg):
    """Number of login sessions under a user slice = its session-*.scope children."""
    return sum(1 for p in cg.glob("session-*.scope") if p.is_dir())


# --- user-manager breakdown --------------------------------------------------
# Children of user-<uid>.slice: session-N.scope (one per login) and
# user@<uid>.service (systemd user manager + everything started through it).
# Only user@ is exported: its unit name is stable, so it is one series per
# user. session-N scopes are deliberately NOT exported — logind session IDs
# increase monotonically per login, so per-session series would churn the
# TSDB without bound (the same cardinality reasoning as EXCLUDE_PREFIXES).
# Interactive-session usage is derivable in PromQL as slice total minus
# manager; the session count is cgroup_user_login_sessions.

MANAGER_GAUGES = [
    ("cgroup_user_manager_memory_current_bytes", "Current memory usage of the user's systemd manager tree (user@uid.service; slice minus this = login sessions)."),
]
MANAGER_COUNTERS = [
    ("cgroup_user_manager_cpu_usage_usec_total", "Cumulative CPU usage of the user's systemd manager tree (microseconds)."),
]


def sample_manager(cg, service, uid):
    rows = []
    for child in sorted(cg.glob("user@*.service")):
        if not child.is_dir():
            continue
        rows.append({
            "_labels": {
                "cgroup": str(child.relative_to(CGROUP_ROOT)),
                "service": service,
                "uid": uid,
            },
            "cgroup_user_manager_memory_current_bytes": read_int(child / "memory.current"),
            "cgroup_user_manager_cpu_usage_usec_total": read_kv(child / "cpu.stat").get("usage_usec"),
        })
    return rows


# --- per-process attribution -------------------------------------------------
# "What is this user running?" — aggregate the live processes of a user slice
# by command label and keep the union of the top-N by memory, CPU, IO and swap,
# so heavy hitters of every kind survive the cut. The command label (never pid)
# keeps Prometheus cardinality bounded. Cumulative values (cpu, io) are
# exported as gauges, not counters: the sum drops when a process exits, which
# would break counter monotonicity.

PROC_GAUGES = [
    ("cgroup_process_count", "Live processes with this command name in the user slice (top-N by mem/cpu/io/swap only)."),
    ("cgroup_process_memory_rss_bytes", "Resident memory (VmRSS) summed over live processes with this comm."),
    ("cgroup_process_swap_bytes", "Swapped-out memory (VmSwap) summed over live processes with this comm."),
    ("cgroup_process_threads", "Threads summed over live processes with this comm."),
    ("cgroup_process_dstate_count", "Processes in uninterruptible sleep (D state) with this comm — IO-stall attribution."),
    ("cgroup_process_zombie_count", "Zombie processes with this comm."),
    ("cgroup_process_cpu_seconds", "CPU time summed over live processes with this comm. Gauge: drops when they exit."),
    ("cgroup_process_cpu_percent", "CPU%% summed over live processes with this comm (100 = one core; 0 on the first scrape)."),
    ("cgroup_process_io_read_bytes", "Storage reads summed over live processes with this comm. Gauge; absent unless /proc/<pid>/io was readable for every process."),
    ("cgroup_process_io_written_bytes", "Storage writes summed over live processes with this comm. Gauge; absent unless /proc/<pid>/io was readable for every process."),
]

# Worst offenders with the exact process identity (pid + full command line),
# htop-style — the drill-down procwatch.py used to provide. pid/cmd label
# churn is confined to these TOP_PIDS rows per user.
TOPPROC_GAUGES = [
    ("cgroup_top_process_memory_bytes", "Top processes by RSS per user (rank 1 = biggest); pid/cmd give the exact process."),
    ("cgroup_top_process_cpu_percent", "Top processes by CPU%% per user (100 = one core); 0 on the first scrape."),
    ("cgroup_top_process_io_bytes_per_sec", "Top processes by disk IO rate (read+write B/s) per user; 0 on the first scrape; only processes with readable /proc/<pid>/io compete."),
]

# Interpreter executables where the script argument, not the binary, names the
# job — on a shared compute host everything is "python3" otherwise.
INTERPRETERS = {
    "python", "pypy", "node", "nodejs", "java", "perl", "ruby",
    "Rscript", "bash", "sh", "zsh", "fish", "julia", "lua",
}
# Flags whose value is a separate argv element; the value must not be mistaken
# for the script name (java -cp /opt/lib Main would otherwise become 'java lib').
# Keyed like INTERPRETERS (version suffix stripped). -jar/-m are NOT here: their
# value IS the job name and the flag itself is skipped by the '-' check.
VALUE_FLAGS = {
    "java": {"-cp", "-classpath", "--class-path", "-p", "--module-path",
             "--add-modules", "--add-opens", "--add-exports", "--add-reads",
             "--upgrade-module-path", "--patch-module", "-d"},
    "python": {"-W", "-X", "--check-hash-based-pycs"},
    "node": {"-r", "--require", "--loader", "--experimental-loader",
             "--conditions", "--title", "--max-old-space-size"},
    "perl": {"-I"},
    "ruby": {"-I", "-r", "-C"},
    "bash": {"-O", "+O", "--rcfile", "--init-file"},
}


def command_label(comm, argv):
    """htop-style command identity used as the `comm` label.

    - interpreter: 'python3 train.py' — first non-flag argument, skipping the
      values of known value-taking flags (see VALUE_FLAGS)
    - comm hit the kernel's 15-byte cap: fall back to basename(argv[0])
    - otherwise keep comm — it preserves self-renamed workers (e.g. firefox's
      'Web Content') that argv[0] would merge into the parent binary
    """
    if not argv:
        return comm  # kernel thread or vanished; zombies also have empty cmdline
    exe = os.path.basename(argv[0])
    base = exe.rstrip("0123456789.")
    if base in INTERPRETERS:
        value_flags = VALUE_FLAGS.get(base, ())
        skip_next = False
        for arg in argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if arg.startswith("-") or (base == "bash" and arg.startswith("+")):
                skip_next = arg in value_flags
                continue
            arg = os.path.basename(arg)
            # Guard against inline code ('python -c ...') and other non-script
            # arguments: they would explode label cardinality.
            if arg and len(arg) <= 64 and not any(ch.isspace() for ch in arg):
                return f"{exe} {arg}"
            break
        return exe
    if len(comm.encode()) >= 15 and exe:
        return exe
    return comm


def iter_pids(cg):
    """All PIDs anywhere under this cgroup. v2 keeps processes in leaf cgroups,
    so the slice's own cgroup.procs is empty — walk the whole subtree."""
    for procs_file in cg.glob("**/cgroup.procs"):
        try:
            for tok in procs_file.read_text().split():
                yield int(tok)
        except (OSError, ValueError):
            continue


def read_proc(pid):
    """One process as a dict, or None if it vanished mid-scrape.

    The io fields are None when /proc/<pid>/io is unreadable — it needs root
    (or CAP_SYS_PTRACE) for other users' processes, unlike status/stat/cmdline
    which are world-readable.
    """
    base = Path("/proc") / str(pid)
    try:
        status = (base / "status").read_text()
        stat = (base / "stat").read_text()
    except OSError:
        return None
    comm, rss, swap, threads = "", 0, 0, 0
    for line in status.splitlines():
        if line.startswith("Name:"):
            parts = line.split(None, 1)
            comm = parts[1] if len(parts) > 1 else ""
        elif line.startswith(("VmRSS:", "VmSwap:", "Threads:")):
            try:
                val = int(line.split()[1])
            except (IndexError, ValueError):
                continue
            if line.startswith("VmRSS:"):
                rss = val * 1024
            elif line.startswith("VmSwap:"):
                swap = val * 1024
            else:
                threads = val
    # stat: comm may contain spaces/parens, so split after the closing ')'.
    # Fields after it start at state (field 3): utime is field 14, stime 15.
    # Same parsing as read_procs() in procwatch.py (retired, absorbed here).
    fields = stat.rpartition(")")[2].split()
    state = fields[0] if fields else "?"
    try:
        cpu_j = int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        cpu_j = 0
    cpu = cpu_j / CLK_TCK
    io_r = io_w = None
    try:
        for line in (base / "io").read_text().splitlines():
            k, _, v = line.partition(":")
            if k == "read_bytes":
                io_r = int(v)
            elif k == "write_bytes":
                io_w = int(v)
    except (OSError, ValueError):
        io_r = io_w = None
    try:
        argv = [a for a in (base / "cmdline").read_bytes().decode("utf-8", "replace").split("\0") if a]
    except OSError:
        argv = []
    return {
        "pid": pid,
        "label": command_label(comm, argv),
        "cmd": " ".join(argv)[:CMD_MAXLEN] if argv else f"[{comm}]",
        "rss": rss, "swap": swap, "threads": threads,
        "cpu": cpu, "cpu_j": cpu_j, "state": state, "io_r": io_r, "io_w": io_w,
    }


class DeltaTracker:
    """Per-second rate of a cumulative per-PID value, computed as the delta
    between two successive collections (procwatch.py's CPU%% approach,
    generalized). The first collection after start reports 0 (no previous
    sample), as does a reused PID whose value went backwards. State needs no
    lock of its own — collections are serialized by the render lock."""

    def __init__(self):
        self.prev = {}       # pid -> cumulative value at previous collection
        self.prev_t = None
        self.cur = {}
        self.dt = None

    def begin(self):
        self.now = time.monotonic()
        self.dt = (self.now - self.prev_t) if self.prev_t is not None else None
        self.cur = {}

    def rate(self, pid, value):
        self.cur[pid] = value
        prev = self.prev.get(pid)
        if self.dt and self.dt > 0 and prev is not None and value >= prev:
            return (value - prev) / self.dt
        return 0.0

    def end(self):
        self.prev = self.cur
        self.prev_t = self.now


CPU_TRACKER = DeltaTracker()   # cumulative jiffies -> CPU%% via /CLK_TCK*100
IO_TRACKER = DeltaTracker()    # cumulative read+written bytes -> B/s


def sample_processes(cg, service, uid):
    """Returns (per-command rows, per-PID worst-offender rows) for a user slice."""
    if TOP_PROCS <= 0:
        return [], []
    agg = {}   # command label -> summed fields
    recs = []  # per-PID records for the worst-offender rows
    for pid in iter_pids(cg):
        p = read_proc(pid)
        if not p or not p["label"]:
            continue
        p["pct"] = CPU_TRACKER.rate(pid, p["cpu_j"]) / CLK_TCK * 100.0
        p["io_rate"] = (IO_TRACKER.rate(pid, p["io_r"] + p["io_w"])
                        if p["io_r"] is not None else None)
        recs.append(p)
        a = agg.setdefault(p["label"], {
            "count": 0, "rss": 0, "swap": 0, "threads": 0,
            "dstate": 0, "zombie": 0, "cpu": 0.0, "pct": 0.0,
            "io_r": 0, "io_w": 0, "io_unreadable": 0,
        })
        a["count"] += 1
        a["rss"] += p["rss"]
        a["swap"] += p["swap"]
        a["threads"] += p["threads"]
        a["cpu"] += p["cpu"]
        a["pct"] += p["pct"]
        a["dstate"] += p["state"] == "D"
        a["zombie"] += p["state"] == "Z"
        if p["io_r"] is None:
            # Emit io all-or-nothing per label: a partial sum would look
            # complete and misattribute disk traffic.
            a["io_unreadable"] += 1
        else:
            a["io_r"] += p["io_r"]
            a["io_w"] += p["io_w"]

    def top(keyfn):
        return sorted(agg, key=lambda c: keyfn(agg[c]), reverse=True)[:TOP_PROCS]

    # Swap gets its own dimension: a fully swapped-out job has tiny RSS and
    # would be dropped by the other cuts — exactly the one worth seeing.
    # D-state/zombie commands are always kept (rare, and the point of the metric).
    keep = (set(top(lambda a: a["rss"])) | set(top(lambda a: a["cpu"]))
            | set(top(lambda a: a["pct"])) | set(top(lambda a: a["swap"]))
            | set(top(lambda a: a["io_r"] + a["io_w"])))
    keep |= {c for c, a in agg.items() if a["dstate"] or a["zombie"]}
    rows = []
    for label in sorted(keep):
        a = agg[label]
        io_ok = not a["io_unreadable"]
        rows.append({
            "_labels": {"service": service, "uid": uid, "comm": label},
            "cgroup_process_count": a["count"],
            "cgroup_process_memory_rss_bytes": a["rss"],
            "cgroup_process_swap_bytes": a["swap"],
            "cgroup_process_threads": a["threads"],
            "cgroup_process_dstate_count": a["dstate"],
            "cgroup_process_zombie_count": a["zombie"],
            "cgroup_process_cpu_seconds": round(a["cpu"], 2),
            "cgroup_process_cpu_percent": round(a["pct"], 1),
            "cgroup_process_io_read_bytes": a["io_r"] if io_ok else None,
            "cgroup_process_io_written_bytes": a["io_w"] if io_ok else None,
        })
    top_rows = []
    io_recs = [r for r in recs if r["io_rate"] is not None]
    for metric, key, pool in (("cgroup_top_process_memory_bytes", "rss", recs),
                              ("cgroup_top_process_cpu_percent", "pct", recs),
                              ("cgroup_top_process_io_bytes_per_sec", "io_rate", io_recs)):
        ranked = sorted(pool, key=lambda r: r[key], reverse=True)[:TOP_PIDS]
        for rank, p in enumerate(ranked, 1):
            top_rows.append({
                "_labels": {"service": service, "uid": uid, "rank": str(rank),
                            "pid": str(p["pid"]), "comm": p["label"], "cmd": p["cmd"]},
                metric: p[key] if key == "rss" else round(p[key], 1),
            })
    return rows, top_rows


# Metric schema — (prom_name, kind, help_text). kind: gauge|counter.
# Counter names already include the _total suffix per Prometheus convention.
GAUGES = [
    ("cgroup_memory_current_bytes", "Current memory usage in bytes."),
    ("cgroup_memory_peak_bytes", "Peak memory usage in bytes."),
    ("cgroup_memory_max_bytes", "Hard memory limit in bytes (omitted when 'max')."),
    ("cgroup_memory_high_bytes", "Soft memory limit in bytes (omitted when 'max')."),
    ("cgroup_memory_anon_bytes", "Anonymous memory in bytes (heap/stack; not reclaimable without swap)."),
    ("cgroup_memory_file_bytes", "Page cache in bytes (reclaimable; charged to the first toucher)."),
    ("cgroup_psi_cpu_some_avg10", "CPU PSI some avg10 (% time at least one task stalled)."),
    ("cgroup_psi_cpu_some_avg60", "CPU PSI some avg60."),
    ("cgroup_psi_cpu_full_avg10", "CPU PSI full avg10 (% time all tasks stalled)."),
    ("cgroup_psi_cpu_full_avg60", "CPU PSI full avg60."),
    ("cgroup_psi_memory_some_avg10", "Memory PSI some avg10."),
    ("cgroup_psi_memory_some_avg60", "Memory PSI some avg60."),
    ("cgroup_psi_memory_full_avg10", "Memory PSI full avg10."),
    ("cgroup_psi_memory_full_avg60", "Memory PSI full avg60."),
    ("cgroup_psi_io_some_avg10", "IO PSI some avg10."),
    ("cgroup_psi_io_some_avg60", "IO PSI some avg60."),
    ("cgroup_psi_io_full_avg10", "IO PSI full avg10."),
    ("cgroup_psi_io_full_avg60", "IO PSI full avg60."),
    ("cgroup_user_login_sessions", "Number of login sessions (session-*.scope) under a user slice."),
]
COUNTERS = [
    ("cgroup_cpu_usage_usec_total", "Cumulative CPU usage in microseconds."),
    ("cgroup_cpu_nr_throttled_total", "Number of times the cgroup has been throttled."),
    ("cgroup_cpu_throttled_usec_total", "Cumulative throttled time in microseconds."),
    ("cgroup_psi_cpu_some_seconds_total", "Total time at least one task stalled on CPU (seconds)."),
    ("cgroup_psi_cpu_full_seconds_total", "Total time all tasks stalled on CPU (seconds)."),
    ("cgroup_psi_memory_some_seconds_total", "Total time at least one task stalled on memory (seconds)."),
    ("cgroup_psi_memory_full_seconds_total", "Total time all tasks stalled on memory (seconds)."),
    ("cgroup_psi_io_some_seconds_total", "Total time at least one task stalled on IO (seconds)."),
    ("cgroup_psi_io_full_seconds_total", "Total time all tasks stalled on IO (seconds)."),
    ("cgroup_memory_events_low_total", "memory.events: low boundary breaches."),
    ("cgroup_memory_events_high_total", "memory.events: high boundary breaches."),
    ("cgroup_memory_events_max_total", "memory.events: max boundary breaches."),
    ("cgroup_memory_events_oom_total", "memory.events: OOM events."),
    ("cgroup_memory_events_oom_kill_total", "memory.events: OOM kills."),
    ("cgroup_memory_workingset_refault_file_total", "Previously-hot file pages evicted then read back (thrashing signal)."),
    ("cgroup_memory_pgscan_total", "Pages scanned for reclaim (all paths)."),
    ("cgroup_memory_pgscan_kswapd_total", "Pages scanned by background reclaim (global memory pressure)."),
    ("cgroup_memory_pgscan_direct_total", "Pages scanned by direct reclaim (cgroup hit its own memory.high/max)."),
    ("cgroup_memory_pgsteal_total", "Pages actually reclaimed."),
]


def sample_one(cg):
    """Return dict keyed by Prometheus metric name. Values may be None — skipped on emit."""
    name = str(cg.relative_to(CGROUP_ROOT))
    uid = user_slice_uid(cg.name)
    is_user = uid is not None
    service = display_service(cg.name)
    m_events = read_kv(cg / "memory.events")
    m_stat = read_kv(cg / "memory.stat")
    cpu_stat = read_kv(cg / "cpu.stat")
    m_psi = read_pressure(cg / "memory.pressure")
    c_psi = read_pressure(cg / "cpu.pressure")
    i_psi = read_pressure(cg / "io.pressure")

    # PSI 'total' field is microseconds — convert to seconds for prom convention.
    def psi_total_sec(d, key):
        v = d.get(key)
        return v / 1_000_000 if v is not None else None

    return {
        # uid: the slice owner's numeric UID; -1 for non-user cgroups
        # (system.slice services/scopes) so every series carries the label.
        "_labels": {"cgroup": name, "service": service, "uid": str(uid) if is_user else "-1"},
        "_is_user": is_user,
        "cgroup_user_login_sessions": count_login_sessions(cg) if is_user else None,
        "cgroup_memory_current_bytes": read_int(cg / "memory.current"),
        "cgroup_memory_peak_bytes": read_int(cg / "memory.peak"),
        "cgroup_memory_max_bytes": read_int(cg / "memory.max"),
        "cgroup_memory_high_bytes": read_int(cg / "memory.high"),
        "cgroup_memory_anon_bytes": m_stat.get("anon"),
        "cgroup_memory_file_bytes": m_stat.get("file"),
        # workingset_refault_file / pgscan_{kswapd,direct} exist since kernel 5.9;
        # older kernels (RHEL8 4.18) only have unsplit workingset_refault / pgscan.
        "cgroup_memory_workingset_refault_file_total":
            m_stat.get("workingset_refault_file", m_stat.get("workingset_refault")),
        "cgroup_memory_pgscan_total": m_stat.get("pgscan"),
        "cgroup_memory_pgscan_kswapd_total": m_stat.get("pgscan_kswapd"),
        "cgroup_memory_pgscan_direct_total": m_stat.get("pgscan_direct"),
        "cgroup_memory_pgsteal_total": m_stat.get("pgsteal"),
        "cgroup_psi_cpu_some_avg10": c_psi.get("some_avg10"),
        "cgroup_psi_cpu_some_avg60": c_psi.get("some_avg60"),
        "cgroup_psi_cpu_full_avg10": c_psi.get("full_avg10"),
        "cgroup_psi_cpu_full_avg60": c_psi.get("full_avg60"),
        "cgroup_psi_memory_some_avg10": m_psi.get("some_avg10"),
        "cgroup_psi_memory_some_avg60": m_psi.get("some_avg60"),
        "cgroup_psi_memory_full_avg10": m_psi.get("full_avg10"),
        "cgroup_psi_memory_full_avg60": m_psi.get("full_avg60"),
        "cgroup_psi_io_some_avg10": i_psi.get("some_avg10"),
        "cgroup_psi_io_some_avg60": i_psi.get("some_avg60"),
        "cgroup_psi_io_full_avg10": i_psi.get("full_avg10"),
        "cgroup_psi_io_full_avg60": i_psi.get("full_avg60"),
        "cgroup_cpu_usage_usec_total": cpu_stat.get("usage_usec"),
        "cgroup_cpu_nr_throttled_total": cpu_stat.get("nr_throttled"),
        "cgroup_cpu_throttled_usec_total": cpu_stat.get("throttled_usec"),
        "cgroup_psi_cpu_some_seconds_total": psi_total_sec(c_psi, "some_total"),
        "cgroup_psi_cpu_full_seconds_total": psi_total_sec(c_psi, "full_total"),
        "cgroup_psi_memory_some_seconds_total": psi_total_sec(m_psi, "some_total"),
        "cgroup_psi_memory_full_seconds_total": psi_total_sec(m_psi, "full_total"),
        "cgroup_psi_io_some_seconds_total": psi_total_sec(i_psi, "some_total"),
        "cgroup_psi_io_full_seconds_total": psi_total_sec(i_psi, "full_total"),
        "cgroup_memory_events_low_total": m_events.get("low"),
        "cgroup_memory_events_high_total": m_events.get("high"),
        "cgroup_memory_events_max_total": m_events.get("max"),
        "cgroup_memory_events_oom_total": m_events.get("oom"),
        "cgroup_memory_events_oom_kill_total": m_events.get("oom_kill"),
    }


def escape_label_value(v):
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def emit(out, rows, metric, kind, help_text):
    """Append one HELP/TYPE block for `metric`, one line per row that has it."""
    out.append(f"# HELP {metric} {help_text}")
    out.append(f"# TYPE {metric} {kind}")
    for r in rows:
        v = r.get(metric)
        if v is None:
            continue
        labels = ",".join(
            f'{k}="{escape_label_value(str(lv))}"' for k, lv in r["_labels"].items()
        )
        out.append(f"{metric}{{{labels}}} {v}")


def render_metrics(samples, managers, procs, top_procs, io_rows):
    """Build Prometheus text format. One HELP/TYPE block per metric."""
    out = []
    for metric, help_text in GAUGES:
        emit(out, samples, metric, "gauge", help_text)
    for metric, help_text in COUNTERS:
        emit(out, samples, metric, "counter", help_text)
    for metric, help_text in IO_COUNTERS:
        emit(out, io_rows, metric, "counter", help_text)
    for metric, help_text in MANAGER_GAUGES:
        emit(out, managers, metric, "gauge", help_text)
    for metric, help_text in MANAGER_COUNTERS:
        emit(out, managers, metric, "counter", help_text)
    for metric, help_text in PROC_GAUGES:
        emit(out, procs, metric, "gauge", help_text)
    for metric, help_text in TOPPROC_GAUGES:
        emit(out, top_procs, metric, "gauge", help_text)
    # Host-level gauge: how many user accounts are currently logged in, i.e. how
    # many user-*.slice exist on this OS right now. No labels — one value per host;
    # Prometheus attaches the instance label at scrape time.
    n_users = sum(1 for s in samples if s.get("_is_user"))
    out.append("# HELP cgroup_logged_in_users Number of user accounts currently logged in (user-*.slice present).")
    out.append("# TYPE cgroup_logged_in_users gauge")
    out.append(f"cgroup_logged_in_users {n_users}")
    out.append("")
    return "\n".join(out)


def collect():
    samples, managers, procs, top_procs, io_rows = [], [], [], [], []
    CPU_TRACKER.begin()
    IO_TRACKER.begin()
    for cg in discover_cgroups():
        s = sample_one(cg)
        if s.get("cgroup_memory_current_bytes") is None:
            continue  # cgroup vanished mid-scrape
        samples.append(s)
        io_rows.extend(sample_io(cg, s["_labels"]))
        if s["_is_user"]:
            svc = s["_labels"]["service"]
            uid = s["_labels"]["uid"]
            managers.extend(sample_manager(cg, svc, uid))
            comm_rows, top_rows = sample_processes(cg, svc, uid)
            procs.extend(comm_rows)
            top_procs.extend(top_rows)
    CPU_TRACKER.end()
    IO_TRACKER.end()
    return samples, managers, procs, top_procs, io_rows


# One collection pass shared by concurrent scrapers: ThreadingHTTPServer would
# otherwise let overlapping requests (second Prometheus, manual curl, timeout
# retry) each walk all of /sys/fs/cgroup and /proc at the same time.
CACHE_TTL_SEC = 5
_render_lock = threading.Lock()
_render_cache = {"expires": 0.0, "body": b""}


def render_cached():
    with _render_lock:
        now = time.monotonic()
        if now >= _render_cache["expires"]:
            _render_cache["body"] = render_metrics(*collect()).encode("utf-8")
            _render_cache["expires"] = now + CACHE_TTL_SEC
        return _render_cache["body"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            try:
                body = render_cached()
            except Exception as e:
                self.send_error(500, f"collection error: {e}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/" or self.path == "/healthz":
            body = b"cgroup-exporter ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


def parse_listen(s):
    host, _, port = s.rpartition(":")
    if not host or not port:
        raise SystemExit(f"invalid CGROUP_EXPORTER_LISTEN={s!r}, expected host:port")
    try:
        return host, int(port)
    except ValueError:
        raise SystemExit(f"invalid CGROUP_EXPORTER_LISTEN={s!r}, port must be an integer")


def main():
    host, port = parse_listen(LISTEN)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"cgroup-exporter listening on {host}:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
