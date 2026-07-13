#!/usr/bin/env python3
"""cgroup v2 Prometheus exporter — long-running HTTP server, stdlib only.

Each GET /metrics reads /sys/fs/cgroup live and emits Prometheus text format.
Does NOT write to disk. Runs alongside the v1 collector (which writes SQLite).

Listen address: env CGROUP_EXPORTER_LISTEN (default 127.0.0.1:9753).
Intended to be reached via SSH reverse tunnel from a laptop running Prometheus.
"""
import os
import pwd
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
LISTEN = os.environ.get("CGROUP_EXPORTER_LISTEN", "127.0.0.1:9753")

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
    is_user = user_slice_uid(cg.name) is not None
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
        "_labels": {"cgroup": name, "service": service},
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


def render_metrics(samples):
    """Build Prometheus text format. One HELP/TYPE block per metric."""
    out = []
    for metric, help_text in GAUGES:
        out.append(f"# HELP {metric} {help_text}")
        out.append(f"# TYPE {metric} gauge")
        for s in samples:
            v = s.get(metric)
            if v is None:
                continue
            cg = escape_label_value(s["_labels"]["cgroup"])
            svc = escape_label_value(s["_labels"]["service"])
            out.append(f'{metric}{{cgroup="{cg}",service="{svc}"}} {v}')
    for metric, help_text in COUNTERS:
        out.append(f"# HELP {metric} {help_text}")
        out.append(f"# TYPE {metric} counter")
        for s in samples:
            v = s.get(metric)
            if v is None:
                continue
            cg = escape_label_value(s["_labels"]["cgroup"])
            svc = escape_label_value(s["_labels"]["service"])
            out.append(f'{metric}{{cgroup="{cg}",service="{svc}"}} {v}')
    # Host-level gauge: how many user accounts are currently logged in, i.e. how
    # many user-*.slice exist on this OS right now. No labels — one value per host;
    # Prometheus attaches the instance label at scrape time.
    n_users = sum(1 for s in samples if s.get("_is_user"))
    out.append("# HELP cgroup_logged_in_users Number of user accounts currently logged in (user-*.slice present).")
    out.append("# TYPE cgroup_logged_in_users gauge")
    out.append(f"cgroup_logged_in_users {n_users}")
    out.append("")
    return "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            try:
                samples = [sample_one(cg) for cg in discover_cgroups()]
                samples = [s for s in samples if s.get("cgroup_memory_current_bytes") is not None]
                body = render_metrics(samples).encode("utf-8")
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
    return host, int(port)


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
