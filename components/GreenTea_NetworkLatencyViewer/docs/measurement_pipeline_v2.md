# 計測パイプライン v2 設計 — 収集 / 記録 / 計測 の 3 分割

> 現 `gtnlv-rpid` (monolithic) を **収集部 (collector) / 記録部 (recorder) / 計測部 (analyzer)** に再設計する。`robot_comm_spec` v2.0.0-dev で確定した **24bit cycle_count** (`a4eeadf`) を記録キーの主軸とする。

## 1. 背景と目的

### 1.1 現 `gtnlv-rpid` の構造的課題

| 課題 | 詳細 |
|---|---|
| CSV が 3 役兼任 | record + live tail + WebUI 読みを 1 CSV で。live tail が GB 級 file で重い、rotation 後付けで SnifferReader hang バグ |
| PPS bridge が post-run のみ | bridge_offset は shutdown 時 join。live OWD は raw approx しか出ない |
| leg 分解がオフライン | 4 leg は sniffer_bridge.py 別実行。live で wire/air leg 出ない |
| 二重受信未対応 | eth1 + wlan0 で broadcast 2x カウント |
| robustness 低 | reader thread が hang しても検出されない (実際 live で発生) |

### 1.2 cycle_count 導入による解決

`robot_comm_spec` で **AI が全ロボットへ同時送出する周期 = cycle** を定義し、downlink offset 51-53 に **24bit cycle_count** を付与 (sender ごと +1、0–16,777,215、100 Hz でも ~1.9 日 wrap しない)。

→ **`(cycle_count, robot_id)` を記録キー**にでき、wrap 拡張 (epoch) 不要。各計測点を同一キーで結合できる。

### 1.3 設計目標

1. **収集 / 記録 / 計測 の責務分離** — 計測ロジック変更が収集部に波及しない
2. **生値記録** — `(cycle_count, robot_id)` キーで各計測点の生値を保存。後から別解析できる
3. **計測の最小依存** — OWD 計算は **cycle_count (キー) + TSF→PPS bridge (時刻)** のみ使用。`corr_unix_time` / `t_rx_esp_timer_us` は記録するが計測ロジックは読まない

## 2. アーキテクチャ概観

```
┌─ 収集部 (collector) ── 各 source thread → 生 event ─────────────────┐
│                                                                      │
│ ■ 下り DL (AIPC → robot、キー = cycle_count + robot_id)             │
│   ① wire SPAN (wire_capture)  AIPC→AP 有線 frame を mirror で sniff │
│        → {cycle_count, robot_id, t_tx_unix(payload 38-45),          │
│           t_wire_phc(PHC hwtstamp)}                                 │
│   ② sniffer air (UART)        AP→HID の air frame (AP TX)           │
│        → {cycle_count, robot_id, t_air_tsf, t_air_recv}             │
│   ③ HID rx_dl (52000+id)      HID が受信した瞬間                    │
│        → {cycle_count, robot_id, t_rx_tsf, t_rx_esp, corr_unix}     │
│                                                                      │
│ ■ 上り UL (robot → AIPC、キー = robot_id + ul_seq)                  │
│   ④ HID tx_ul (52000+id)      HID が送信する直前                    │
│        → {robot_id, ul_seq, t_tx_tsf}                               │
│   ⑤ sniffer air (UART)        HID→AP の air frame (broadcast)       │
│        → {robot_id, t_air_tsf, t_air_recv}                          │
│   ⑥ uplink (50000+id)         RasPi/AIPC が受信                     │
│        → {robot_id, ul_seq, t_recv}                                 │
│                                                                      │
│ ■ 時刻同期 (cycle 非依存、timesync)                                │
│   ⑦ PPS gpio (/dev/pps0)      → {unix_assert(PHC 割込)}            │
│   ⑧ sniffer PPS marker (UART) → {t_pps_tsf, t_recv}                │
│   ⑨ HID hb (52000+id)         → {cal 残差, dropped, ...}           │
│                                                                      │
│ ※ AIPC は計測コードを持たない。下り送信側 (①) は有線 mirror の      │
│   外部 sniff で取得。sniffer は ②(下り) と ⑤(上り) の両方を担う     │
└────────────────────┬───────────────────────────────────────────┘
                     ↓ 統一 event (type + source + t + payload)
┌─ 記録部 (recorder) ── 生値を 3 系統スキーマに保存 ───────────────┐
│  downlink 表: PK=(cycle_count, robot_id)、各 leg の生時刻を列に    │
│  uplink 表:   PK=(robot_id, ul_seq)                             │
│  timesync 表: PK=時刻、PPS bridge / marker の生値                │
│  sink: record=CSV/Parquet(永続) / live=SQLite(tmpfs, 直近5min)   │
└────────────────────┬─────────────────────────────────────────┘
                     ↓
┌─ 計測部 (analyzer) ── 記録の生値から導出 ───────────────────────┐
│  PpsBridgeEstimator: timesync 表 → tsf→unix 関数 (drift 線形回帰)  │
│  OwdComputer: downlink 表 join → 4 leg + total、cycle 単位集計     │
│  LossAnalyzer: cycle_count 連番から欠落検出 (24bit、wrap 考慮不要) │
└──────────────────────────────────────────────────────────────┘
```

## 3. データソースと cycle_count キーの結合可能性

| ソース | cycle_count | robot_id | 備考 |
|---|---|---|---|
| **wire SPAN** (wire_capture、AIPC→AP frame を mirror で sniff) | △ **payload parse 要** | △ payload offset 02 / dst IP | **要改修**: wire_capture.py で UDP payload offset 51-53 抽出 + PHC hwtstamp。AIPC は計測コードを持たず、送出時刻は payload offset 38-45、物理通過時刻は PHC で取得 |
| HID rx_dl (52000+id) | ✅ payload offset 51-53 を報告 | ✅ port/offset 02 | spec 追加済 (radio_metrics)。**要実装**: metrics_radio で parse |
| sniffer air frame (UART) | △ **payload parse 要** | △ dst MAC | **要改修**: sniffer.ino で offset 51-53 を binary record に。open auth で平文読取可 |
| PPS bridge / marker | — cycle 無関係 | — | timesync 表 (時刻キー) |
| uplink / tx_ul (上り) | — **上りに cycle 概念なし** | ✅ | uplink 表 (ul_seq キー) |

> **送信側 (AIPC tx) は独立 source ではなく wire SPAN の一部**。pc_emulator (試験) も本番 AI も payload に cycle_count / robot_id / 送出時刻 (offset 38-45) を埋めるが、計測系はそれを **有線 mirror port で外部 sniff** して取得する (`CLAUDE.md`「AIPC は計測コードを持たない」原則)。`pc_emulator.py` の改修は payload offset 51-53 への 24bit cycle_count 書込のみ。

> `TEAM_SSID_OPEN` は **open auth (無線暗号化なし)** のため、sniffer / wire は air/SPAN frame の payload を平文で読める。これが air/wire leg を cycle_count キーで結合できる前提。

## 4. スキーマ (3 系統)

### 4.1 downlink 表 — PK=(cycle_count, robot_id)

| 列 | source | 意味 |
|---|---|---|
| `cycle_count` | wire SPAN (payload) | PK。24bit、wrap 拡張不要 |
| `robot_id` | wire SPAN (payload) | PK |
| `t_tx_unix` | wire SPAN (payload offset 38-45) | AI 送出時刻 (AI/AIPC clock)。payload に埋め込まれた値 |
| `t_wire_phc` | wire SPAN (PHC hwtstamp) | **同じ frame を Xikestor mirror で sniff した時刻** (PHC、ns) |
| `t_air_tsf` | sniffer | air RX 時の AP TSF |
| `t_air_recv_unix` | sniffer | sniffer UART 受信時の RasPi unix |
| `t_hid_rx_tsf` | HID rx_dl | HID 受信時の AP TSF |
| `t_hid_rx_esp` | HID rx_dl | HID esp_timer (**記録のみ、計測未使用**) |
| `corr_unix_time` | HID rx_dl | payload 送信時刻コピー (`t_tx_unix` と同値、**記録のみ、計測未使用**) |
| `rssi`, `frame_size` | sniffer/HID | 補助 |

> **`t_tx_unix` と `t_wire_phc` は同じ AIPC→AP frame の 2 種の時刻**:
> - `t_tx_unix` = AI が payload に埋めた論理送出時刻 (AI/AIPC clock)
> - `t_wire_phc` = その frame が **Xikestor mirror port を通過した物理時刻** (RasPi PHC、ns)
>
> → **有線区間遅延 = `t_wire_phc − t_tx_unix`** で、AI が時刻を埋めてから switch SPAN ポートに乗るまで (= AIPC ホスト内部 socket→NIC + wire 区間) を計測できる。AIPC は計測コードを持たず、すべて有線ミラーの外部観測で取得する。

→ **計測部は `t_*_tsf` を PPS bridge で unix 化し、cycle_count キーで join して 4 leg を算出**。leg 分解:
> - **wire 区間** (AIPC 内部 + 有線): `t_wire_phc − t_tx_unix`
> - **AP queue + air 区間**: `t_air_recv_unix(bridge) − t_wire_phc` ※ air TSF を PPS bridge で unix 化
> - **air→HID 区間**: `t_hid_rx(bridge) − t_air(bridge)`
> - **total OWD**: `t_hid_rx(bridge) − t_tx_unix`

### 4.2 uplink 表 — PK=(robot_id, ul_seq)

上り (HID→AIPC) は cycle 概念がないため別系統。経路は `HID → (air) → AP → (wire) → RasPi/AIPC`。`ul_seq` をキーに 3 計測点 (収集部 ④⑤⑥) を結合する。

| 列 | source | 意味 |
|---|---|---|
| `robot_id` | HID tx_ul | PK |
| `ul_seq` | HID tx_ul | PK。上り送信の per-type カウンタ (損失検出キー) |
| `hid_seq` | HID tx_ul | HID 全 emit 通し番号 (発行順序の解釈用) |
| `tx_port` | HID tx_ul | 送信先ポート (50000=通常テレメトリ / 51000=CAN テレメトリ) |
| `t_hid_tx_tsf` | HID tx_ul (④) | HID 送信直前の AP TSF |
| `t_air_tsf` | sniffer (⑤) | HID→AP air frame (broadcast) の AP TSF |
| `t_air_recv_unix` | sniffer (⑤) | sniffer UART 受信時の RasPi unix |
| `t_recv_unix` | uplink listener (⑥) | RasPi/AIPC が 50000+id broadcast を受けた unix |
| `frame_size` | HID/sniffer | 補助 |

**leg 分解** (`t_*_tsf` は PPS bridge で unix 化):
> - **HID→air 区間**: `t_air(bridge) − t_hid_tx(bridge)` — HID 内部 tx 処理 + air 送出
> - **air→host 区間**: `t_recv_unix − t_air(bridge)` — AP 中継 + wire/broadcast 戻り
> - **total UL OWD**: `t_recv_unix − t_hid_tx(bridge)`

**下りとの違い・注意点**:
- **cycle_count を持たない**。上りは HID 発で、AI 送出周期と無関係 → `ul_seq` が損失検出/結合キー
- **全 broadcast (50000/51000/52000+id)**。sniffer は air 上で全 STA 宛 broadcast を観測でき、chip filter の影響を受けない (`docs/lessons_learned.md` §C.2)
- **tx_ul (52000+id) と実 uplink (50000+id) は別パケット**。tx_ul は「HID が 50000 へ送る直前」の metrics 報告。両者を `ul_seq` で対応付ける (上り payload に ul_seq が無い場合は時刻近接、`radio_metrics.md` §4.2.1 の曖昧性に留意)
- sniffer ⑤ と tx_ul ④ の air frame 対応も `ul_seq` (broadcast payload に含まれれば) or 時刻近接で取る

### 4.3 timesync 表 — PK=時刻

PPS bridge (`unix_assert`, `tsf`, `bridge_offset`) と PPS marker。cycle と独立。計測部の `PpsBridgeEstimator` が tsf→unix 変換に使う共通リソース。

## 5. 各層の責務とモジュール境界

### 5.1 収集部 (collector)
- 各 source を thread で読み、**生 event** (統一 dataclass: `type`, `source`, `t_local`, `payload`) を emit
- **加工しない** (時刻変換・join は計測部の責務)
- 各 thread に heartbeat (last_event_t)、監視 thread が無音検出 → log + (live) 再起動
- 二重受信 dedup: MetricsListener で `(robot_id, hid_seq)` LRU set

### 5.2 記録部 (recorder)
- event を 3 系統スキーマに振り分けて生値記録
- **sink は pluggable**:
  - `RecordSink`: append-only CSV/Parquet (SD/外部、提出データ)
  - `LiveSink`: SQLite (tmpfs)、`DELETE WHERE t < now-300` で 5min ring。WebUI が `SELECT` で window 取得 (tail 廃止)
  - 両方同時可 (record しつつ live 表示)

### 5.3 計測部 (analyzer)
- 記録の生値から導出。**cycle_count (キー) + TSF→PPS bridge (時刻) のみ依存**
- `PpsBridgeEstimator`: timesync 表から `tsf_unix = a·tsf + b` を rolling 線形回帰 (drift 9.52ppm 追従)、per-packet O(1) 変換
- `OwdComputer`: downlink 表を cycle_count で join → 4 leg + total、live per-packet 適用
- `LossAnalyzer`: cycle_count 連番から欠落検出。24bit なので 1.9 日まで wrap 考慮不要

### 5.4 WebUI の位置づけ (層ではなく consumer)

WebUI (`tools/dashboard/app.py`、Streamlit) は **3 層のいずれにも属さない下流 consumer**。収集部・記録部・計測部に**埋め込まない**:

- **計測部に埋めない**: 計測部は「cycle_count キー join + TSF→PPS bridge」の純変換層に保つ。表示の都合 (リフレッシュ間隔・窓・描画) が計測ロジックに混入すると、CLI/バッチでの再解析が阻害される
- **収集部・記録部に埋めない**: この 2 層は「計測非依存で生値を落とすだけ」が要件。表示を持たせると本走時の負荷・責務が濁る
- **WebUI 停止が計測・記録に一切影響しない** (fire-and-forget consumer) ことを担保

WebUI は **記録部 store と計測部出力の両方を読む**:

| 読む先 | 内容 | 現 `app.py` での該当 |
|---|---|---|
| 記録部 live store | TX rate / loss / air→HID rate 等の**素の流量** (計測部を通さない生値) | Live タブ (tmpfs CSV tail、P1 で SQLite SELECT に移行) |
| 計測部 出力 | OWD median/p99、PPS Δt、bridge offset 等の**派生値** | Overview タブ |

→ live 監視時は記録部の live store を直読み + 必要に応じ計測部を on-demand に回して派生値を重畳。本走 (記録のみ・表示なし) は収集→記録の 2 層だけ動かせば足り、WebUI は起動しなくてよい。

> **WebUI framework は差し替え可能**。録画制御を daemon A の unix socket (§5.6) に置いたため、WebUI は store を読み socket を叩くだけの薄い consumer。現行 Streamlit (`app.py`、821 行、Plotly + tailscale 配信済) を据え置く。乗り換え検討は「live per-packet OWD / 4-leg を全レート描画時に 2s full rerun で RasPi CPU が詰まる」場合のみ → push 型 (FastAPI+SSE / partial callback の Dash)。framework 選定は daemon A に依存しないので後から低コストで変更できる。

## 5.5 プロセスモデル (daemon 分離)

3 論理層を **3 daemon にはせず「2 daemon + WebUI」** に分離する。境界は記録部の store (tmpfs SQLite / CSV)。

```
┌─ daemon A: gtnlv-rpid ────────────┐
│  収集部 + 記録部                   │   ← 本走で動かす唯一の必須プロセス
│  UART/PPS/UDP capture → store 書込 │
└───────────┬────────────────────┘
            ↓ store (tmpfs SQLite / CSV) = プロセス境界
┌─ daemon B: gtnlv-analyzer ────────┐   ← live 時のみ常駐。record path は batch
│  計測部 (PPS bridge / OWD / loss)  │      (analyze.py / sniffer_bridge.py を都度実行で代替)
└───────────┬────────────────────┘
            ↓
        WebUI (Streamlit) = 別プロセスの consumer
```

| 判断 | 理由 |
|---|---|
| **収集部 + 記録部 = 1 daemon (A)** | 高レート event path を共有 (sniffer 2Mbps + 100Hz×複数 source)。分離すると生 event を毎回 IPC シリアライズ → §2.16 系の遅延を自作。収集が止まれば記録は無意味＝同一障害ドメインで可。thread watchdog (P3) で固める |
| **計測部 = 別 daemon (B)** | submission data の保全。SnifferReader hang で live 全体が死んだ実績 (§1.1) → 計測部 crash が capture を巻き込まない。CPU 重い (rolling 回帰/join) ので GIL を capture から分離。store が既に自然な IPC 境界 |
| record path の analyzer は daemon 化不要 | 既存 `analyze.py` / `sniffer_bridge.py` の batch 実行で足りる。live のときだけ B を常駐 |
| WebUI = 別 consumer | §5.4 の通り。停止しても A/B 無傷 (fire-and-forget) |

**運用**: 本走は daemon A のみ (systemd `Restart=on-failure` で自動復帰、現状 tmux より堅い)。live 監視は A + B + WebUI の 3 プロセス。

## 5.6 sink トグルと UI 起点の録画制御

「恒久保存 ⇄ live のみ」は **排他モードにせず、2 つの sink を独立に on/off する合成**にする (§5.2 が「record しつつ live 表示」を要件化しているため)。

### sink 2 系統 (起動フラグ)

| フラグ | sink | 保存先 | 挙動 |
|---|---|---|---|
| `--record <DIR>` | RecordSink | SD / 外部 SSD (永続) | append-only、削除なし、提出データ |
| `--live [--live-keep-s 300]` | LiveSink | tmpfs (`/dev/shm`) | SQLite ring、直近 N 秒のみ、SD 非消費 |

少なくとも一方を必須 (両方省略は起動エラー = 「何も保存しない誤起動」防止)。現 `--out-dir` + `--keep-recent-s` をこの 2 フラグへ整理 (`--keep-recent-s` → `--live-keep-s`)。

| `--record` | `--live` | recording 初期状態 | ユースケース |
|:---:|:---:|---|---|
| ✅ | ❌ | ON (boot から) | **本走**: 提出データのみ、UI 不要、最小負荷 |
| ❌ | ✅ | OFF (UI で開始) | **開発監視**: ring on、UI から start/stop |
| ✅ | ✅ | ON + ring | **overnight**: 記録しつつ監視、UI で segment 切替可 |

### UI 起点の録画 = RecordSink を runtime トグル (capture hot path 不変)

```
capture (UART/PPS/UDP)
   │  ← 常に同じ。構造的分岐を増やさない
   ├──→ LiveSink (tmpfs ring, 常時 on)      ← WebUI/analyzer が読む
   └──→ RecordSink (永続, tee)  …on/off は per-event の bool 1 個
              ▲
              │ start/stop  (WebUI → daemon A 制御チャネル)
```

- live ring は **常時 on**。RecordSink へ流すかは **per-event の bool フラグ 1 個** = パイプライン構造不変。capture 自体の挙動を変える「mid-run mode 分岐」ではないので §2.16 系の遅延リスクを生まない。
- pre-roll/再シリアライズ回避のため RecordSink は daemon A 内の tee のまま (別 daemon 化しない)。

### 制御チャネル: daemon A の unix domain socket (制御専用 thread)

- WebUI (別プロセス) → daemon A へ `start_record {tag}` / `stop_record` / `status` を送る。localhost 限定の unix socket、ポート管理不要。
- **capture thread とは別 thread**で受けるので hot path に触れない。data store に制御を混ぜない (store は計測データ専用に保つ)。
- 起動時 `--record` 指定なら recording は ON 状態でこの socket を開く (本走中に UI から stop も可)。

### pre-roll (録画前の N 秒も残す)

`start_record` 時に **まず live ring の現バッファ (直近 N 秒) を永続側へ flush してから** 継続 append する。

→ DFS freeze / §2.20 の worst-case 803ms spike のような過渡現象は「見てから録画」では間に合わない。pre-roll があれば「異常を UI で見て録画ボタン → その手前 N 秒込みで保存」ができる。これが UI 起点録画の最大の価値。WebUI には **Start/Stop Recording ボタン + 録画状態 (tag / 経過 / サイズ)** を出す。

> escape hatch: socket が使えない最小構成向けに、SIGUSR1 で「現 ring を永続へ dump」だけ受ける実装を残してもよい (任意・後回し可)。

## 6. 生値記録の方針 (重要)

`corr_unix_time` と `t_rx_esp_timer_us` は **記録するが計測ロジックでは使わない**:

| field | 記録 | 計測使用 | 理由 |
|---|:---:|:---:|---|
| `cycle_count` | ✅ | ✅ | 損失検出・join キー |
| `t_*_tsf` | ✅ | ✅ | PPS bridge で unix 化、OWD 主軸 |
| `corr_unix_time` | ✅ | ❌ | cycle_count + AIPC 送信記録で送信時刻が引けるため冗長。後検証の保険で記録のみ |
| `t_rx_esp_timer_us` | ✅ | ❌ | TSF 不連続検出・較正検証の補助。生値保全のため記録のみ |

→ **「生データは全部残す、計測ロジックは最小依存」**。計測手法を変えても記録形式は不変、過去データを再解析できる。

### 6.1 esp_timer の将来の活用先 (記録のみ → 必要時に計測部へ追加)

`t_rx_esp_timer_us` (HID local の単調連続 clock) は今は計測未使用だが、生値記録してあるため、後から計測部に診断モジュールとして追加できる。TSF (AP 同期) と性質が違うため、両者を突き合わせると以下が分離計測できる:

| # | 取れるデータ | 仕組み | 価値 |
|---|---|---|---|
| 1 | AP clock vs HID clock の相対 drift | tsf_us と esp_timer_us を時系列で線形 fit → slope 差 | HID の TSF 追従度・AP clock 安定性を分離。時刻同期の質の根拠 |
| 2 | **TSF 不連続 (AP 再 associate) の検出** | esp_timer 連続 / TSF は re-assoc で jump → 乖離検出 | bridge 適用を弾く / re-assoc マーク。**§2.20 の worst-case 803ms / p99 5.12ms spike が AP queue freeze か TSF jump かを切り分け**できる |
| 3 | HID 内部処理遅延 (rx→emit) | rx 時 esp_timer と metrics emit 時 esp_timer の差 | FreeRTOS スケジューリング遅延 (§2.15 task 化の効果) を HID-local で直接計測 |
| 4 | TSF 較正残差 (較正品質) | (esp_timer中点, TSF) 線形回帰の予測 vs 実測 | PPS bridge floor 58μs の内訳分解 (HID 内較正の寄与) |
| 5 | bridge 非依存の HID-local 受信間隔 jitter | 2 つの rx_dl の esp_timer 差 | AP TSF / PPS bridge jitter 非依存の純粋な packet 間隔ばらつき |

→ 提出主軸 (OWD/loss) は cycle + TSF bridge に最小依存させて堅牢に保ち、**品質診断が必要になったら #2 (TSF 不連続) あたりから計測部に追加**するのが自然。記録形式を変えずに拡張できる。

## 7. 前提作業 (実装着手前)

| 作業 | 内容 | 依存 |
|---|---|---|
| **sniffer.ino cycle_count parse** | air frame payload offset 51-53 (24bit) → binary record (TYPE_FRAME に列追加)。open auth で平文 | air leg の cycle 結合 |
| **pc_emulator.py cycle_count 書込** | offset 51-53 に 24bit cycle_count (送出ごと +1)、aipc_seq 廃止。payload offset 38-45 の送出時刻は従来通り | wire SPAN で sniff される送信側 |
| **wire_capture.py payload 抽出** | mirror port で sniff した AIPC→AP frame の UDP payload offset 51-53 (cycle_count) + offset 38-45 (t_tx_unix) を抽出し、PHC hwtstamp (t_wire_phc) と併せて記録 | wire leg + 有線区間遅延 (t_wire_phc − t_tx_unix) |

## 8. 段階的移行計画 (提出 6/26 まで)

| Phase | 内容 | 備考 |
|---|---|---|
| **P0** | sniffer.ino + pc_emulator cycle_count 対応 | 前提作業 |
| **P1** | 記録部の SQLite live sink 導入、WebUI を SQL 読みに | tail 廃止、SnifferReader hang 解消 |
| **P2** | 計測部 PpsBridgeEstimator で live per-packet OWD | live で真の OWD (現状 raw approx を脱却) |
| **P3** | 収集部 二重受信 dedup + thread watchdog | robustness |
| **P4** | leg join live 化 (sniffer/wire cycle_count 結合) | 4 leg live 表示 |

> **record path (overnight CSV/Parquet) は現状の解析資産 (analyze.py / sniffer_bridge.py) を活かすため維持**。live path のみ SQLite 化するハイブリッドが現実的。

## 9. 関連ドキュメント

- `robot_comm_spec` downlink_command.md (offset 51-53 cycle_count)、radio_metrics.md (rx_dl)
- `docs/measurement_architecture.md` (機器役割・3 軸時刻同期)
- `docs/pps_sync_design.md` (PPS bridge §5.2、§10.6)
- `docs/phase3_findings.md` §2.18-2.20 (PPS bridge 精度、OWD 4 方式比較)
