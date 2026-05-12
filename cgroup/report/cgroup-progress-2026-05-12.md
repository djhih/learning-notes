# cgroup v2 階段性進度報告

**作者：** jhih
**日期：** 2026-05-12
**狀態：** 第一週 Day 1 — Sprint 起點，toolkit 已就緒、baseline 尚未開始收

---

## TL;DR

- **已掌握 cgroup v2 的概念與實作機制**（架構、systemd 對應、PSI 語意），並有實機驗證紀錄（memtest cgroup 觸發 OOM kill 567 次，限制機制確認有效）。
- **完成不依賴 baseline 數字的 toolkit**：Python baseline collector（已通過本機 smoke test）+ Ansible playbook（部署 / 套規則 / rollback）+ 三份操作文件。
- **接下來 2 週可推進到 3–5 個 service 的 production rollout**，Day 14 提交完整 report。
- **PSI 自動化（Meta oomd 那套）刻意不在此 sprint 部署**，理由與替代方案在 §2.1，最終決策需要對齊。
- **目前需要主管裁決的議題**列在 §5，主要是 PSI 觸發行為的政策決定。

---

## 1. 目前掌握的範圍

### 1.1 概念面

| 主題 | 狀態 |
|---|---|
| cgroup v2 架構（controllers、subtree_control、層級委派） | 已掌握 |
| memory controller 的四個層級（max / high / low / min） | 已掌握 |
| CPU controller（weight vs quota 的選擇） | 已掌握 |
| systemd Slice / Scope / Service 與 cgroup 樹的對應 | 已掌握 |
| PSI 的語意（some / full × avg10 / avg60 / avg300） | 已掌握語意，**應用方式待對齊**（見 §2.1） |
| IO controller（io.weight、io.max） | 已讀過，未實作 |
| cpuset / NUMA pinning | 已讀過，本 sprint 不處理 |
| GPU 的 cgroup 控制 | 已查證：**cgroup v2 控不到**，需用其他機制（NVIDIA MIG / MPS） |

### 1.2 實作面

- **memtest cgroup 實驗**：在 `/sys/fs/cgroup/memtest/` 設 `memory.max=200MB`，跑壓力測試 → 觸發 567 次 OOM kill，驗證限制機制有效、能讀懂 `memory.events` 計數。
- **Baseline collector smoke test**：本機跑一次採樣 46 個 cgroup，22 個 metric 欄位（memory / CPU / PSI）全部解析正確，user.slice/user-1000.slice 顯示 12GB 與系統實況吻合。

### 1.3 已產出的內部文件

放在 [`lecture/`](../lecture/)：

| 文件 | 主要回答 |
|---|---|
| `cgroup-learning-roadmap.zh-tw.html` | 2 週要做哪些事、什麼順序 |
| `cgroup-systemd-mapping.zh-tw.html` | systemd 屬性 ↔ cgroup 檔案對應，設計規則時的查表 reference |
| `cgroup-toolkit-quickstart.zh-tw.html` | 怎麼跑 playbook、Canary 流程、rollback 指令 |
| `cgroup-toolkit-walkthrough.zh-tw.html` | 為什麼這樣寫、考慮過的替代方案 |

---

## 2. 目前不確定 / 需對齊的議題

### 2.1 PSI 的應用方式（最重要的開放議題）

PSI 本身只是三個檔案、六個數字（cpu / memory / io × some / full）。**不確定的不是「PSI 是什麼」，是「PSI 要怎麼用」**。拆成三題：

| 子問題 | 性質 | 怎麼解 |
|---|---|---|
| (A) 看哪個訊號（`some` 還是 `full`、`avg10` 還是 `avg60`） | 純技術 | 我來決定，建議 `some_avg60` 作為早期警示 |
| (B) Threshold 設多少 | 數據驅動 | baseline 收一週後才有依據；提前決定 = 猜 |
| (C) 觸發後做什麼 | **政策決定** | **需要主管裁決**（見 §5） |

#### 建議：本 sprint 不部署 PSI 自動化

Meta 那套（oomd / senpai）對我們目前規模太早。Meta 自己也是先有靜態 cgroup 規則跑了好幾年才疊上自動化。直接從 PSI 自動化開始，等於跳過所有營運經驗。

**替代計畫**：
1. 此 sprint 用 collector 把 PSI 數據收下來
2. Day 12 整理出 **「PSI 候選 service 清單 + 初步 threshold 估算」**
3. 下個 sprint 才決定要不要做、做到哪一層（純監控 / 告警 / 自動行動）

### 2.2 Cgroup 規則的「硬規定」數值怎麼設

這是 Ansible 路線的核心阻力 — 我不知道 `memory.max` 該設多少、`cpu.weight` 該設幾。

**根因**：沒有 baseline 數據，所以是猜的。

**解法**：本 sprint 的核心動作就是先收 baseline、再回填數值。流程：
1. baseline 跑 5–7 天 → 拿到每個 service 的 `memory.peak`、CPU usage 分布
2. 第一版規則：`memory.max ≈ peak × 1.3`、`memory.high ≈ peak × 1.15`
3. Canary 觀察、確認沒踩到實際用量上限後 rollout

---

## 3. 已交付的 toolkit

### 3.1 結構

```
cgroup/
├── collector/                          # 不依賴 Ansible，可單機跑
│   ├── collect.py                      # 主程式（嵌入 SQLite schema）
│   ├── cgroup-baseline.service         # systemd oneshot
│   └── cgroup-baseline.timer           # 每 60 秒觸發
├── ansible/
│   ├── ansible.cfg
│   ├── inventory.example
│   ├── group_vars/all.yml              # cgroup_rules 預設 {}
│   ├── playbooks/
│   │   ├── deploy-collector.yml
│   │   ├── apply-cgroup-rules.yml
│   │   └── rollback-cgroup-rules.yml
│   └── roles/
│       ├── baseline_collector/         # 部署 collect.py + timer
│       └── cgroup_slice/               # 寫 systemd drop-in
└── report/
    └── cgroup-progress-2026-05-12.md   # 本文件
```

### 3.2 設計決策摘要

| 決定 | 為什麼 |
|---|---|
| **SQLite，不接 Prometheus** | 2 週 sprint 範圍內「能查資料」比「漂亮 dashboard」重要；下個 sprint 再上 Prometheus |
| **systemd timer + oneshot，不是常駐 daemon** | script 可被殺、可升級、無 memory leak 風險 |
| **schema 嵌在 Python 字串裡** | 部署只推一檔；避免外部 schema.sql 跟程式碼脫節 |
| **走 systemd drop-in，不直接寫 `/sys/fs/cgroup/`** | systemd 會在 daemon-reload / restart 時覆寫直寫的值 |
| **`cgroup_rules: {}` 預設空字典** | 誤跑保護 — 沒設 host_vars 的機器讀到空字典 = no-op |
| **真實數值放 `host_vars/` 不是 `group_vars/`** | 不同機器規格不同，group-level 假設「整群設一樣」會錯太多次 |
| **不自動 restart service** | 自動 restart = 全 fleet 同時短暫斷線；改成印警告列出該手動 restart 的 unit |
| **Rollback 用 `find` 找 `50-cgroup.conf` marker** | 即使 ansible state 跟 server 脫節也能 rollback |

完整理由（含「替代方案是什麼、為什麼沒選」）見 `lecture/cgroup-toolkit-walkthrough.zh-tw.html`。

---

## 4. 兩週推進計畫

**目標時程：** 2026-05-12（一）～ 2026-05-27（二），全職主力。

### Week 1：collector live + playbook 骨架

| Day | 主軸 | 平行 |
|---|---|---|
| Day 1（05-12 一） | 寫 collector v0，本機跑通 | 列 target services、讀 systemd mapping |
| Day 2（05-13 二） | collector v1：加 PSI、memory.events、cpu.stat | 設計 slice 切法 |
| Day 3（05-14 三） | Ansible 推 collector 到 3–5 台機器，**baseline 開始計時** | 寫 playbook 骨架（placeholder 數值） |
| Day 4（05-15 四） | 確認 collector 穩定運作 | playbook 加 dry-run / rollback / check-mode |
| Day 5（05-16 五） | 在 sacrificial 機器測 playbook（假數值） | 準備 baseline 視覺化（pandas + matplotlib） |

> 週末讓 baseline 繼續跑、自己休息。

### Week 2：分析 + rollout + report

| Day | 主軸 |
|---|---|
| Day 8（05-19 一） | 分析 baseline（已收 5 天）→ 產出第一版規則數值表 |
| Day 9（05-20 二） | 選 **1 個低風險 service** 作為 canary，套規則、盯 1 個工作天 |
| Day 10（05-21 三） | Canary OK → rollout 到第 2、3 個 service |
| Day 11（05-22 四） | 完成所有目標 service 的 rollout，收上線前後對照數據 |
| Day 12（05-23 五） | 從 baseline 找出 PSI 候選 service，寫初步 threshold 估算 |
| Day 13（05-26 一） | 寫最終 report（含 baseline 圖、規則設計依據、PSI 候選清單） |
| Day 14（05-27 二） | 與主管對齊下個 sprint 的方向 |

### Day 14 預計交付

1. baseline collector 在 3–5 台機器跑了 7+ 天，數據可查
2. 3–5 個 service 已透過 Ansible 套上靜態 cgroup 規則，且數值有 baseline 依據
3. 最終 report 含：baseline 圖、規則設計依據、上線前後對照、地雷紀錄、PSI 候選清單

---

## 5. 需要主管裁決的議題

### Q1：PSI 自動化的政策層級

PSI 偵測到「資源緊張」之後該做什麼？三選一：

| 選項 | 描述 | 建議使用情境 |
|---|---|---|
| **A. 純監控** | 只記錄 PSI 數值，不自動行動 | 風險最低；適合先做這個 |
| **B. 告警** | PSI 超過 threshold 時 page on-call | 中等風險；需要 alert routing |
| **C. 自動降級 / kill** | 像 Meta oomd 主動殺 process | 風險最高；需要明確的「誰可被殺」政策 |

**我建議先做 A**，跑半年後評估是否要疊 B。C 是更久之後的事。

### Q2：目標 service 範圍

第一輪 rollout 的 3–5 個 service，建議怎麼挑？兩種思路：

- **挑「最吃資源的」**：baseline 看到 memory peak 最高的前 3 名 → 限額帶來的價值最大
- **挑「最不重要的」**：log 收集、cron job → 出包代價最小

**我建議優先「最不重要的」起手**（風險最低、Canary 出包不痛），第二輪再處理「最吃資源的」。

### Q3：rollout 風險容忍度

Day 9 Canary 出包（例如 service 被 OOM）時，我是否有權自行 rollback？還是需要先通報？這影響反應速度。

---

## 6. 明確不在此 sprint 範圍

避免被誤解為「2 週完工」，以下項目**刻意**不在此 sprint：

- **全 fleet rollout** — 跑通方法後下個 sprint 才放量
- **oomd / senpai 自動化部署** — 沒有靜態規則跑過之前，threshold 是猜的
- **io.weight / io.max 細節調校** — 除非 baseline 顯示 io.pressure 明顯，否則先放著
- **cpuset / NUMA pinning** — 另一個主題，跟限額無關
- **GPU 資源控制** — cgroup v2 本來就控不到，已查證
- **Prometheus / Grafana 接線** — SQLite + pandas 此 sprint 夠用
- **cgroup v1 相容** — 假設所有目標機器都是 v2（modern 主流預設）

---

## 7. 風險與 fallback

| 風險 | Fallback |
|---|---|
| baseline 收不到 5 天就要決策 | 用 3 天 + 過去監控數據（如果有）混算，並在 report 標註 caveat |
| Canary 服務套上規則後 OOM | 立刻用 rollback playbook；該台先停，分析後再決定規則是否放寬 |
| Ansible 環境不熟導致進度卡住 | 用最笨的 file template + handlers，不追求漂亮的 role 抽象 |
| baseline 資料分析卡住 | 不追求視覺化漂亮，pandas `describe()` + 幾張 line plot 已足夠 |

---

## 8. 附錄：相關內部文件

- 學習路線：[`lecture/cgroup-learning-roadmap.zh-tw.html`](../lecture/cgroup-learning-roadmap.zh-tw.html)
- systemd ↔ cgroup 對應 reference：[`lecture/cgroup-systemd-mapping.zh-tw.html`](../lecture/cgroup-systemd-mapping.zh-tw.html)
- Toolkit 操作手冊：[`lecture/cgroup-toolkit-quickstart.zh-tw.html`](../lecture/cgroup-toolkit-quickstart.zh-tw.html)
- Toolkit 設計攻略：[`lecture/cgroup-toolkit-walkthrough.zh-tw.html`](../lecture/cgroup-toolkit-walkthrough.zh-tw.html)
- 既有概念筆記：`lecture/cgroup-v2.zh-tw.html`、`cgroup-setup-pitfalls.zh-tw.html`、`meta-psi-automation.zh-tw.html`、`gpu-monitor.html`

---

*下次更新預計：2026-05-19（Day 8，baseline 分析完成後）*
