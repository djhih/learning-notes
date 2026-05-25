# Day 8：baseline 整理 → 報告

> 跑了一週，現在要把資料整理成「Day 9 canary 用什麼規則」+「下個 sprint 要看哪些 PSI 候選」+「填進報告的內容」。

## 流程概覽

```
1. fetch          (ansible 拉 DB 回控制機)
2. quality check  (確認資料品質)
3. analyze        (跑 analyze.py 拿到 top services / rules / PSI)
4. transcribe     (把產出貼進報告 §2.2 / §5 / 附錄)
5. apply          (host_vars 寫好，準備 Day 9 canary)
```

---

## Step 1: 把 DB 拉回本機

```bash
cd /home/jjd/Documents/llt/cgroup/ansible

mkdir -p ~/cgroup-data

ansible -i inventory.ini baseline_targets -b -m fetch \
  -a 'src=/var/lib/cgroup-baseline/samples.db dest=~/cgroup-data/ flat=no' \
  --ask-pass --ask-become-pass

# DB 會在：
ls -lh ~/cgroup-data/192.168.0.1/var/lib/cgroup-baseline/samples.db
```

預期檔案大小依規模而定（參考 deploy-verify.md §5 那張對照表）。

## Step 2: 裝 pandas（如果還沒）

```bash
python3 -c "import pandas" 2>/dev/null && echo "OK" || pip install --user pandas
```

不想污染系統可以用 venv：

```bash
python3 -m venv ~/cgroup-venv
source ~/cgroup-venv/bin/activate
pip install pandas
```

## Step 3: 跑分析

```bash
cd /home/jjd/Documents/llt/cgroup/analysis

python3 analyze.py \
  --db ~/cgroup-data/192.168.0.1/var/lib/cgroup-baseline/samples.db
```

### 輸出長什麼樣

分五段輸出，逐段看：

**(1) QUALITY CHECK** — 確認資料能用：

```
DB:           samples.db (32.4 MB)
rows:         234,567
cgroups:      47 distinct
timespan:     7.01 days (2026-05-13 14:30 → 2026-05-20 14:30)
samples:      10080 timer fires (median gap 60s)
  OK: no gaps > 120s
null coverage:
     memory_current             0.0%
     memory_peak                0.0%
     cpu_usage_usec             0.0%
     mem_psi_some_avg60         0.0%
```

**這段一定要先看**。判讀：

| 看到 | 意思 |
|---|---|
| `samples` ≈ 預期次數（一週應該 ~10000） | timer 穩定運作 |
| `gaps > 120s` 有警告 | 中間有時 collector 沒跑（server 重啟？資源被搶？）— `journalctl` 看 |
| 某個 column NULL% 高 | 那個欄位不能用，看下面 troubleshoot |

**(2) TOP 20 SERVICES BY MEMORY PEAK** — 看誰是大戶：

```
service                                       peak    avg     cores
---------------------------------------------------------------------
postgresql.service                        16384 MB  9821 MB    1.84
nginx.service                              6128 MB  4502 MB    0.62
redis.service                              2048 MB  1932 MB    0.15
...
```

這是純觀察，不下結論。可以拿這份跟運維 / 同事核對「對，這是預期的吃量」。

**(3) RECOMMENDED CGROUP RULES** — 自動算好 Day 9 候選規則：

```
service                       peak_mb cores  memory_max  memory_high  cpu_weight
postgresql.service               16384  1.84         22G          19G          100
nginx.service                     6128  0.62        8.0G         7.0G          100
redis.service                     2048  0.15       2624M        2368M           50
```

公式：`memory_max = peak × 1.3`, `memory_high = peak × 1.15`，`cpu_weight` 按 cores 分檔（>2.0 = 200，>0.5 = 100，>0.1 = 50，else 25）。

**這是建議值，不是定論**。你要對每個服務 sanity check：

- peak 是「整週最高峰」 — 如果剛好那一秒爆衝是 outlier？看 `--csv` 拿全資料畫 distribution
- 有 OOM kills 警告？表示 baseline 期間就被殺過，limit 設原本還更低
- 業務語義：這個 service 平時要不要 burst？要 burst 的話 `memory_max` 給寬一點

**(4) PSI CANDIDATES** — 下個 sprint 要看的：

```
service                          type   mem%   cpu%    io%      peak
postgresql.service                 io   0.32   0.15   2.40   16384MB
build-worker.service              cpu   0.05   5.21   0.10   1024MB
```

PSI `> 1.0%` 的服務（threshold 可以調 `--mem-psi`、`--cpu-psi`、`--io-psi`）。

`type` 欄表示卡哪：`io` = IO 壓力大、`cpu` = CPU 壓力大、`mem+io` = 兩個都卡。

**這份是給主管看的「下個 sprint 預計處理對象」清單**，不是這個 sprint 動的東西。

**(5) ANSIBLE host_vars YAML** — 直接複製貼上：

```yaml
cgroup_rules:
  postgresql.service:
    memory_max:  "22G"
    memory_high: "19G"
    cpu_weight:  100
  ...
```

## Step 4: 把結果填進報告

### §2.2 — baseline 數值結論

把 quality check + top services 表填進報告 §2.2，例如：

```markdown
### 2.2 Baseline 結果

收集週期：2026-05-13 ~ 2026-05-20（7.0 天）
監測 cgroup 數：47 個
無資料缺口（無 gap > 120s）

#### 觀察到的 top 5 by memory peak

| service | peak_mb | avg_mb | cpu_cores | mem_psi |
|---|---|---|---|---|
| postgresql.service | 16384 | 9821 | 1.84 | 0.32% |
| ... |

#### Baseline 期間發生的事件

- N 個 service 在 baseline 期間有 OOM kill 紀錄（見 quality check）
- IO PSI 偏高的服務：postgresql.service (2.40%)
```

### §5 / Q2 — service 候選

從 RECOMMENDED CGROUP RULES 那段挑 3-5 個準備 Day 9 canary，更新報告 §5 Q2 的「目標 service 範圍」。建議 **挑兩個極端**：

- 1 個「最不重要的」當 canary（出包不痛）
- 2-3 個「中等重要」做後續 rollout
- top 1 大戶（如 postgres）**留到第二輪**

### §2.1 PSI 候選清單

把 PSI CANDIDATES 段落貼進報告 §2.1，附說明：

```markdown
#### Baseline 期間的 PSI 候選

下列 service 在 baseline 期間 PSI some_avg60 > 1.0%，
表示有實質的資源等待。但目前 sprint 不對它們做 PSI 自動化，
僅列入下個 sprint review 清單：

| service | type | mem% | cpu% | io% |
|---|---|---|---|---|
| ... |
```

## Step 5: 把 host_vars 寫好（Day 9 準備）

```bash
cd /home/jjd/Documents/llt/cgroup/ansible
mkdir -p host_vars
```

新建 `host_vars/192.168.0.1.yml`（IP 換成你的）：

```yaml
---
# 從 analyze.py 第 (5) 段複製過來
# 只留 3-5 個你決定要 canary + rollout 的
cgroup_rules:
  log-collector.service:        # canary（不重要的）
    memory_max:  "2G"
    memory_high: "1.5G"
    cpu_weight:  50

  nginx.service:                # round 2
    memory_max:  "8G"
    memory_high: "7G"
    cpu_weight:  100

  # postgres 留 round 3，先觀察前兩個
```

寫完先別跑 — Day 9 才開始套。

## Troubleshoot

### `pandas not installed`
```bash
pip install --user pandas
```

### `cgroups: 0 distinct`
DB 是空的，或 collector pattern 沒抓到。回去看 deploy-verify.md Step 0.3。

### Quality check 顯示 `gaps > 120s` 一堆
表示 collector timer 中途有失敗。`journalctl -u cgroup-baseline.service` 看哪幾次 fail、為什麼。常見：server 重開、磁碟滿、systemd 重啟。

### `cpu_psi` 某些 cgroup 全 NULL
PSI 在某些舊版 kernel 上對特定 cgroup 不會產出。看 kernel version：< 4.20 整個 PSI 不存在；< 5.13 部分 cgroup 沒有。**升級 kernel 或 ignore**。

### Top service 的 peak 看起來不合理高
有兩種可能：
1. 真的有那麼一個 spike — `--csv` 出全表，看那個值是哪天哪個小時、是不是孤立 outlier
2. memory.peak 是「自 cgroup 建立以來的最高峰」，**service restart 會清零** — 如果整週沒重啟，這個值就是整週最高

判讀：如果 spike 只持續一兩秒就回落，可以選 p95 + buffer 而非 peak × 1.3 作為 memory_max。看 `--csv` 自己拍。

## 進階：自己 query

`analyze.py` 是預設視角。如果想自己挖：

```python
import sqlite3, pandas as pd
db = sqlite3.connect('~/cgroup-data/.../samples.db')
df = pd.read_sql("SELECT * FROM samples WHERE cgroup='system.slice/nginx.service'", db)

# 每小時 memory.current 平均
df['hour'] = pd.to_datetime(df['ts'], unit='s').dt.floor('h')
df.groupby('hour')['memory_current'].mean().plot()

# PSI 高峰
df.nlargest(20, 'mem_psi_some_avg60')[['ts', 'mem_psi_some_avg60']]
```

22 個欄位都在 `df.columns` 可看。pandas + matplotlib 想畫什麼都行。
