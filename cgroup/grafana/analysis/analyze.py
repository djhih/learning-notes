#!/usr/bin/env python3
"""
analyze.py — 算出每個 user/cgroup 的 memory / cpu / io 用量,並給出建議的
systemd 限制值(MemoryHigh/Max/Low、CPUQuota/Weight、IOWeight)。

用量為「跨 host 最大」:同一 user 取所有 host 中最保守(最大)的用量,
產出一份可套用到全部機器的通用限制(外層 max by (<label>))。

前提:先用 check-data.sh 把「複製出來的 /prometheus 資料夾」掛成本機 Prometheus
(預設 http://localhost:9091),這支再對它查。

只依賴 Python 標準函式庫(不用 pip)。

用法:
  python3 analyze.py                                  # 預設 localhost:9091, label=service, 15d
  python3 analyze.py --label service --window 15d
  python3 analyze.py --at 1720800000                  # 指定評估時間(unix 秒);預設自動抓資料最新時間
  python3 analyze.py --out my_limits.csv
"""
import argparse, csv, json, sys, urllib.parse, urllib.request


def api(base, path, params=None):
    url = base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def q(base, expr, at):
    """instant query;at=None 時用 Prometheus 的『現在』。回傳 [(labels, value), ...]"""
    params = {"query": expr}
    if at:
        params["time"] = at
    d = api(base, "/api/v1/query", params)
    if d.get("status") != "success":
        print(f"!! 查詢失敗: {expr}\n   {d}", file=sys.stderr)
        return []
    return [(r["metric"], float(r["value"][1])) for r in d["data"]["result"]]


def keyed(rows, label):
    out = {}
    for m, v in rows:
        out[m.get(label, "?")] = v
    return out


def h_bytes(n):
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PiB"


def mib(n):
    # systemd 的 M = MiB
    return f"{max(0, round(float(n) / 2**20))}M"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prom", default="http://localhost:9091")
    p.add_argument("--label", default="service", help="辨識 user 的 label 名")
    p.add_argument("--window", default="15d")
    p.add_argument("--at", type=int, default=0, help="評估時間 unix 秒;0=自動抓資料最新時間")
    p.add_argument("--out", default="cgroup_limits.csv")
    a = p.parse_args()
    base, W, L = a.prom.rstrip("/"), a.window, a.label

    # 靜態快照的『現在』通常沒資料 → 評估時間鎖到 TSDB head 最新時間
    at = a.at or None
    if not at:
        try:
            hs = api(base, "/api/v1/status/tsdb")["data"].get("headStats", {})
            mt = int(hs.get("maxTime", 0))
            if mt > 0:
                at = mt // 1000
                print(f"# 評估時間鎖定資料最新時間 at={at}", file=sys.stderr)
        except Exception as e:
            print(f"# 自動抓最新時間失敗({e}),改用 Prometheus 現在時間", file=sys.stderr)

    # 有哪些 metric
    names = api(base, "/api/v1/label/__name__/values")["data"]
    have = set(n for n in names if n.startswith("cgroup_"))
    if "cgroup_memory_current_bytes" not in have:
        sys.exit("!! 缺 cgroup_memory_current_bytes,無法分析。先跑 check-data.sh 確認資料。")

    # 舊版 exporter 可能沒有 io bytes;動態偵測
    io_metrics = sorted(n for n in have if "io_" in n and "bytes" in n)

    # 跨 host 取最大 → 產「全機通用」限制:同一 user 在所有 host 中最保守(最大)的
    # 用量。外層包 max by (<label>) (...) 把 instance 收斂掉,一個 user 一列。
    # 對用量小的 host,限制會偏鬆(這是刻意的:一份設定要套所有機器)。
    agg = lambda inner: f"max by ({L}) ({inner})"

    # 記憶體(bytes)
    mem_peak = keyed(q(base, agg(f"max_over_time(cgroup_memory_current_bytes[{W}])"), at), L)
    mem_p95 = keyed(q(base, agg(f"quantile_over_time(0.95, cgroup_memory_current_bytes[{W}])"), at), L)
    mem_p50 = keyed(q(base, agg(f"quantile_over_time(0.50, cgroup_memory_current_bytes[{W}])"), at), L)

    # CPU(核心數)= rate(usec)/1e6
    cpu_p95 = keyed(q(base, agg(f"quantile_over_time(0.95, rate(cgroup_cpu_usage_usec_total[5m])[{W}:5m]) / 1e6"), at), L)
    cpu_peak = keyed(q(base, agg(f"max_over_time(rate(cgroup_cpu_usage_usec_total[5m])[{W}:5m]) / 1e6"), at), L)

    # IO(bytes/s,讀+寫;沒有就跳過)
    io_p95, io_peak = {}, {}
    if io_metrics:
        rate_sum = " + ".join(f"rate({m}[5m])" for m in io_metrics)
        io_p95 = keyed(q(base, agg(f"quantile_over_time(0.95, ({rate_sum})[{W}:5m])"), at), L)
        io_peak = keyed(q(base, agg(f"max_over_time(({rate_sum})[{W}:5m])"), at), L)

    users = sorted(set(mem_peak) | set(cpu_p95) | set(io_p95))
    if not users:
        sys.exit("!! 查不到資料。可能 --window 太短(資料在更早以前)或 --label 名稱不對。先跑 check-data.sh。")

    rows = []
    for u in users:
        mp, mk, m5 = mem_p95.get(u, 0), mem_peak.get(u, 0), mem_p50.get(u, 0)
        cpk = cpu_peak.get(u, 0)
        row = {
            L: u,
            # ---- 實際用量(給你判斷) ----
            "mem_p95": h_bytes(mp),
            "mem_peak": h_bytes(mk),
            "cpu_p95_cores": round(cpu_p95.get(u, 0), 2),
            "cpu_peak_cores": round(cpk, 2),
            # ---- 建議限制值(套 systemctl set-property 前請人工複核) ----
            "MemoryHigh": mib(mp),          # 軟限制 = p95(超過先回收,不殺)
            "MemoryMax": mib(mk * 1.3),     # 硬底線 = peak × 1.3
            "MemoryLow": mib(m5),           # 保底 = p50(全域壓力下不被回收)
            "CPUWeight": 100,               # 相對權重(只在搶資源時生效)
            "CPUQuota": f"{max(1, round(cpk * 1.2 * 100))}%",  # 峰值 × 1.2
            "IOWeight": 100,
        }
        if io_metrics:
            row["io_p95"] = h_bytes(io_p95.get(u, 0)) + "/s"
            row["io_peak"] = h_bytes(io_peak.get(u, 0)) + "/s"
        rows.append(row)

    # 印成對齊表格
    cols = list(rows[0].keys())
    wid = {c: max(len(c), max(len(str(r[c])) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(wid[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r[c]).ljust(wid[c]) for c in cols))

    with open(a.out, "w", newline="") as f:
        cw = csv.DictWriter(f, fieldnames=cols)
        cw.writeheader()
        cw.writerows(rows)
    print(f"\n寫出 {len(rows)} 筆 -> {a.out}")

    if not io_metrics:
        print("註:此資料沒有 io bytes metric(舊版 exporter)。IO 只能用 IOWeight 相對權重 + 看 io PSI,"
              "無法給絕對用量/上限。", file=sys.stderr)
    print("提醒:數值為『跨 host 最大』的全機通用值,對用量小的 host 會偏鬆。"
          "MemoryMax/CPUQuota 是硬限制,套用前先人工複核,並先在一台 canary 上驗證 "
          "OOM=0、PSI 不爆。", file=sys.stderr)


if __name__ == "__main__":
    main()
