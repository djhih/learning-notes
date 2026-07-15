#!/usr/bin/env python3
"""
cgroup-limits.py — end-to-end cgroup usage analysis in one program.

  1. Pull the TSDB out of the source Prometheus container (docker cp).
  2. Spin up a throwaway analysis Prometheus on that copy (default port 9091).
  3. Analyse: per-host, per-user usage + suggested systemd limits,
     print a table and write a CSV. The container is torn down afterwards
     (use --keep to retain it).

Stdlib + docker CLI only.

Examples:
  python3 cgroup-limits.py                       # auto-detect source (container publishing 9090)
  python3 cgroup-limits.py --source cgroup-prometheus
  python3 cgroup-limits.py --data ./prometheus-data     # reuse an existing dir, skip the copy
  python3 cgroup-limits.py --window 30d --label service --out limits.csv --keep
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ANALYZE_NAME = "cgroup-analyze"

# MemoryMax = min(peak * MEM_FACTOR, host root-cgroup peak * SYS_FRAC)
MEM_FACTOR = 1.5
SYS_FRAC = 0.9


# shell / docker
def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def docker(args, check=True):
    r = run(["docker", *args])
    if check and r.returncode != 0:
        sys.exit(f"!! docker {' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout


def have_docker():
    if run(["docker", "version"]).returncode != 0:
        sys.exit("!! docker not available (is your user in the docker group?)")


# http
def api(base, path, params=None):
    url = base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def q(base, expr, at):
    params = {"query": expr}
    if at:
        params["time"] = at
    d = api(base, "/api/v1/query", params)
    if d.get("status") != "success":
        print(f"!! query failed: {expr}\n   {d}", file=sys.stderr)
        return []
    return [(r["metric"], float(r["value"][1])) for r in d["data"]["result"]]


# step 1: pull data
def detect_source():
    for filt in ("publish=9090", "name=prometheus"):
        for line in docker(["ps", "--filter", filt, "--format", "{{.ID}} {{.Names}}"]).splitlines():
            cid, _, name = line.partition(" ")
            if name.strip() != ANALYZE_NAME:
                return cid, name.strip()
    return None, None


def tsdb_path(cid):
    # Read the actual data path from the container's args; fall back to /prometheus.
    for a in docker(["inspect", "--format", "{{range .Args}}{{println .}}{{end}}", cid]).splitlines():
        if a.startswith("--storage.tsdb.path="):
            return a.split("=", 1)[1]
    return "/prometheus"


def fresh_dir(base):
    # Return an empty dir: wipe `base`, or fall back to base-1, base-2... if it's
    # left root-owned from a previous run and can't be removed.
    d, i = base, 1
    while os.path.exists(d):
        try:
            shutil.rmtree(d)
            break
        except Exception:
            d = f"{base}-{i}"
            i += 1
    os.makedirs(d, exist_ok=True)
    return d


def pull(source, workdir):
    cid, name = (source, source) if source else detect_source()
    if not cid:
        sys.exit("!! Prometheus not found. use --source <container>, or --data <dir> to skip the pull.")
    path = tsdb_path(cid)
    print(f"==> source: {name} ({cid[:12]})  TSDB: {path}")
    size = run(["docker", "exec", cid, "du", "-sh", path]).stdout.strip()
    if size:
        print(f"    size: {size}")
    datadir = fresh_dir(os.path.join(workdir, "data"))
    print(f"==> copying data to {datadir} ...")
    r = run(["docker", "cp", f"{cid}:{path}/.", datadir])
    if r.returncode != 0:
        sys.exit(f"!! docker cp failed:\n{r.stderr.strip()}")
    return datadir


# step 2: serve 
def serve(datadir, port, image):
    docker(["rm", "-f", ANALYZE_NAME], check=False)
    print(f"==> starting analysis Prometheus @ http://localhost:{port} (huge retention, nothing pruned)")
    docker([
        "run", "-d", "--name", ANALYZE_NAME,
        "--user", os.environ.get("PUID", "0:0"),   # run as root so mount perms never block writes
        "-p", f"127.0.0.1:{port}:9090",
        "-v", f"{os.path.abspath(datadir)}:/prometheus",
        image,
        "--config.file=/etc/prometheus/prometheus.yml",
        "--storage.tsdb.path=/prometheus",
        "--storage.tsdb.retention.time=100y",
        "--storage.tsdb.retention.size=0",
    ])
    base = f"http://localhost:{port}"
    print("==> waiting for ready", end="", flush=True)
    for _ in range(60):
        try:
            with urllib.request.urlopen(base + "/-/ready", timeout=2) as r:
                if r.status == 200:
                    print(" OK")
                    return base
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(1)
    print()
    sys.exit("!! analysis Prometheus did not start. logs:\n" + docker(["logs", "--tail", "25", ANALYZE_NAME], check=False))


# step 3: analyse
def detect_at(base):
    # Snapshot data is static, so "now" is usually empty — evaluate at the
    # newest sample instead (TSDB head max time).
    try:
        hs = api(base, "/api/v1/status/tsdb")["data"].get("headStats", {})
        mt = int(hs.get("maxTime", 0))
        return mt // 1000 if mt > 0 else None
    except Exception:
        return None


def keyed(rows, labels):
    # key each series by the given label tuple, e.g. (instance, service)
    return {tuple(m.get(l, "?") for l in labels): v for m, v in rows}


def h_bytes(n):
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PiB"


def mib(n):
    return f"{max(0, round(float(n) / 2**20))}M"


def analyze(base, host_label, user_label, window, at=0):
    H, L, W = host_label, user_label, window
    at = at or detect_at(base)  # default: newest sample in the snapshot
    if at:
        print(f"==> eval time at={at} (newest sample)")

    names = api(base, "/api/v1/label/__name__/values")["data"]
    have = set(n for n in names if n.startswith("cgroup_"))
    if "cgroup_memory_current_bytes" not in have:
        sys.exit("!! cgroup_memory_current_bytes missing; cannot analyse.")
    io_metrics = sorted(n for n in have if "io_" in n and "bytes" in n)

    # One row per (host, user): each host is sized from its own usage, no merging.
    by = (H, L)
    mem_peak = keyed(q(base, f"max_over_time(cgroup_memory_current_bytes[{W}])", at), by)
    mem_p95 = keyed(q(base, f"quantile_over_time(0.95, cgroup_memory_current_bytes[{W}])", at), by)
    mem_p50 = keyed(q(base, f"quantile_over_time(0.50, cgroup_memory_current_bytes[{W}])", at), by)
    cpu_p95 = keyed(q(base, f"quantile_over_time(0.95, rate(cgroup_cpu_usage_usec_total[5m])[{W}:5m]) / 1e6", at), by)
    cpu_peak = keyed(q(base, f"max_over_time(rate(cgroup_cpu_usage_usec_total[5m])[{W}:5m]) / 1e6", at), by)

    # Per-host memory ceiling: max over all cgroups on a host == the root cgroup peak.
    sys_mem = keyed(q(base, f"max by ({H}) (max_over_time(cgroup_memory_current_bytes[{W}]))", at), (H,))

    io_p95, io_peak = {}, {}
    if io_metrics:  # older exporters have no io bytes metric
        rate_sum = " + ".join(f"rate({m}[5m])" for m in io_metrics)
        io_p95 = keyed(q(base, f"quantile_over_time(0.95, ({rate_sum})[{W}:5m])", at), by)
        io_peak = keyed(q(base, f"max_over_time(({rate_sum})[{W}:5m])", at), by)

    idx = sorted(set(mem_peak) | set(cpu_p95) | set(io_p95))
    if not idx:
        sys.exit("!! no data. --window may be too short, or --label is wrong.")

    rows = []
    for k in idx:  # k = (host, user)
        mp, mk, m5, cpk = mem_p95.get(k, 0), mem_peak.get(k, 0), mem_p50.get(k, 0), cpu_peak.get(k, 0)
        mem_max = mk * MEM_FACTOR
        cap = sys_mem.get((k[0],), 0) * SYS_FRAC   # host root-cgroup peak * 0.9
        if cap:
            mem_max = min(mem_max, cap)
        row = {
            H: k[0], L: k[1],
            # observed usage
            "mem_p95": h_bytes(mp), "mem_peak": h_bytes(mk),
            "cpu_p95_cores": round(cpu_p95.get(k, 0), 2), "cpu_peak_cores": round(cpk, 2),
            # suggested limits: High=p95 (soft), Low=p50 (floor),
            # Max=min(peak*MEM_FACTOR, host_root_peak*SYS_FRAC) (hard)
            "MemoryHigh": mib(mp), "MemoryMax": mib(mem_max), "MemoryLow": mib(m5),
            "CPUWeight": 100, "CPUQuota": f"{max(1, round(cpk * 1.2 * 100))}%", "IOWeight": 100,
        }
        if io_metrics:
            row["io_p95"] = h_bytes(io_p95.get(k, 0)) + "/s"
            row["io_peak"] = h_bytes(io_peak.get(k, 0)) + "/s"
        rows.append(row)
    return rows, io_metrics


# output
def render(rows):
    cols = list(rows[0].keys())
    data = [[str(r[c]) for c in cols] for r in rows]
    wid = [max(len(cols[i]), *(len(d[i]) for d in data)) for i in range(len(cols))]

    def cell(text, i):
        # user column left-aligned, numeric/unit columns right-aligned
        return text.ljust(wid[i]) if i == 0 else text.rjust(wid[i])

    def border(left, mid, right):
        return left + mid.join("─" * (wid[i] + 2) for i in range(len(cols))) + right

    def line(vals):
        return "│ " + " │ ".join(cell(vals[i], i) for i in range(len(cols))) + " │"

    print(border("┌", "┬", "┐"))
    print(line(cols))
    print(border("├", "┼", "┤"))
    for d in data:
        print(line(d))
    print(border("└", "┴", "┘"))


def render_by_host(rows, host_label):
    # one table per host; drop the host column since the heading shows it
    for h in sorted({r[host_label] for r in rows}):
        sub = [{k: v for k, v in r.items() if k != host_label} for r in rows if r[host_label] == h]
        print(f"\n=== {host_label} = {h} ({len(sub)} users) ===")
        render(sub)


def filter_rows(rows, label, include, exclude):
    # keep only real users for the CSV; the on-screen tables stay unfiltered
    inc = re.compile(include) if include else None
    exc = re.compile(exclude) if exclude else None
    out = []
    for r in rows:
        u = r.get(label, "")
        if inc and not inc.search(u):
            continue
        if exc and exc.search(u):
            continue
        out.append(r)
    return out


def write_csv(rows, out):
    import csv
    cols = list(rows[0].keys())
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


# main
def main():
    p = argparse.ArgumentParser(description="cgroup usage analysis: pull data, start Prometheus, analyse — one shot")
    p.add_argument("--source", help="source Prometheus container name/ID (default: auto-detect the one publishing 9090)")
    p.add_argument("--data", help="use an existing TSDB dir, skip docker cp")
    p.add_argument("--workdir", default="./cgroup-analysis", help="work dir for pulled data and output")
    p.add_argument("--port", type=int, default=9091)
    p.add_argument("--image", default="prom/prometheus:v2.55.1")
    p.add_argument("--label", default="service", help="label identifying a user")
    p.add_argument("--host-label", default="instance", help="label identifying a host")
    p.add_argument("--include", help="regex: keep only users whose --label value matches")
    p.add_argument("--exclude", help="regex: drop users whose --label value matches")
    p.add_argument("--window", default="15d")
    p.add_argument("--at", type=int, default=0, help="eval time (unix seconds); 0 = auto (newest sample)")
    p.add_argument("--out", help="CSV output path (default <workdir>/cgroup_limits.csv)")
    p.add_argument("--keep", action="store_true", help="keep the analysis container afterwards (default: remove)")
    a = p.parse_args()

    have_docker()
    os.makedirs(a.workdir, exist_ok=True)
    out = a.out or os.path.join(a.workdir, "cgroup_limits.csv")

    # 1. data
    if a.data:
        datadir = os.path.abspath(a.data)
        print(f"==> using existing dir: {datadir}")
    else:
        datadir = pull(a.source, a.workdir)

    # 2. serve
    base = serve(datadir, a.port, a.image)

    # 3. analyse (always tear the container down unless --keep)
    try:
        rows, io_metrics = analyze(base, a.host_label, a.label, a.window, a.at)
        render_by_host(rows, a.host_label)   # tables show every row
        csv_rows = filter_rows(rows, a.label, a.include, a.exclude)
        if not csv_rows:
            sys.exit("!! no rows left for the CSV after --include/--exclude.")
        write_csv(csv_rows, out)
        print(f"\ntable: {len(rows)} rows (all)   CSV: {len(csv_rows)} rows  →  {out}")
        if not io_metrics:
            print("note: no io bytes metric (old exporter); IO is IOWeight only.", file=sys.stderr)
        print("note: one row per (host, user); each host is sized from its own usage. "
              "MemoryMax/CPUQuota are hard limits — review, then verify OOM=0 and PSI on a canary first.",
              file=sys.stderr)
    finally:
        if a.keep:
            print(f"\n(analysis container kept: {ANALYZE_NAME} @ {base}; stop with: docker rm -f {ANALYZE_NAME})")
        else:
            docker(["rm", "-f", ANALYZE_NAME], check=False)
            print(f"\n==> removed analysis container {ANALYZE_NAME} (data kept in {datadir})")


if __name__ == "__main__":
    main()
