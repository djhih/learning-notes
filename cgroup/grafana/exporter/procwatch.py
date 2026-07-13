#!/usr/bin/env python3
"""Per-user top-process exporter — the "which process exactly" drill-down.

The cgroup exporter (exporter.py) tells you which *user* (user-<uid>.slice) is
hot. This one tells you which *process* inside that user is responsible, with
its command line — like reading htop for the worst offender.

For each real login user (uid >= PROCWATCH_MIN_UID) it walks /proc and emits the
top-N processes by memory (RSS) and by CPU, carrying pid / comm / cmdline as
labels so a Grafana table can show them htop-style.

CPU% is the delta of (utime+stime) between two successive scrapes: 100 means one
core fully used; a multi-threaded process can exceed 100. The first scrape after
start reports 0% (no previous sample yet); memory is correct from the first.

Listen address: env PROCWATCH_LISTEN (default 127.0.0.1:9754).
Reads /proc only; never writes. Runs alongside exporter.py.
"""
import os
import pwd
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = os.environ.get("PROCWATCH_LISTEN", "127.0.0.1:9754")
MIN_UID = int(os.environ.get("PROCWATCH_MIN_UID", "1000"))   # human login users
TOP_N = int(os.environ.get("PROCWATCH_TOP_N", "3"))
CMD_MAXLEN = 160

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
PROC = "/proc"

_lock = threading.Lock()
_prev_cpu = {}       # pid -> (utime+stime) jiffies at last scrape
_prev_time = None    # monotonic timestamp of last scrape


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def read_procs():
    """One /proc pass: list of processes owned by uid >= MIN_UID."""
    procs = []
    for pid in os.listdir(PROC):
        if not pid.isdigit():
            continue
        d = f"{PROC}/{pid}"
        try:
            uid = os.stat(d).st_uid
            if uid < MIN_UID:
                continue
            # /proc/<pid>/stat — comm (field 2) can contain spaces and ')',
            # so split on the LAST ')': everything after is space-delimited.
            raw = _read(f"{d}/stat").decode("latin-1")
            rp = raw.rindex(")")
            comm = raw[raw.index("(") + 1:rp]
            rest = raw[rp + 2:].split()
            cpu_j = int(rest[11]) + int(rest[12])          # utime + stime (fields 14,15)
            rss = int(_read(f"{d}/statm").split()[1]) * PAGE_SIZE
            cmd = _read(f"{d}/cmdline").replace(b"\x00", b" ").decode("latin-1").strip()
            procs.append({
                "pid": int(pid), "uid": uid, "comm": comm,
                "cmd": (cmd or f"[{comm}]")[:CMD_MAXLEN],
                "rss": rss, "cpu_j": cpu_j,
            })
        except (OSError, ValueError, IndexError):
            continue   # process vanished mid-read or line was malformed
    return procs


def sample():
    """Read procs and attach a CPU% computed against the previous scrape."""
    global _prev_cpu, _prev_time
    with _lock:
        now = time.monotonic()
        procs = read_procs()
        dt = (now - _prev_time) if _prev_time is not None else None
        for p in procs:
            prev = _prev_cpu.get(p["pid"])
            if dt and dt > 0 and prev is not None and p["cpu_j"] >= prev:
                p["cpu"] = (p["cpu_j"] - prev) / CLK_TCK / dt * 100.0
            else:
                p["cpu"] = 0.0
        _prev_cpu = {p["pid"]: p["cpu_j"] for p in procs}
        _prev_time = now
        return procs


def username(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid-{uid}"


def esc(v):
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render():
    procs = sample()
    by_user = {}
    for p in procs:
        by_user.setdefault(p["uid"], []).append(p)

    def block(metric, help_text, key, fmt):
        out = [f"# HELP {metric} {help_text}", f"# TYPE {metric} gauge"]
        for uid, plist in by_user.items():
            user = esc(username(uid))
            for rank, p in enumerate(sorted(plist, key=lambda x: x[key], reverse=True)[:TOP_N], 1):
                labels = (f'user="{user}",rank="{rank}",pid="{p["pid"]}",'
                          f'comm="{esc(p["comm"])}",cmd="{esc(p["cmd"])}"')
                out.append(f"{metric}{{{labels}}} {fmt(p[key])}")
        return out

    lines = []
    lines += block("procwatch_top_mem_bytes",
                   "Top processes by resident memory per user (rank 1 = biggest).",
                   "rss", lambda v: str(int(v)))
    lines += block("procwatch_top_cpu_percent",
                   "Top processes by CPU per user (100 = one core fully used).",
                   "cpu", lambda v: f"{v:.1f}")
    lines.append("")
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            try:
                body = render().encode("utf-8")
            except Exception as e:
                self.send_error(500, f"collection error: {e}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/healthz"):
            body = b"procwatch ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


def main():
    host, _, port = LISTEN.rpartition(":")
    if not host or not port:
        raise SystemExit(f"invalid PROCWATCH_LISTEN={LISTEN!r}, expected host:port")
    httpd = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"procwatch listening on {host}:{port} (min_uid={MIN_UID}, top_n={TOP_N})",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
