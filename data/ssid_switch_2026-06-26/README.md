# SSID 切替時間測定 (2026-06-26、Open<->normal トグル、6台 batch)

set_ssid.py で downlink set_ssid(port 41000+id)を送り Open(TEAM_SSID_OPEN)<->
normal(TEAM_SSID WPA2)をトグル。outage を測定。

## 重要: 測定法
- ❌ rx_dl 到着時刻(t_rpid_recv_unix)ギャップ = batch(rx_dlb)の flush 間隔 0.5s が床に
  なり真の outage をマスク(switch_arrivaltime_CONFOUNDED.txt)。
- ✅ cycle_count(sender 単調カウンタ)の欠落数 ÷ レート = batch flush 非依存の真の outage
  (switch_cyclecount.txt、switch_time.py の現行版)。

## 結果(cycle_count 基準、6回トグル × 6台)
- 大半の切替: outage ~0(欠落 cycle 0 = <1 周期 ≈ <16ms @60Hz)。同一 AP の別 SSID 間
  re-assoc が高速。
- 稀に worst-case 8.40s / 9.48s(遅い WPA2 4-way handshake → 未接続時 firmware が既定 SSID へ
  revert)。-> normal: mean 0.47s/max 8.40s、-> open: mean 0.68s/max 9.48s(外れ値支配)。

## 注意・残課題
- revert-to-default 挙動でトグルの「切替先」帰属が乱れる(既に目標 SSID なら no-op→0)。
  厳密な方向別統計には robot 側の実 SSID 状態確認 + revert 抑止が必要。
- batch では cycle_count 基準が必須。per-frame なら到着時刻でも可。
- 再現: RasPi5 で sudo python3 src/switch_time.py <seq...>(live DB tmpfs 依存)。

## 追記: on-device マーカー法 (権威値、firmware SWMARK)
HID firmware に SWMARK を追加: set_ssid 受信時 `SWMARK recv <millis>`、接続完了
(ARDUINO_EVENT_WIFI_STA_GOT_IP) 時 `SWMARK conn <millis>`。ttyACM(XIAO USB-CDC)で
読み、recv→conn の on-device millis 差 = 真の切替時間(network/batch 非依存)。

結果 (r1,r2、各6回トグル、switch_marker_ondevice.txt):
- →normal(WPA2): ~1.15s (1142-1162ms、非常に安定)
- →open       : ~1.57s (1560-1620ms、安定)
- 全体 mean 1366ms / min 1142 / max 1620、sd ~210ms(方向差が支配)

知見: 切替時間は recv→IP取得で ~1.15-1.57s。意外にも **Open への切替の方が遅い**
(WPA2 1.15s < Open 1.57s、両台・全回で一貫)。先の cycle_count 法(~0/稀に8-9s)は
batch flush 交絡 or 部分再接続を見ていた可能性で、本マーカー法が権威値。
src/switch_marker.py で再現(RasPi5、r1/r2 に SWMARK firmware 必要)。
