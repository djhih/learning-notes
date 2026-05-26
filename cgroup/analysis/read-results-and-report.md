# 怎麼讀 analyze.py 輸出 + 怎麼整理成報告

> 跑完 `python3 analyze.py --db samples.db` 之後讀這份。
> 教學重點：**每段數字告訴你什麼、紅綠旗、怎麼變成主管能看懂的文字**。

---

## 1. 五段輸出對應到報告哪一節

```
analyze.py 段落              →  報告（cgroup-progress-2026-05-12.md）位置

(1) QUALITY CHECK           →  §1.2 實作面（更新成「baseline 收 7 天」那句）
                                §7 風險（若品質有 caveat）

(2) TOP N SERVICES          →  §5 baseline 結果（新增章節）或 §1.4

(3) RECOMMENDED RULES       →  §5 Q2 目標 service 範圍（從 baseline 挑出來）

(4) PSI CANDIDATES          →  §2.1 PSI 候選清單（下個 sprint 處理）

(5) host_vars READY YAML    →  不放報告。複製到 ansible/host_vars/<host>.yml
```

---

## 2. 逐段判讀

### (1) QUALITY CHECK — 先看這段，資料壞下面都別信

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

#### 紅綠旗

| 指標 | 綠旗 | 黃旗 | 紅旗 |
|---|---|---|---|
| `rows` | > 50 × cgroups × hours | < 預期 50% | 0 或 < 1000 |
| `timespan` | ≥ 5 天 | 3-5 天 | < 3 天（baseline 不夠） |
| `samples` ≈ timespan × 1440（min） | 差 < 5% | 5-15% 缺 | > 15% 缺 |
| `gap > 120s` | 0 | 1-3 個 | > 3 個 → collector 中斷過 |
| NULL % | 全部 < 5% | 5-30% | > 30% → kernel / cgroup 設定有問題 |

#### 怎麼寫進報告

**綠旗版**（一行話帶過）：

```markdown
**Baseline 收集**：2026-05-13 ~ 2026-05-20（7 天），共 234,567 筆觀測、
覆蓋 47 個 cgroup，無資料缺口。
```

**有黃旗版**（要明確標 caveat）：

```markdown
**Baseline 收集**：2026-05-13 ~ 2026-05-19（6 天），共 198,432 筆觀測。
**Caveat**：05-15 14:00 collector 異常停止 30 分鐘（journalctl 顯示 OOM），
影響 1 個 sample 點；其他時段穩定。整體資料可用，僅 mem_psi 在
某 5% 觀測中為 NULL（kernel 4.x 已知行為）。
```

**透明的 caveat 比假裝沒事好** — 主管會看出來。

---

### (2) TOP N SERVICES BY MEMORY PEAK — 看誰大

```
service                                       peak    avg     cores
---------------------------------------------------------------------
postgresql.service                        16384 MB  9821 MB    1.84
nginx.service                              6128 MB  4502 MB    0.62
redis.service                              2048 MB  1932 MB    0.15
mysql-aux.service                          1500 MB   980 MB    0.43
...
```

#### 三個欄位的意義

- `peak` = 整週的 `memory.peak` 最高值（cgroup 累積監測值，service 重啟會清零）
- `avg` = `memory.current` 平均
- `cores` = 平均用了幾顆 CPU（從 `cpu.stat` 算出）

#### 紅綠旗

| 觀察 | 解讀 |
|---|---|
| `avg` 跟 `peak` 接近 | **穩定**服務（DB、cache）— memory_max 設窄一點 OK |
| `avg << peak`（差 5x 以上） | **bursty**服務（web、batch）— memory_max 要寬，避免 peak 時被 OOM |
| `cores > 服務本應吃的` | 異常（loop、busy wait、CPU 漏水）— 值得獨立調查 |
| `cores ≈ 0` 但 `avg` 大 | service 閒置但記憶體沒釋放（cache 不算 bug） |

#### 怎麼寫進報告

不要直接貼 20 行 raw 表。**挑「主管會 care 的前 5-10 個」+ 加一句解讀**：

```markdown
### 5.1 觀察到的 top 5 services

| service | peak | avg | cpu cores | 特性 |
|---|---|---|---|---|
| postgresql | 16 GB | 9.8 GB | 1.84 | 穩定吃量，avg 接近 peak |
| nginx | 6 GB | 4.5 GB | 0.62 | 有 burst（worker 動態調整） |
| redis | 2 GB | 1.9 GB | 0.15 | 穩定，CPU 很低 |
| mysql-aux | 1.5 GB | 980 MB | 0.43 | mid burst |
| log-collector | 500 MB | 320 MB | 0.05 | 低負載 |

**觀察**：總記憶體 peak 26 GB（單台 server 64 GB），有充足空間設限額。
PostgreSQL 是最吃量的服務、且穩定 — 可作為「規則最有意義」的對象。
```

兩個重點：
1. **加一個「特性」欄**（穩定 / bursty / 異常） — 數字之外的判斷
2. **末尾一句綜合觀察** — 不是 list 完就走

---

### (3) RECOMMENDED CGROUP RULES — 自動算的建議值

```
service                       peak_mb cores  memory_max  memory_high  cpu_weight
postgresql.service               16384  1.84         22G          19G          100
nginx.service                     6128  0.62        8.0G         7.0G          100
redis.service                     2048  0.15       2624M        2368M           50
```

`memory_max = peak × 1.3`、`memory_high = peak × 1.15`、`cpu_weight` 按 cores 分檔。

#### 不要全盤照單收 — 三個 sanity check

**Check 1：peak 是不是孤立 outlier？**

如果某 service 整週只有「某 5 分鐘」衝到 peak，剩下都很穩 → 用 `peak × 1.3` 太浪費。改用 `p95 × 1.5` 比較合理。

確認方法：

```python
import sqlite3, pandas as pd
db = sqlite3.connect('samples.db')
df = pd.read_sql("SELECT * FROM samples WHERE cgroup='system.slice/postgresql.service'", db)
df['memory_mb'] = df['memory_current'] / 1024 / 1024
print(df['memory_mb'].describe())
print(df['memory_mb'].quantile([0.5, 0.9, 0.95, 0.99]))
```

如果 `p99 << max` → 是 outlier，用 p95 + buffer。

**Check 2：baseline 期間有沒有踩到 OOM？**

analyze.py 會印警告：

```
WARN: 2 service(s) had OOM kills in baseline period:
  - postgresql.service (3 kills) — limit too low?
```

如果有 — 意思是 baseline 期間就被 systemd 預設限額（或這 service 自己的設定）殺過。**這 service 的 peak 可能是「被殺前的最高值」、不是「自然 peak」**。要看 journalctl 評估真實需求，不能用 `peak × 1.3` 因為這個 peak 是人造的。

**Check 3：業務 semantic — 這 service 需要 burst 嗎？**

- Web service / batch job 高峰會超 baseline → 要更寬的 `memory_max`
- 系統 daemon（sshd、cron） → 設窄一點 OK，反正 baseline 已是常態

#### 怎麼寫進報告

**直接表，但加「依據」一欄**：

```markdown
### 5.2 第一輪建議規則

依據 baseline 觀測，採用 `memory_max ≈ peak × 1.3`、`memory_high ≈ peak × 1.15`。

| service | memory_max | memory_high | cpu_weight | 依據 |
|---|---|---|---|---|
| postgresql.service | 22G | 19G | 100 | peak 16G × 1.3，**穩定服務、buffer 30% 夠** |
| nginx.service | 8G | 7G | 100 | peak 6G × 1.3，**bursty 服務，buffer 30% 仍可能緊** |
| redis.service | 2.5G | 2.3G | 50 | peak 2G × 1.3，**極穩定** |
| mysql-aux.service | 2G | 1.7G | 50 | peak 1.5G × 1.3 |
| log-collector.service | 700M | 600M | 50 | peak 500M × 1.3，**低風險 canary 候選** |

**Caveat**：postgresql baseline 期間有 3 次 OOM kill，現有限額已踩到。
若採用上表規則仍有風險，建議先在 staging 跑 3 天驗證。
```

**有 caveat 要顯眼寫出來**。

---

### (4) PSI CANDIDATES — 下個 sprint 的清單

```
service                          type   mem%   cpu%    io%      peak
postgresql.service                 io   0.32   0.15   2.40   16384MB
build-worker.service              cpu   0.05   5.21   0.10   1024MB
```

#### 意義

PSI `some_avg60 > 1.0%` 的 service。`type` 告訴你卡在哪裡：

- `io` = IO PSI 高 → 磁碟瓶頸（DB、log heavy）
- `cpu` = CPU PSI 高 → 計算密集，可能 throttle
- `mem` = memory PSI 高 → 記憶體緊（reclaim 多）
- `mem+io` 或多重組合 → 多重瓶頸

#### 紅綠旗

| PSI 範圍 | 解讀 |
|---|---|
| < 1% | 健康，無 pressure |
| 1-5% | 有感、值得注意 |
| 5-10% | 顯著、應該處理 |
| > 10% | 嚴重 — 已經在影響 latency / throughput |

#### 怎麼寫進報告

進 §2.1 PSI 章節，當「下個 sprint 候選清單」：

```markdown
### 2.1 PSI 候選清單（下個 sprint review）

下列 service 在 baseline 期間 PSI some_avg60 > 1.0%，
表示有實質的資源等待。**本 sprint 不對它們做 PSI 自動化**，
僅列入下個 sprint review：

| service | 卡在 | mem PSI | cpu PSI | io PSI | 建議 |
|---|---|---|---|---|---|
| postgresql.service | IO | 0.32% | 0.15% | **2.40%** | 評估 io.weight / 換 SSD |
| build-worker.service | CPU | 0.05% | **5.21%** | 0.10% | 評估 cpu.weight 提升 |

PostgreSQL 的 IO PSI 2.4% 顯著、跟它「資料庫 + 高 IO」性質吻合，
建議下個 sprint 優先 review。
```

---

### (5) host_vars YAML — 直接複製

不放進報告（操作細節，主管不需要看）。複製到：

```bash
ansible/host_vars/192.168.0.1.yml
```

跟你的 inventory 主機名對應。**只留你要 Day 9 canary + round 1 rollout 的 3-5 個**，其他註解掉留下輪。

---

## 3. 完整 worked example：報告新增章節怎麼寫

把上面四段組合，**新增一個 §5 章節**到主報告：

```markdown
---

## 5. Baseline 結果（2026-05-19 更新）

### 5.1 收集情況

Baseline 收集週期：2026-05-13 ~ 2026-05-20（7 天）
監測 cgroup 數：47 個（system.slice services + user-1001.slice）
總樣本數：234,567，無資料缺口
DB 大小：32 MB

### 5.2 觀察到的 top 5 services

[第 (2) 段那個表 + 觀察句]

### 5.3 第一輪建議規則

[第 (3) 段那個表 + 依據欄 + caveats]

### 5.4 PSI 候選清單（給下個 sprint）

[第 (4) 段那個表 + 卡點 + 建議]

### 5.5 Day 9 canary 計畫

依據以上分析，挑 **log-collector.service** 作為 Day 9 canary：

- 用量低（500 MB peak）→ 規則邊界容易設準
- 業務不關鍵 → 套錯出包代價低
- 無 burst 跡象 → 規則生效後行為可預測

下午 14:00 套規則，盯到 18:00 觀察 memory.events、CPU throttle、PSI 三個面向。
無異常 → Day 10 接 redis + mysql-aux。
```

這個 §5 就是 Day 14 主管報告的核心。

---

## 4. 三個容易忽略的 reporting 原則

### 4.1 數字之外要有「解讀」

```markdown
[爛] postgresql peak 16384 MB
[好] postgresql peak 16 GB（avg 9.8 GB），avg/peak 比 0.6 → 穩定吃量
```

數字旁邊加「特性」「趨勢」「對比」，主管不用自己算。

### 4.2 主動標 caveat 比掩蓋好

如果資料有問題（gap、NULL、outlier、OOM 痕跡），**自己先講出來**：

```markdown
**Caveat**：postgresql 在 05-15 觸發 3 次 OOM kill（journal 顯示 limit
未調 = 預設 cgroup 限制不足）。本次規則建議基於 peak 重算，
但仍建議先 staging 驗證。
```

主管會「相信會看資料的人」。

### 4.3 不要把所有 service 都塞進報告

47 個 cgroup 不要全列。挑 top 10 + 你想處理的 3-5 個。**主管 5 分鐘讀完報告**，列太多會失焦。完整資料放附錄 / CSV、報告本文只放 actionable 的。

---

## 5. 工具鏈速查

| 我想做的事 | 跑這個 |
|---|---|
| 拿 baseline 數字 | `python3 analyze.py --db samples.db` |
| 拿圖（嵌進報告） | `python3 visualize.py --db samples.db --out plots/` |
| 自己挖某 service 細節 | 開 jupyter / python REPL，`pd.read_sql("SELECT * FROM samples WHERE cgroup='...'", db)` |
| 全 raw CSV 給同事 | `python3 analyze.py --db samples.db --csv summary.csv` |
| 產 host_vars 規則 | 從 analyze.py 第 (5) 段複製 |

---

## TL;DR — 流程

1. **跑 analyze.py** → 拿 5 段輸出
2. **看 QUALITY CHECK** → 沒紅旗才繼續
3. **挑 5-10 個 services**（不要全列）→ 加「特性」欄
4. **sanity check 規則建議**（outlier、OOM、burst）→ 該調整就調
5. **寫進報告 §5**（新章節）+ host_vars YAML（操作檔）
6. **PSI 候選** → 寫進 §2.1，標明「下 sprint review」
7. **Day 9 canary**：從規則表挑「低風險、可預測」的一個起手