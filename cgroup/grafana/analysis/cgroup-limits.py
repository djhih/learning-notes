#!/usr/bin/env python3
"""
cgroup-limits.py — end-to-end cgroup usage analysis in one program.

  1. Pull the TSDB out of the source Prometheus container (docker cp).
  2. Spin up a throwaway analysis Prometheus on that copy (default port 9091).
  3. Analyse: per-user cross-host max usage + suggested systemd limits,
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
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ANALYZE_NAME = "cgroup-analyze"


# ---------- shell / docker ----------
def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def docker(args, check=True):
    r = run(["docker", *args])
    if check and r.returncode != 0:
        sys.exit(f"!! docker {' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout


def have_docker():
    if run(["docker", "version"]).returncode != 0:
        sys.exit("!! 需要可用的 docker(且你的帳號要能跑 docker,例如在 docker 群組)。")


# ---------- http ----------
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
        print(f"!! 查詢失敗: {expr}\n   {d}", file=sys.stderr)
        return []
    return [(r["metric"], float(r["value"][1])) for r in d["data"]["result"]]


# ---------- step 1: pull data ----------
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
        sys.exit("!! 找不到來源 Prometheus 容器。用 --source <容器名> 指定,或 --data <資料夾> 跳過抓取。")
    path = tsdb_path(cid)
    print(f"==> 來源容器: {name} ({cid[:12]})  TSDB: {path}")
    size = run(["docker", "exec", cid, "du", "-sh", path]).stdout.strip()
    if size:
        print(f"    資料大小: {size}")
    datadir = fresh_dir(os.path.join(workdir, "data"))
    print(f"==> 複製資料到 {datadir} ...")
    r = run(["docker", "cp", f"{cid}:{path}/.", datadir])
    if r.returncode != 0:
        sys.exit(f"!! docker cp 失敗:\n{r.stderr.strip()}")
    return datadir


# ---------- step 2: serve ----------
def serve(datadir, port, image):
    docker(["rm", "-f", ANALYZE_NAME], check=False)
    print(f"==> 啟動分析用 Prometheus @ http://localhost:{port}(retention 設超大,不刪資料)")
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
    print("==> 等待就緒", end="", flush=True)
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
    sys.exit("!! 分析用 Prometheus 沒起來。log:\n" + docker(["logs", "--tail", "25", ANALYZE_NAME], check=False))


# ---------- step 3: analyse ----------
def detect_at(base):
    # Snapshot data is static, so "now" is usually empty — evaluate at the
    # newest sample instead (TSDB head max time).
    try:
        hs = api(base, "/api/v1/status/tsdb")["data"].get("headStats", {})
        mt = int(hs.get("maxTime", 0))
        return mt // 1000 if mt > 0 else None
    except Exception:
        return None


def keyed(rows, label):
    return {m.get(label, "?"): v for m, v in rows}


def h_bytes(n):
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PiB"


def mib(n):
    return f"{max(0, round(float(n) / 2**20))}M"


def analyze(base, label, window, at=0):
    L, W = label, window
    at = at or detect_at(base)  # default: newest sample in the snapshot
    if at:
        print(f"==> 評估時間鎖定 at={at}(資料最新時間)")

    names = api(base, "/api/v1/label/__name__/values")["data"]
    have = set(n for n in names if n.startswith("cgroup_"))
    if "cgroup_memory_current_bytes" not in have:
        sys.exit("!! 資料裡沒有 cgroup_memory_current_bytes,無法分析。")
    io_metrics = sorted(n for n in have if "io_" in n and "bytes" in n)

    # Collapse instance with max by(<label>): one row per user, taking the
    # largest usage across hosts — a single fleet-wide limit.
    def agg(inner):
        return f"max by ({L}) ({inner})"

    mem_peak = keyed(q(base, agg(f"max_over_time(cgroup_memory_current_bytes[{W}])"), at), L)
    mem_p95 = keyed(q(base, agg(f"quantile_over_time(0.95, cgroup_memory_current_bytes[{W}])"), at), L)
    mem_p50 = keyed(q(base, agg(f"quantile_over_time(0.50, cgroup_memory_current_bytes[{W}])"), at), L)
    cpu_p95 = keyed(q(base, agg(f"quantile_over_time(0.95, rate(cgroup_cpu_usage_usec_total[5m])[{W}:5m]) / 1e6"), at), L)
    cpu_peak = keyed(q(base, agg(f"max_over_time(rate(cgroup_cpu_usage_usec_total[5m])[{W}:5m]) / 1e6"), at), L)

    io_p95, io_peak = {}, {}
    if io_metrics:  # older exporters have no io bytes metric
        rate_sum = " + ".join(f"rate({m}[5m])" for m in io_metrics)
        io_p95 = keyed(q(base, agg(f"quantile_over_time(0.95, ({rate_sum})[{W}:5m])"), at), L)
        io_peak = keyed(q(base, agg(f"max_over_time(({rate_sum})[{W}:5m])"), at), L)

    users = sorted(set(mem_peak) | set(cpu_p95) | set(io_p95))
    if not users:
        sys.exit("!! 查不到資料。可能 --window 太短(資料在更早以前)或 --label 名稱不對。")

    rows = []
    for u in users:
        mp, mk, m5, cpk = mem_p95.get(u, 0), mem_peak.get(u, 0), mem_p50.get(u, 0), cpu_peak.get(u, 0)
        row = {
            L: u,
            # observed usage
            "mem_p95": h_bytes(mp), "mem_peak": h_bytes(mk),
            "cpu_p95_cores": round(cpu_p95.get(u, 0), 2), "cpu_peak_cores": round(cpk, 2),
            # suggested limits: High=p95 (soft), Max=peak*1.3 (hard), Low=p50 (protected floor)
            "MemoryHigh": mib(mp), "MemoryMax": mib(mk * 1.3), "MemoryLow": mib(m5),
            "CPUWeight": 100, "CPUQuota": f"{max(1, round(cpk * 1.2 * 100))}%", "IOWeight": 100,
        }
        if io_metrics:
            row["io_p95"] = h_bytes(io_p95.get(u, 0)) + "/s"
            row["io_peak"] = h_bytes(io_peak.get(u, 0)) + "/s"
        rows.append(row)
    return rows, io_metrics


# ---------- output ----------
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


def write_csv(rows, out):
    import csv
    cols = list(rows[0].keys())
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


# ---------- main ----------
def main():
    p = argparse.ArgumentParser(description="cgroup 用量分析:抓資料→起 Prometheus→分析,一鍵完成")
    p.add_argument("--source", help="來源 docker Prometheus 容器名/ID(預設自動偵測發布 9090 的容器)")
    p.add_argument("--data", help="改用現有 TSDB 資料夾,跳過 docker cp 抓取")
    p.add_argument("--workdir", default="./cgroup-analysis", help="工作目錄(放抓下來的資料與輸出)")
    p.add_argument("--port", type=int, default=9091)
    p.add_argument("--image", default="prom/prometheus:v2.55.1")
    p.add_argument("--label", default="service", help="辨識 user 的 label")
    p.add_argument("--window", default="15d")
    p.add_argument("--at", type=int, default=0, help="評估時間 unix 秒;0=自動抓資料最新時間")
    p.add_argument("--out", help="CSV 輸出路徑(預設 <workdir>/cgroup_limits.csv)")
    p.add_argument("--keep", action="store_true", help="分析完保留分析用容器(預設收掉)")
    a = p.parse_args()

    have_docker()
    os.makedirs(a.workdir, exist_ok=True)
    out = a.out or os.path.join(a.workdir, "cgroup_limits.csv")

    # 1. data
    if a.data:
        datadir = os.path.abspath(a.data)
        print(f"==> 使用現有資料夾: {datadir}")
    else:
        datadir = pull(a.source, a.workdir)

    # 2. serve
    base = serve(datadir, a.port, a.image)

    # 3. analyse (always tear the container down unless --keep)
    try:
        rows, io_metrics = analyze(base, a.label, a.window, a.at)
        print()
        render(rows)
        write_csv(rows, out)
        print(f"\n共 {len(rows)} 列  →  {out}")
        if not io_metrics:
            print("註:無 io bytes metric(舊版 exporter),IO 僅給 IOWeight。", file=sys.stderr)
        print("提醒:數值為『跨 host 最大』的全機通用值,對用量小的 host 會偏鬆;"
              "MemoryMax/CPUQuota 是硬限制,套用前先複核並在 canary 上驗證 OOM=0、PSI 不爆。",
              file=sys.stderr)
    finally:
        if a.keep:
            print(f"\n(分析用容器保留: {ANALYZE_NAME} @ {base};停用: docker rm -f {ANALYZE_NAME})")
        else:
            docker(["rm", "-f", ANALYZE_NAME], check=False)
            print(f"\n==> 已收掉分析用容器 {ANALYZE_NAME}(抓下來的資料保留在 {datadir})")


if __name__ == "__main__":
    main()
