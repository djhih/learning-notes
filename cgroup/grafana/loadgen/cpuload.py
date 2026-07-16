#!/usr/bin/env python3
"""Adjustable CPU load: hold each worker at a chosen duty cycle.

burn.py pins workers at 100%; this one holds a *known* CPU level so the
dashboard numbers can be checked against ground truth: N workers at --load L
should read about N*L percent total for this command on Host Detail
"Top processes — CPU" and the User Processes drill-down.

Duty-cycle scheduling: each 100 ms period spins for L% of it, sleeps the rest.
"""
import argparse
import multiprocessing
import time

PERIOD = 0.1  # seconds per duty cycle; short enough to read as steady load


def worker(load, deadline):
    busy = PERIOD * load / 100.0
    x = 0
    while time.monotonic() < deadline:
        start = time.monotonic()
        while time.monotonic() - start < busy:
            x = (x * x + 1) & 0xFFFFFFFF
        rest = PERIOD - (time.monotonic() - start)
        if rest > 0:
            time.sleep(rest)


def main():
    # fork keeps the workers' argv identical to this script's, so the exporter
    # and htop attribute them to "python3 cpuload.py"; the forkserver default
    # (Linux, Python >= 3.14) would show them as bare "python3 -c ...".
    multiprocessing.set_start_method("fork")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=2, help="worker processes")
    ap.add_argument("--load", type=float, default=50.0, help="per-worker CPU%% (0-100)")
    ap.add_argument("--duration", type=float, default=600.0, help="seconds to run")
    args = ap.parse_args()
    load = max(0.0, min(100.0, args.load))

    deadline = time.monotonic() + args.duration
    procs = [multiprocessing.Process(target=worker, args=(load, deadline), daemon=True)
             for _ in range(args.workers)]
    for p in procs:
        p.start()
    print(f"{args.workers} workers at {load:.0f}% for {args.duration:.0f}s "
          f"— expect ~{args.workers * load:.0f}% total on the dashboards", flush=True)
    for p in procs:
        p.join()


if __name__ == "__main__":
    main()
