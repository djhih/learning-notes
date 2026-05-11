#!/usr/bin/env python3
"""cgroup v2 baseline collector — single-shot, called by systemd timer."""
import os
import sqlite3
import sys
import time
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
DB_PATH = Path(os.environ.get("CGROUP_BASELINE_DB", "/var/lib/cgroup-baseline/samples.db"))

INCLUDE_PATTERNS = [
    "system.slice/*.service",
    "system.slice/*.scope",
    "user.slice/user-*.slice",
]
EXCLUDE_PREFIXES = ("session-",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts INTEGER NOT NULL,
  cgroup TEXT NOT NULL,
  memory_current INTEGER,
  memory_peak INTEGER,
  memory_max INTEGER,
  memory_high INTEGER,
  memory_events_low INTEGER,
  memory_events_high INTEGER,
  memory_events_max INTEGER,
  memory_events_oom INTEGER,
  memory_events_oom_kill INTEGER,
  cpu_usage_usec INTEGER,
  cpu_nr_throttled INTEGER,
  cpu_throttled_usec INTEGER,
  cpu_psi_some_avg10 REAL,
  cpu_psi_some_avg60 REAL,
  cpu_psi_some_total INTEGER,
  mem_psi_some_avg10 REAL,
  mem_psi_some_avg60 REAL,
  mem_psi_some_total INTEGER,
  mem_psi_full_avg60 REAL,
  io_psi_some_avg60 REAL,
  io_psi_full_avg60 REAL,
  PRIMARY KEY (ts, cgroup)
);
CREATE INDEX IF NOT EXISTS idx_samples_cgroup_ts ON samples(cgroup, ts);
"""

# Columns in SCHEMA after (ts, cgroup), in order. Must match insert below.
COLUMNS = [
    "memory_current", "memory_peak", "memory_max", "memory_high",
    "memory_events_low", "memory_events_high", "memory_events_max",
    "memory_events_oom", "memory_events_oom_kill",
    "cpu_usage_usec", "cpu_nr_throttled", "cpu_throttled_usec",
    "cpu_psi_some_avg10", "cpu_psi_some_avg60", "cpu_psi_some_total",
    "mem_psi_some_avg10", "mem_psi_some_avg60", "mem_psi_some_total",
    "mem_psi_full_avg60",
    "io_psi_some_avg60", "io_psi_full_avg60",
]


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


def sample_one(cg):
    name = str(cg.relative_to(CGROUP_ROOT))
    m_events = read_kv(cg / "memory.events")
    cpu_stat = read_kv(cg / "cpu.stat")
    m_psi = read_pressure(cg / "memory.pressure")
    c_psi = read_pressure(cg / "cpu.pressure")
    i_psi = read_pressure(cg / "io.pressure")
    return {
        "cgroup": name,
        "memory_current": read_int(cg / "memory.current"),
        "memory_peak": read_int(cg / "memory.peak"),
        "memory_max": read_int(cg / "memory.max"),
        "memory_high": read_int(cg / "memory.high"),
        "memory_events_low": m_events.get("low"),
        "memory_events_high": m_events.get("high"),
        "memory_events_max": m_events.get("max"),
        "memory_events_oom": m_events.get("oom"),
        "memory_events_oom_kill": m_events.get("oom_kill"),
        "cpu_usage_usec": cpu_stat.get("usage_usec"),
        "cpu_nr_throttled": cpu_stat.get("nr_throttled"),
        "cpu_throttled_usec": cpu_stat.get("throttled_usec"),
        "cpu_psi_some_avg10": c_psi.get("some_avg10"),
        "cpu_psi_some_avg60": c_psi.get("some_avg60"),
        "cpu_psi_some_total": c_psi.get("some_total"),
        "mem_psi_some_avg10": m_psi.get("some_avg10"),
        "mem_psi_some_avg60": m_psi.get("some_avg60"),
        "mem_psi_some_total": m_psi.get("some_total"),
        "mem_psi_full_avg60": m_psi.get("full_avg60"),
        "io_psi_some_avg60": i_psi.get("some_avg60"),
        "io_psi_full_avg60": i_psi.get("full_avg60"),
    }


def write_samples(db, ts, rows):
    sql = (
        "INSERT OR REPLACE INTO samples (ts, cgroup, "
        + ", ".join(COLUMNS)
        + ") VALUES ("
        + ", ".join(["?"] * (len(COLUMNS) + 2))
        + ")"
    )
    db.executemany(
        sql,
        [(ts, r["cgroup"], *[r[c] for c in COLUMNS]) for r in rows],
    )
    db.commit()


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    ts = int(time.time())
    rows = [sample_one(cg) for cg in discover_cgroups()]
    rows = [r for r in rows if r["memory_current"] is not None]
    if rows:
        write_samples(db, ts, rows)
    print(f"[{ts}] wrote {len(rows)} samples to {DB_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
