#!/usr/bin/env python3
"""CPU burner: spin N workers to drive CPU usage.

On the Grafana "Host Detail" dashboard this lights the CPU-usage rate panel;
inside a CPUQuota-capped cgroup it also lights CPU throttling and CPU PSI as the
workers are held below the quota. (Quota enforcement on --user units needs the
cpu controller delegated to the user manager; use run.sh --system for
guaranteed throttling.)
"""
import argparse
import multiprocessing
import time


def spin(deadline):
    x = 0
    while time.monotonic() < deadline:
        x = (x * x + 1) & 0xFFFFFFFF


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--duration", type=float, default=600.0)
    args = ap.parse_args()

    deadline = time.monotonic() + args.duration
    procs = [multiprocessing.Process(target=spin, args=(deadline,), daemon=True)
             for _ in range(args.workers)]
    for p in procs:
        p.start()
    print(f"burning on {args.workers} workers for {args.duration:.0f}s", flush=True)
    for p in procs:
        p.join()


if __name__ == "__main__":
    main()
