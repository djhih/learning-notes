# Cgroup Week 1 報告 — 口頭報告逐字稿

> 預計時間 15～18 分鐘。標 〔...〕的是給自己看的提示，不要唸出來。
>
> 跟 djhih.md 的對應：本稿 §2.1 用 L1/L2/L3 框架（取代 MD 裡 A/B/C 那張表）；其他章節跟 MD 結構一致。

---

## 開場（約 30 秒）

> 主管你好，今天想跟你 sync 一下 cgroup 這個任務 week 1 的進度。
>
> 整體分四塊：第一塊講我這週看過、實作過哪些東西；第二塊講目前還不確定、需要跟你對齊的地方；第三塊講 collector 怎麼寫的；第四塊講 Ansible 怎麼寫的。
>
> 最後會列出兩件需要你幫忙決定的事。大概 15 分鐘左右。

〔停頓，等 ack〕

---

## §1 已掌握的範圍（約 1.5 分鐘）

> 先講已經掌握的部分。
>
> cgroup v2 我把整個架構讀完了 — 包括 controllers、subtree_control 怎麼傳遞、層級委派這些概念。
>
> memory controller 的四個層級 max、high、low、min 我搞清楚它們各自的行為差別。簡單講 **max 是會殺人的硬上限，high 是會 throttle 但不殺的軟上限，low 跟 min 反過來、是「保護」這個 cgroup 不被搶走記憶體**。
>
> CPU 那邊是 weight 跟 quota 兩種哲學 — **weight 是相對的、CPU 緊張時才生效；quota 是絕對上限、閒著也不能超過**。一般 service 用 weight，batch job 用 quota。
>
> systemd 跟 cgroup 的對應關係我也讀清楚了 — 重點是 **systemd 是 cgroup 的擁有者**，所以我們的設定都必須走 systemd unit，不能直接戳 /sys/fs/cgroup，不然下次 daemon-reload 會被覆寫。
>
> PSI 等下單獨講，因為它跟不確定點有關。
>
> IO 那邊我讀過 io.weight 跟 io.max 怎麼用，但這輪先不動，理由稍後說。

〔停頓〕

> 實際做過的小實驗有兩個。
>
> 第一個是 **memtest cgroup** — 手動建一個 cgroup、設 memory.max 200MB，丟壓力測試進去，總共觸發 567 次 OOM kill。這個實驗讓我親眼看到限制機制有效，也學會怎麼讀 memory.events 那個計數檔。
>
> 第二個是 **baseline collector smoke test** — 我寫的 Python script 跑一次掃了 46 個 cgroup、22 個欄位，包括 PSI 全部解析正確。其中 user.slice/user-1000.slice 顯示 12GB，跟我用 htop 看到的一致，資料品質沒問題。

---

## §2 目前不確定的議題（最重要章節，約 3 分鐘）

> 接下來是這次 sync 最想跟你對齊的部分 — 兩個不確定點。

### §2.1 PSI 怎麼用

〔投影片或螢幕：把 PSI 的 some/full 範例輸出秀出來〕

> PSI 全名是 Pressure Stall Information，是 kernel 提供的訊號，告訴你**某個 cgroup 有多少百分比的時間「在等資源不夠」**。它跟 CPU utilization 不一樣 — CPU 用 100% 不等於有壓力，可能它需要的就是這麼多；PSI > 0 才是真的「有人在等」。
>
> PSI 有兩個 scope — `some` 表示「至少一個 task 在等」，`full` 表示「全部 task 都在等、整個 cgroup 卡住」。後者比較嚴重。

〔投影片：some 和 full 的概念圖〕

> 怎麼用我目前還在思考。我原本整理出三個子問題 — 看哪個訊號、threshold 設多少、觸發後做什麼 — 但後來覺得這分法太細，討論的時候會卡住。
>
> **我重新拆成兩個更基本的問題**。

〔停頓〕

> **第一個：蒐集 PSI 要做什麼？**
>
> 也就是「我們對 PSI 的期待是什麼？」分三個層級：

〔白板或投影片：列 L1 / L2 / L3〕

> - **L1 — 純監控**。資料收下來，事後想查可以查。投入接近 0，因為現在 collector 已經在做了。
> - **L2 — dashboard 或自動告警**。PSI 接到 monitoring 系統，超過 threshold 自動 page on-call。要有 monitoring infra。
> - **L3 — 自動行動**。例如 Meta 那套 oomd，PSI 超過某個值自動 kill 大耗記憶體的 process。這要先定義「誰可以被殺、什麼條件下可以」的政策。
>
> 我的建議是 **這個 sprint 先到 L1，下個 sprint 評估 L2，半年後再看 L3**。理由是每一層都要靠前一層的營運經驗才能設好 threshold。直接跳 L3 等於跳過所有試錯。

〔停頓〕

> **第二個：怎麼判斷 PSI 有問題？**
>
> 這題我覺得有個前置 — 「什麼算問題」。PSI **自己不會告訴你**「使用者有沒有覺得 lag、業務指標有沒有變差」，它只是內部訊號，說「資源在搶」。所以 PSI 高不等於有問題，要看我們 care 什麼。
>
> 技術上有兩種判斷方法：
>
> - **方法 a — 絕對門檻**。例如 `some_avg60 > 10`，或 `full_avg60 > 5` 就告警。優點是 day 1 就能用、不用 baseline；缺點是對天生 PSI 高的 service（像 DB）會誤警。
> - **方法 b — baseline 偏差**。每個 service 自己跟自己比，threshold 設在「平均加 3 倍標準差」之類的位置。要等 baseline 至少一週後才能用，但 per-service 精準很多。
>
> 我打算這樣銜接：**day 1 到 7 baseline 收集中先用方法 a 絕對門檻**，超過就 log；**day 7 之後切到方法 b**，per-service 自適應。

〔停頓〕

> 順便提一下背景 — **Meta 有一套叫 oomd / senpai 的工具**，就是基於 PSI 自動調整資源。但他們是先靜態 cgroup 規則跑了好幾年、累積營運經驗後才疊上去的。不是 PSI 那麼新潮的東西我們就要追，**我評估直接跳那套對我們太早**。

### §2.2 Cgroup 規則的「適合的」數值

> 這題其實跟 §2.1 是同根問題 — 兩個都卡在「沒有 baseline 數據」。
>
> 我不知道 nginx 該設 memory.max 多少、cpu.weight 多少，因為沒實測過它平常吃多少。
>
> 解法很直接 — **先收 baseline，再回填數值**。具體流程：
>
> 1. baseline 跑 5 到 7 天，拿到每個 service 的 `memory.peak`、CPU usage 分布
> 2. 第一版規則用 **`memory.max ≈ peak × 1.3`、`memory.high ≈ peak × 1.15`**
> 3. 在一台 canary 機器套上、觀察沒踩到實際用量上限，再 rollout 到其他機器

---

## §3 Collector — 怎麼寫的（約 4 分鐘）

> 接下來講 collector 的設計。分四個面向：總體架構、Python script 結構、SQLite schema、systemd unit。

### §3.1 為什麼是 oneshot + timer，不是常駐 daemon

〔白板或投影片：兩種寫法對比〕

> 第一個關鍵決定 — collector 不是常駐 daemon。是 **一個 Python script 跑一次就退出，靠 systemd timer 每 60 秒喚起一次**。
>
> 為什麼這樣設計：
>
> - **沒有 memory leak、FD leak 的風險** — 跑完就死，下次重來
> - **失敗自動復活** — 即使 script 掛掉、被 OOM、被升級覆寫，下一輪 timer 一樣會跑
> - **升級就是 cp 一個檔** — 下次 timer 觸發自動用新版，不用 reload daemon、不用熱替換
> - **沒有 sleep loop** — 不用自己管「等 60 秒、處理 signal、reconnect」這些
>
> 唯一 trade-off 是每次冷啟動有 import sqlite3 之類的成本，**實測 150ms**。每 60 秒一次完全可忽略。

### §3.2 collect.py 結構

〔投影片：流程圖 main → discover → sample_one → write_samples〕

> 整個 script 大約 180 行，**全部用 Python 標準函式庫、零第三方依賴**。部署不用 pip install、不用 venv。
>
> 分四塊：
>
> **第一塊：`main()`** — 入口函式，做四件事：
>
> 1. 確保 DB 目錄存在、開 SQLite 連線、跑 schema（CREATE IF NOT EXISTS）
> 2. 拿當前 timestamp
> 3. 呼叫 `discover_cgroups()` 列出要監測的 cgroup
> 4. 對每個 cgroup 跑 `sample_one()`，寫進 DB
>
> **第二塊：`discover_cgroups()`** — 用 glob 列出我們關心的 cgroup 路徑，包含 `system.slice/*.service`、`system.slice/*.scope`、`user.slice/user-*.slice`，排除 session-* 這種太瑣碎的。
>
> 它用 `yield` 寫成 **generator function** — 不一次 build 一個大 list，省記憶體。
>
> **第三塊：`sample_one(cg)`** — 對一個 cgroup 採樣所有 metric，組成一個 dict 回傳。
>
> 它呼叫三個 helper，因為 cgroup 檔案有三種格式：
>
> - `read_int()` 讀單一整數的檔案（像 `memory.current` 裡就一個數字）
> - `read_kv()` 讀 key-value 多行檔案（像 `memory.events`：`oom 573` 那種）
> - `read_pressure()` 讀 PSI 格式（`some avg10=0.05 avg60=0.10 total=...`）
>
> 三個 helper 都有完整 error handling — 檔案不存在、權限不對、內容格式異常都回 None，**上層不用煩**。
>
> **第四塊：`write_samples()`** — 用 `executemany` 一次寫一批 row 進 SQLite，比 for loop 跑 execute 快很多。SQL 用 `?` placeholder 防 injection、自動處理型別轉換。

### §3.3 SQLite schema 為什麼這樣設計

〔投影片：schema 結構〕

> Schema 是「**寬表**」設計 — 一列就是「某 cgroup 在某時間點的所有 metric」，22 個欄位橫向展開。
>
> **為什麼不用長表**（`ts, cgroup, metric_name, value` 那種）：
>
> - 寬表用 pandas 直接 `SELECT *` 就拿到 DataFrame，畫圖、算統計、跨 metric 比較都很順
> - 長表加 metric 不用改 schema、但查詢要做 PIVOT，分析慢
>
> 對我們「metric 集合固定」的情境，寬表勝出。如果之後支援動態 metric（plugin 機制），再轉長表。
>
> **Schema 嵌在 Python 字串裡**，不是獨立的 schema.sql。部署只推一個檔，避免 schema 跟讀寫程式碼脫節。
>
> Primary key 是 `(ts, cgroup)` — 同一秒同一個 cgroup 只一列。配合 `INSERT OR REPLACE` 達到「重跑也安全」。
>
> 開了 `PRAGMA journal_mode=WAL` — Write-Ahead Logging，**寫入時不會 lock 整個 DB**，pandas 在另一個 process 查資料時 collector 還能寫。

### §3.4 systemd unit + timer 設計

〔投影片：兩個檔案對應〕

> 兩個 systemd unit 檔案，主檔名一樣、副檔名不同 — `cgroup-baseline.service` 跟 `cgroup-baseline.timer`。**systemd 看到同名就會自動配對**。
>
> 為什麼拆兩個檔：**service 定義「跑什麼」，timer 定義「什麼時候跑」**。比 cron 好的地方：觸發失敗有 log、unit 可以查狀態、可以加 hardening。
>
> **Service 的關鍵設定**：
>
> - `Type=oneshot` — 跑完就結束，不是常駐 daemon
> - `User=root` — 要寫 `/var/lib/cgroup-baseline/`
> - 四行 hardening：`ProtectSystem=strict` 把 /usr、/etc 變唯讀；`ProtectHome=true` 看不到 home 目錄；`PrivateTmp=true` 獨立 /tmp；`NoNewPrivileges=true` 防權限升級
> - `ReadWritePaths=/var/lib/cgroup-baseline` — 在 strict 模式下開一個寫入例外
>
> 這個 hardening 即使 collector 被惡意控制，能造成的傷害也很有限。
>
> **Timer 的關鍵設定**：
>
> - `OnBootSec=30s` — 開機後 30 秒第一次觸發（給系統穩定時間）
> - `OnUnitActiveSec=60s` — 上次觸發後 60 秒再觸發
> - `AccuracySec=1s` — 觸發誤差控制在 1 秒（預設 1 分鐘，省電用，對我們太鬆）
> - `Persistent=false` — 漏掉的觸發不補跑（漏一次沒差）

### §3.5 本機驗證結果

〔可以秀 journalctl 輸出〕

> 我在我自己機器上完整跑過一輪 — 手動部署、跑兩輪、清理。確認：
>
> - timer 觸發間隔 59 秒（符合預期）
> - journal 收到 stderr log：「wrote 47 samples」
> - SQLite DB 累積資料、48 distinct cgroup、PSI 全部解析正確
> - rollback 完全乾淨，systemd 找不到這 unit
>
> Toolchain 跑得通，可以直接走 Ansible 推到 server。

---

## §4 Ansible — 怎麼寫的（約 4 分鐘）

> 接下來講 Ansible 怎麼設計。

### §4.1 整體結構：playbook + role 分層

〔投影片：repo 目錄樹〕

> Ansible 有兩個層級的組織：
>
> - **Playbook**（在 `playbooks/`）— 「做哪件事」的入口
> - **Role**（在 `roles/`）— 把可重複的工作打包
>
> 我寫了 **三個 playbook** 跟 **兩個 role**：
>
> | Playbook | 用哪個 role |
> |---|---|
> | `deploy-collector.yml` | `baseline_collector` |
> | `apply-cgroup-rules.yml` | `cgroup_slice` |
> | `rollback-cgroup-rules.yml` | 直接寫 task，不用 role |
>
> 這種分層的好處 — 之後想加新 playbook（例如「升級 collector 不動規則」、「reload 不 restart」），新增一個 playbook 檔就好，role 不用動。

### §4.2 兩個 role 各做什麼

> **`baseline_collector` role** 把 collect.py + 兩個 systemd unit 推到 target 機器，做的事跟我手動部署一樣，5 個 task：
>
> 1. 建 `/var/lib/cgroup-baseline/` 目錄
> 2. copy `collect.py` 到 `/usr/local/bin/cgroup-baseline-collect`
> 3. copy `.service` 到 `/etc/systemd/system/`
> 4. copy `.timer` 到 `/etc/systemd/system/`
> 5. `daemon-reload` + `enable --now timer`
>
> 重點是檔案來源用**相對路徑** `{{ playbook_dir }}/../../collector/` 抓 — 不複製到 role 自己的 `files/` 目錄。後面解釋為什麼。
>
> **`cgroup_slice` role** 套 cgroup 規則。它讀 `cgroup_rules` 變數（一個 dict），每個 entry 對應一個 service 的規則，產出 systemd drop-in 檔案到 `/etc/systemd/system/<svc>.d/50-cgroup.conf`。
>
> Template 用 `{% if X is defined %}` 包每一行 — 讓 host_vars 可以只設想設的屬性（nginx 只設 memory、postgres 多設 cpu_weight 跟 io_weight 都行）。

### §4.3 五個刻意的設計決策

〔白板或投影片：列五個 why〕

> 寫的時候做了五個比較刻意的選擇，講一下，這些是面試或 code review 會被問的點：

> **(1) `cgroup_rules: {}` 預設空字典**
>
> 即使有人不小心對全 host 跑 apply playbook，沒有 host_vars 的機器讀到空字典 → role 第一個 task 就 `meta: end_play` 結束 → no-op。
>
> 替代方案是「沒設值就 fail」。但 fail mode 比 no-op 危險很多 — 中途失敗可能 half-applied state，部分機器套了、部分沒套，更難收拾。

> **(2) 真實數值放 `host_vars/`、不是 `group_vars/`**
>
> 不同機器 service 不同、規格不同、負載不同。group_vars 假設「整群設一樣」，對 cgroup limit 來說錯太多次。
>
> 如果之後發現整群機器規格一致、跑的也是同樣 service（例如「所有 web tier」），再開一個 group 用 group_vars 統一管。**不預設要這樣做**。

> **(3) Apply playbook 不自動 restart service**
>
> 寫完 drop-in + daemon-reload 就停手，**不自動 `systemctl restart`**。最後印一條訊息列出該手動 restart 的 unit。
>
> 為什麼：自動 restart = 全 fleet 同時短暫斷線，業務上經常不可接受。違反「ansible 修改設定」跟「service 重啟」這兩個動作的分離原則。**配合 Day 9 canary 流程** — 先在一台機器手動 restart 觀察。

> **(4) Rollback 用 glob 找 `50-cgroup.conf`，不依賴 inventory state**
>
> Rollback playbook **不需要** ansible 知道哪些 service 有規則 — 直接 `find /etc/systemd/system/ -name 50-cgroup.conf` 全砍。
>
> 為什麼：出包要 rollback 時，「ansible state 跟 server 實際狀態一致」這件事可能本來就有疑問。**信「server 上實際的檔案」比信 ansible vars 安全**。即使 host_vars 已經改了或刪了，rollback 仍然有效。
>
> 關鍵：檔名 `50-cgroup.conf` 是我們的 marker，手動加的其他 drop-in（例如 `10-Environment.conf`）不會被砍。

> **(5) 用相對路徑引用 `collect.py`，不複製到 role 內**
>
> `roles/baseline_collector/tasks/main.yml` 裡用 `{{ playbook_dir }}/../../collector/collect.py` 抓檔案。
>
> 為什麼：**單一 source of truth**。collect.py 可以本機跑、也可以被 ansible 推上去 — **同一個檔案**。如果 role 裡 copy 一份，每次改 collect.py 都要記得同步兩個地方，遲早會出包。
>
> Trade-off：違反「role 應該是 self-contained」的慣例。但 repo 結構穩定（collector 跟 ansible 並排），相對路徑沒問題。**如果之後要把 role 發布成 collection 給別人用，再搬進去**。

### §4.4 三個 playbook 的對稱性

> deploy / apply / rollback 三個動作刻意設計成**對稱**：
>
> | 部署動作 | 反向動作 |
> |---|---|
> | `deploy-collector.yml` 建 collector | （手動 / 另寫 remove-collector） |
> | `apply-cgroup-rules.yml` 套規則 | `rollback-cgroup-rules.yml` 砍規則 |
>
> 每個動作都有對應的 reverse，**不會出現「部署得了卻清不掉」的尷尬狀態**。

### §4.5 操作流程

〔投影片：六步流程〕

> Ansible 操作六步：inventory 填好 → 連線檢查 → dry-run → 實跑 → 驗證 → 拉資料回 control 機。
>
> 細節我都寫進報告了，不細講。
>
> **唯一需要協調的事** — target server 上的 `ansible_user` 要有 sudo 權限。建議設 NOPASSWD，不然每跑一次 playbook 要打一次密碼。這部分要請你協助對接負責 server provisioning 的人。

---

## §5 接下來兩週計畫 + 需要你決定的事（約 1.5 分鐘）

> 接下來兩週的計畫：
>
> **Week 1（這週）**
> - Day 3～5：把 collector 透過 Ansible 推到 3～5 台代表性機器，**baseline 開始計時**
> - Day 6～7：weekend baseline 持續跑
>
> **Week 2（下週）**
> - Day 8：分析 baseline，產出第一版 cgroup 規則的數值表
> - Day 9：挑一台**低風險 canary** 套規則，盯一個工作天
> - Day 10～11：rollout 到 3～5 個目標 service
> - Day 12：從 baseline 整理 PSI 候選 service 清單，給初步 threshold 估算
> - Day 13～14：寫最終 report 跟你 sync

〔停頓〕

> **需要你幫忙決定兩件事**：

〔投影片或白板：列兩個 Q〕

> **Q1：PSI 使用層級要走到哪？**
>
> L1 純監控、L2 告警、L3 自動行動。我建議 L1 起手。如果你覺得 L2 也要做，我下個 sprint 排進去；L3 比較大、需要另外規劃。
>
> **Q2：deployment target server 選哪幾台？ansible_user sudo 權限怎麼處理？**
>
> 我需要 3～5 台代表性機器當 baseline target。哪幾台你決定，跟 server provisioning 那邊的對接你也要幫我牽一下。

〔停頓〕

> 有任何問題嗎？

---

## 預期會被問的問題（自己準備用，不講出來）

**Q: 為什麼用 SQLite 不用 Prometheus？**
> 2 週 sprint 範圍內，能查資料比漂亮 dashboard 重要。SQLite 零依賴、單檔、Python 標準庫就有。下個 sprint 規模長大再上 Prometheus，現在做太早。

**Q: 為什麼第一輪不做 IO 限制？**
> IO 限制要看 IO scheduler（要 BFQ）、device 類型、IO 路徑（buffered vs direct）。第一輪先看 `io.pressure` 確認有沒有問題，再決定要不要動 io.weight / io.max。

**Q: 為什麼不直接跑 Meta 的 oomd？**
> oomd 假設你已經有靜態 cgroup 規則跑過、知道哪些 service 在什麼條件下該被殺、業務也接受被殺。我們現在從 0 起步，跳過會踩很多坑。

**Q: 你怎麼確定 collector 不會影響 production performance？**
> oneshot 跑一次大約 150ms，每 60 秒一次，CPU 佔用大概 0.25%。讀 cgroup metric 是 world-readable 的虛擬檔案（kernel 直接生成），不會 lock 也不會影響任何 service。

**Q: cgroup v2 / 不是 v1，相容性會不會有問題？**
> 我們的 target server 跑的都是 modern Linux（kernel 5+），預設就是 v2。`stat -fc %T /sys/fs/cgroup` 會回 `cgroup2fs` 就確認了。如果有 v1 機器另外處理。

**Q: rollback 機制是什麼？Canary 出包怎麼辦？**
> 有 `rollback-cgroup-rules.yml` playbook，會找所有 `50-cgroup.conf` 的 drop-in 檔砍掉、daemon-reload。Canary 出包 → 立刻跑 rollback 那台、分析原因再決定規則要不要放寬。Day 9 是專門設計成「卡住不要硬上 Day 10」。

**Q: 如果 baseline 收不到 5 天就要決策呢？**
> 退路是用 3 天 + 過去監控數據（如果有）混算，並在 report 註明這個 caveat。最壞情況也比「沒有任何資料就猜」好。

**Q: GPU 怎麼納入這套？**
> cgroup v2 控不到 GPU，已查證。GPU 隔離要用 NVIDIA MIG、time-slicing 或 MPS，是完全另一個主題，這個 sprint 不會碰。

---

## 講者自己的 cue card（緊張時看）

1. **節奏**：每講完一個 §X 停頓兩秒、看主管表情再繼續
2. **避免**：「我覺得我可能...」「不確定...」「應該...」這種猶豫詞 — 改成「目前看起來是 X，等 baseline 確認」
3. **碰到不知道的問題**：「這題我沒查過，會後 follow up」比硬掰好
4. **被質疑時間規劃**：先承認「2 週確實 tight」，再說「所以我刻意砍掉了 oomd / 全 fleet rollout / Prometheus 三件事」 — 主動展示 trade-off 比辯護強
