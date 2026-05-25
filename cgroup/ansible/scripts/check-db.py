#!/usr/bin/env python3
"""Diagnose cgroup-baseline SQLite DB. Run on target server (needs root)."""
import os
import sqlite3
import sys

DB = "/var/lib/cgroup-baseline/samples.db"


def fail(msg, code=1):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def main():
    if not os.path.exists(DB):
        fail(f"DB not found at {DB}. Has the collector deployed? Has the timer fired?")

    size_mb = os.path.getsize(DB) / 1024 / 1024
    print(f"db_size:  {size_mb:.2f} MB ({DB})")

    db = sqlite3.connect(DB)

    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    if "samples" not in tables:
        fail(f"'samples' table missing. Found tables: {tables}")

    rows = db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    print(f"rows:     {rows}")

    if rows == 0:
        print("WARN: table exists but empty. Wait for next timer fire (≤60s).")
        return

    cgroups = db.execute("SELECT COUNT(DISTINCT cgroup) FROM samples").fetchone()[0]
    print(f"cgroups:  {cgroups}")

    span_row = db.execute("SELECT MIN(ts), MAX(ts) FROM samples").fetchone()
    ts_min, ts_max = span_row[0], span_row[1]
    if ts_min is None or ts_max is None:
        print("WARN: timestamps are NULL — schema corruption?")
    else:
        span = ts_max - ts_min
        print(f"timespan: {span} sec ({span/60:.1f} min, "
              f"first={ts_min}, last={ts_max})")

    distinct_ts = db.execute("SELECT COUNT(DISTINCT ts) FROM samples").fetchone()[0]
    print(f"samples:  {distinct_ts} (timer fired {distinct_ts} times)")

    print("\ntop 10 by memory_current at most-recent sample:")
    rows_iter = db.execute("""
        SELECT cgroup, memory_current
        FROM samples
        WHERE ts = (SELECT MAX(ts) FROM samples)
          AND memory_current IS NOT NULL
        ORDER BY memory_current DESC
        LIMIT 10
    """)
    found = False
    for r in rows_iter:
        cg, mem = r[0], r[1]
        mb = (mem or 0) / 1024 / 1024
        print(f"  {mb:>8.1f} MB  {cg}")
        found = True
    if not found:
        print("  (no rows with non-null memory_current in latest sample)")

    print("\nlast 5 sample timestamps:")
    for r in db.execute(
        "SELECT ts, COUNT(*) FROM samples GROUP BY ts ORDER BY ts DESC LIMIT 5"
    ):
        print(f"  ts={r[0]}  rows={r[1]}")


if __name__ == "__main__":
    main()
