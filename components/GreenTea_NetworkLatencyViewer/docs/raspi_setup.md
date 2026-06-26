# 計測 RasPi セットアップ手順

> 計測ホスト (Raspberry Pi 5) の環境構築チェックリスト。**現在 Ubuntu 25.10**
> だが後日 **Raspberry Pi OS へ移行予定**。OS 入れ替え後にこの手順を上から
> 実行すれば計測環境を復元できるよう、必要な設定を網羅する。
>
> 移行時に OS 依存で変わる箇所は **「RPi OS 注意」** で明示。

## 0. 役割とハードウェア前提

計測 RasPi5 の各インターフェースの役割 (`docs/measurement_architecture.md` §1 と対応):

| IF | 役割 | 設定要点 |
|---|---|---|
| **eth0** (内蔵 macb) | SPAN destination (mirror dst)、wire 側 hwtstamp 取得 | IP 無し、`promisc on`、PHC `/dev/ptp0` (or `/dev/ptp1`、要 `ethtool -T eth0` 確認) で ns 精度 RX timestamp |
| **eth1** (USB-Eth、Realtek RTL8153 等) | 制御平面。計測 LAN (192.168.4.x) で gtnlv-rpid の UDP socket recv | `.4.212` (AP DHCP)、software TS のみ (hwtstamp 非対応)。RPi OS は USB-Eth を `enx<MAC>` でなく **`eth1`** で命名する |
| **wlan0** | 上流管理アクセス (SSH)。計測経路と分離 | 上流 `TEAM_SSID` / `192.168.1.141` |
| **EC25J** (USB 経由) | LTE backhaul (会場 remote access)。計測経路と独立 | ModemManager 管理 (udev rule 必要、§8.5.1)。irumo (docomo) で接続実績、au系 SIM は不可。GNSS 時刻同期は不使用 |
| **/dev/ttyUSB0** | sniffer C5 (CP2102N、SN `46adfa..`) | UART 2 Mbps binary |
| **/dev/ttyUSB1** | reflector C5 (CP2102N、SN `38ca64..`) | 電源 + flash 用 |
| **/dev/ttyUSB2-5** | EC25J (Quectel、Android descriptor) | AT primary = `/dev/ttyUSB4` (mmcli `at` 表示)。番号は plug 順で変動、`serial/by-id/` 経由が確実 |
| **/dev/pps0** | sniffer GPIO10 → BCM18 PPS | `dtoverlay=pps-gpio,gpiopin=18` 経由。`ppstest /dev/pps0` で 1Hz event |
| **/dev/pps1** | eth0 PHC PPS | `cat /sys/class/pps/pps1/name` で `ptp0` 表示 |

> pps0 / pps1 の **割当は boot 順依存** — `cat /sys/class/pps/*/name` で
> `pps@12.-1` (pps-gpio) と `ptp0` (PHC) の対応を確認すること。
> `pps@12` の `12` は **物理 pin 番号** (BCM18 = pin12) を意味する。

現行ベースライン (確認値):
- OS: **Raspberry Pi OS bookworm** aarch64、kernel `6.12.75+rpt-rpi-2712`
- eth0 hwtstamp: hardware-receive 対応 ✅、PHC `/dev/ptp0`
- USB-Eth: `eth1` で 192.168.4.212 (DHCP)
- LTE: EC25J + irumo SIM、wwan0 ✅ (default route metric 700)
- PPS: `/dev/pps0` (BCM18 sniffer) 1Hz event 確認済

## 1. OS パッケージ

```bash
sudo apt update
sudo apt install -y \
  chrony \        # NTP master 化
  linuxptp \      # hwstamp_ctl (eth0 RX hwtstamp filter 設定)
  ethtool \       # hwtstamp capability 確認
  tcpdump \       # wire/air pcap キャプチャ
  tmux \          # SSH 切断耐性 (background task を確実に常駐させる、後述 §9 注意)
  pipx \          # esptool の隔離インストール
  pps-tools gpiod \  # PPS 同期 (docs/pps_sync_design.md) — ppstest / gpioinfo
  python3 python3-pip python3-serial
```

> **PPS 同期**: `docs/pps_sync_design.md` の C5 GPIO PPS 同期。
> `/boot/firmware/config.txt` 末尾に `dtoverlay=pps-gpio,gpiopin=18` を追加して
> 再起動。pps-gpio 系統は **`/dev/pps0`** に出る (boot 順、要 `cat /sys/class/pps/*/name`
> 確認: `pps@12.-1`=pps-gpio、`ptp0`=eth0 PHC)。40-pin header は gpiochip0 (pinctrl-rp1)。

**RPi OS 注意**: 全て Debian パッケージ名共通でそのまま動く。

## 2. Python 環境

| 用途 | パッケージ | インストール |
|---|---|---|
| sniffer UART decode (`sniffer_runner`, `gtnlv_rpid`) | **pyserial** | `apt install python3-serial` (3.5 確認済) |
| air/wire pcap 解析 (`owd_analyzer/air_wire_diff.py`) | **scapy** | `pipx install scapy` または `pip3 install --break-system-packages scapy` |

> scapy は `air_wire_diff.py` (AX210 monitor pcap × eth0 SPAN pcap の join) でのみ使用。
> air-wire diff 計測をしないなら不要。**現状 RasPi には未インストール** (必要時に追加)。

その他 (`gtnlv_rpid.py`, `wire_capture.py`, `analyze.py`, `sniffer_bridge.py`)
は標準ライブラリのみで動作。

## 3. esptool (ESP32-C5 flash)

```bash
pipx install esptool        # ~/.local/bin/esptool (v5.x)
pipx ensurepath             # PATH に ~/.local/bin を追加
```

> **RPi OS 注意**: Ubuntu の `apt` 同梱版 (`/usr/bin/esptool`、4.7.0) は古い。
> pipx 版 (5.x) を使う。本リポジトリの flash 手順は `~/.local/bin/esptool`
> 前提 (`--chip esp32c5 write-flash --flash-mode qio --flash-freq 80m --flash-size 8MB`)。

## 4. ネットワーク設定

計測経路 (192.168.4.x) と管理経路 (192.168.1.x) を **必ず別 IF に分離** する
(同 subnet だと broadcast 二重受信、`docs/lessons_learned.md` §C.9)。

| IF | 接続先 | IP |
|---|---|---|
| USB-Eth | Xikestor switch の計測 LAN port | 192.168.4.103 (AP DHCP) |
| wlan0 | 上流 `TEAM_SSID` (管理用 WiFi) | 192.168.1.140 |
| eth0 | Xikestor の mirror destination port | **IP 割当なし** |

**RPi OS 注意**: Ubuntu は netplan、RPi OS は NetworkManager (`nmcli`) か
`dhcpcd`。eth0 は IP 不要なので "no IP / link up only" に設定。wlan0 は
上流 SSID に接続。USB-Eth は計測 LAN で DHCP。

## 5. chrony を NTP master 化

会場 LAN にインターネット接続が無い前提。RasPi を **NTP master** にして
AIPC が RasPi に同期 (`docs/measurement_architecture.md` §8、`phase3_findings.md` §2.10)。

`/etc/chrony/conf.d/10-ntp-master.conf` を新規作成:

```
local stratum 10
allow 192.168.4.0/24
```

```bash
sudo systemctl restart chrony
sudo ss -anu | grep ':123 '   # NTP server が listen していることを確認
chronyc tracking              # Stratum が表示されれば OK
```

> `local stratum 10` は上流 NTS が unreach な時の fallback。開発環境で
> 上流があると AIPC が canonical を優先するので、開発時に master 化効果を
> 測るなら AIPC 側で canonical を `chronyc -a delete` する (詳細 §8.2 of
> measurement_architecture.md)。会場では自動的に RasPi のみ selectable。

## 5.5 SSH 公開鍵認証

無人 overnight ラン + ssh 越し flash / tcpdump / 解析回しのため、host PC (AIPC)
から **パスワード入力なしで ssh が通る**状態にする。RPi OS 移行時は毎回必要。

```bash
# host PC で公開鍵が無ければ生成 (RSA 4096 or ed25519)
[ -f ~/.ssh/id_rsa.pub ] || ssh-keygen -t rsa -b 4096 -N '' -f ~/.ssh/id_rsa

# RasPi の現 IP / hostname を <RPI_HOST> として、初回のみ password 入力
ssh-copy-id gochiuma@<RPI_HOST>

# 動作確認 (鍵接続でパスワードなしで通ること)
ssh gochiuma@<RPI_HOST> hostname
```

> パスワード認証は **無効化しない** (RasPi が SSH 鍵だけになるとトラブル時に
> 物理ターミナル以外で復旧できなくなるため)。鍵接続を default にしつつ
> password も残す方針。

## 6. sudo パスワードレス

無人 overnight ラン + SSH 越し sudo (tcpdump / wire_capture / esptool) のため:

```bash
echo 'gochiuma ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/gochiuma-nopasswd
sudo chmod 440 /etc/sudoers.d/gochiuma-nopasswd
```

> セキュリティ上、計測専用ホストでのみ。本番運用で気になるなら対象コマンドを
> `tcpdump`, `ip`, `hwstamp_ctl` に絞ること。

## 7. eth0 hwtstamp + promiscuous (計測前の毎回設定)

SPAN mirror frame を AF_PACKET で受けるには promiscuous 必須
(`docs/lessons_learned.md` §C.14)、PHC hwtstamp には RX_ALL filter が必要:

```bash
sudo ip link set eth0 up
sudo ip link set eth0 promisc on
sudo hwstamp_ctl -i eth0 -r 1 -t 0    # RX hwtstamp = ALL, TX = off
```

> `wire_capture.py` 起動前に必須。systemd service で永続化していないので
> (現状 custom service 無し)、ラン前にスクリプトで実行する。
> **RPi OS 注意**: macb ドライバが PHC を出すかは kernel 依存。RPi OS の
> 公式 kernel でも RasPi5 (BCM2712) の Gigabit Ethernet = macb は PHC 対応の
> はずだが、移行後 `ethtool -T eth0` で `hardware-receive` が出るか必ず確認。
> 出ない場合は software TS (`t_wire_sw_ns`) のみで wifi_leg 計測は継続可能。

## 8. 計測ツール配置

本リポジトリ `tools/` 配下を RasPi の `~/gtnlv/` にコピー:

```bash
# host PC 側から (例)
scp -r tools/rpi_daemon tools/owd_analyzer tools/sniffer_runner \
       tools/cal_sender tools/pc_emulator \
       gochiuma@192.168.4.103:~/gtnlv/
```

配置後の構成:

```
~/gtnlv/
├── rpi_daemon/
│   ├── gtnlv_rpid.py      # 計測デーモン (sniffer UART + UDP socket recv)
│   └── wire_capture.py    # eth0 SPAN + PHC hwtstamp
├── owd_analyzer/
│   ├── analyze.py         # OWD 統計 + loss
│   ├── sniffer_bridge.py  # TSF↔unix bridge
│   └── air_wire_diff.py   # scapy で air/wire 差 (要 scapy)
├── sniffer_runner/run.py  # sniffer binary decoder (offline)
├── cal_sender/cal_sender.py
└── pc_emulator/pc_emulator.py
```

> firmware ビルドは host PC の `tools/.bin/arduino-cli` で行い、`.bin` を
> scp → RasPi の esptool で write-flash する (RasPi に arduino-cli は不要)。

## 8.5 EC25J (LTE backhaul) セットアップ

**Quectel EC25-J** (LTE Cat-4、USB 2.0 信号) を USB 接続 (旧 AX210 の M.2 B-key
スロットでも USB 経由でも可)。会場 LAN にインターネットが無い時の
**remote access / データ吸い上げ用**。**GNSS / 時刻同期には使わない** (RasPi NTP
master を維持、§5)。NITZ 時刻 (秒精度) は `AT+QLTS=2` で取得可、chrony fallback 用。

```bash
sudo apt install -y modemmanager libqmi-utils libmbim-utils
```

### 8.5.1 ModemManager に EC25J を認識させる (重要)

**ModemManager 1.24 (Debian trixie) は Quectel EC25 (VID `2c7c`) を plugin
allowlist に持たない**ため、udev rule で `ID_MM_DEVICE_PROCESS=1` を付与しないと
modem として probe されない:

```bash
sudo tee /etc/udev/rules.d/77-mm-ec25-process.rules > /dev/null <<'EOF'
ACTION=="add|change", SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0125", ENV{ID_MM_DEVICE_PROCESS}="1"
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0125", ENV{ID_MM_DEVICE_PROCESS}="1"
SUBSYSTEM=="net", SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0125", ENV{ID_MM_DEVICE_PROCESS}="1"
SUBSYSTEM=="usbmisc", SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0125", ENV{ID_MM_DEVICE_PROCESS}="1"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo systemctl restart ModemManager
sleep 12
mmcli -L                        # /org/.../Modem/0 [QUALCOMM ...] が出れば OK
```

### 8.5.2 APN 設定 + 接続 (irumo / docomo の例)

```bash
# APN 設定 (SIM キャリアの APN に置換)
sudo nmcli connection add type gsm ifname '*' con-name lte \
  apn spmode.ne.jp \
  ipv4.route-metric 700 ipv6.method ignore
sudo nmcli connection up lte
ip -br addr show wwan0          # 10.x.x.x が付けば LTE 接続成功
ping -c 3 -I wwan0 8.8.8.8      # LTE 経由疎通
```

### 8.5.3 キャリア選定の注意 (KDDI 系 SIM 拒否問題)

**au系 SIM (povo, UQ mobile 等) は EC25J IMEI を網側拒否することがある** (実機で
`AT+CEER` = `5, 33` = "Requested service option not subscribed" を確認、
スマホで同 SIM は接続 OK なため網側 IMEI 制限と推定)。

| キャリア | EC25J 互換性 |
|---|---|
| **docomo 系 (docomo / OCN モバイル ONE / IIJmio D / irumo)** | ✅ 互換性高 |
| SoftBank 系 | △ band 8 主、IMEI 制限緩い (未検証) |
| au / KDDI 系 (povo / UQ) | ❌ 実機で接続不可 (cause 33) |

### 8.5.4 LTE 経由時刻情報 (NITZ)

```bash
sudo systemctl stop ModemManager      # MM が AT port を握るので一時停止
# /dev/ttyUSB? の AT port (mmcli 出力で "at primary" の port、典型 ttyUSB4)
python3 -c '
import serial
s = serial.Serial("/dev/ttyUSB4", 115200, timeout=2)
for cmd in ["AT+CCLK?", "AT+QLTS=2", "AT+CTZU?"]:
    s.write((cmd+"\r").encode()); import time; time.sleep(1)
    print(s.read(s.in_waiting).decode())
s.close()
'
sudo systemctl start ModemManager
```

精度は **秒オーダー** (3GPP TS 24.008 NITZ 仕様)、μs 計測には使えない。
internet/GNSS 切断時の chrony fallback としてのみ価値あり。

> **計測経路との独立性**: LTE IF (wwan0) は **default route にしない** こと
> (計測トラフィックが LTE に逃げると測定が壊れる)。`ipv4.route-metric 700` 等で
> eth1 (100) より下に置く。
>
> **RPi OS 注意**: USB 経由なら kernel driver (`option`, `qmi_wwan`) は標準で
> 自動 load。M.2 経由でも USB 信号として認識されるので同じ手順。GNSS を使う
> 場合のみ `AT+QGPS=1` + 1PPS 配線が要るが、本構成では不使用。

## 9. 動作確認チェックリスト

移行後、以下が通れば計測可能:

```bash
# OS / kernel
uname -a

# パッケージ
which chrony tcpdump tmux hwstamp_ctl ethtool
~/.local/bin/esptool version

# Python
python3 -c 'import serial; print("pyserial", serial.__version__)'

# IF (eth0 / USB-Eth / wlan0 / AX210)
ip -br addr
sudo ethtool -T eth0 | grep hardware-receive   # PHC 対応確認

# PHC
ls -l /dev/ptp0

# USB serial (sniffer / reflector)
ls -l /dev/ttyUSB*

# chrony master
chronyc tracking ; sudo ss -anu | grep ':123 '

# eth0 計測準備
sudo ip link set eth0 promisc on
sudo hwstamp_ctl -i eth0 -r 1 -t 0

# 30s smoke (host PC で pc_emulator 並走)
cd ~/gtnlv && tmux new-session -d -s smoke \
  'python3 -u rpi_daemon/gtnlv_rpid.py --robot-ids 1 --duration 30 \
     --sniffer-port /dev/ttyUSB0 --sniffer-baud 2000000 --out-dir /tmp/smoke'
```

> **重要 (SSH + background)**: `ssh host "cmd &"` の background detach は SSH
> セッション終了で SIGHUP され kill される。RasPi 上で長時間 task を走らせる時は
> **必ず tmux session 内で起動** する (`docs/lessons_learned.md` 関連、本番
> overnight ラン手順も tmux 前提)。

## 10. 計測 AP への wlan0 接続 + 帯域負荷生成 (混雑試験用)

スマホ動画と同等の帯域負荷を **RasPi 自身**から生成して、AP queue 飽和時の HID downlink starvation や PPS jitter を再現性のある形で観察する。

### 11.1 wlan0 の役割切替

| ステップ | wlan0 接続先 | 用途 |
|---|---|---|
| 計測通常時 (default) | TEAM_SSID (上流、192.168.1.140) | 管理 ssh、計測経路と分離 |
| 混雑試験時 | **TEAM_SSID_OPEN** (計測 AP、192.168.4.x) | AP RF/queue を圧迫する負荷生成経路 |

LTE backhaul (§8.5) で remote access 確保済の前提。これが無いと wlan0 を計測 AP に切替えた瞬間 ssh が切れる (上流回線が消える)。

```bash
# 既存 TEAM_SSID プロファイルを残しつつ、計測 AP プロファイル追加
nmcli connection add type wifi ifname wlan0 con-name greentea-open \
  ssid TEAM_SSID_OPEN wifi-sec.key-mgmt none \
  ipv4.method auto ipv6.method ignore \
  ipv4.route-metric 600     # 計測 LAN (USB-Eth 100) より高い metric

# 切替: 計測 AP へ
nmcli connection down 'TEAM_SSID' 2>/dev/null
nmcli connection up greentea-open
ip -br addr show wlan0     # 192.168.4.x が付くこと
ip route                   # default は LTE (wwan0、metric 700) のまま、USB-Eth (100) が計測 LAN、wlan0 (600) は計測 AP 用
```

**重要**: wlan0 は計測 AP 接続中も `default` route を持たせない。計測 trafic は USB-Eth、外部接続は LTE。wlan0 はあくまで「AP queue を圧迫するための帯域生成口」。

戻すとき:
```bash
nmcli connection down greentea-open
nmcli connection up 'TEAM_SSID'
```

### 11.2 帯域負荷生成 (iperf3)

```bash
sudo apt install -y iperf3
```

**構成**:

```
[AIPC .4.160] ←─ wire ─→ [AP LN6001-JP] ←─ RF ch112 ─→ [RasPi wlan0]
                                                          ↑
                                            iperf3 -c で帯域要求
```

AIPC で iperf3 server を立て、RasPi が wlan0 経由で帯域要求 → AP の RF/queue を圧迫:

```bash
# AIPC 側 (foreground tmux 推奨)
iperf3 -s -p 5201

# RasPi 側 — 計測 AP 経由で帯域要求 (300 秒)
iperf3 -c 192.168.4.160 -p 5201 -t 300 \
  --bind-dev wlan0          # ★ wlan0 経由を強制 (route metric 衝突回避)
  # オプション:
  # -u                       # UDP モード (TCP より AP queue 圧迫が予測しやすい)
  # -b 30M                   # 上限 30 Mbps (4K YouTube 相当)
  # -P 4                     # 4 並列 stream
  # -R                       # downlink (RasPi receive、AP → RasPi)
```

### 11.3 帯域負荷下試験の進行

```bash
# 1. AIPC 側 iperf3 server (tmux)
tmux new-session -d -s iperf3srv 'iperf3 -s -p 5201'

# 2. AIPC 側 PPS Δt + pc_emulator 並走を kick off (5min)
TAG=load_rpi_iperf_$(date +%H%M)
OUT=out/m2k_pps_diff_${TAG}
mkdir -p "${OUT}"
python3 tools/m2k_pps_diff/pps_diff.py --duration 305 --out-dir "${OUT}" \
  > "${OUT}/run.stdout" 2>&1 &
PPS=$!
sleep 4
python3 tools/pc_emulator/pc_emulator.py --robot-id 0 --target 192.168.4.111 \
  --port 40000 --rate 100 --duration 300 --listen-metrics \
  > "${OUT}/pc_emulator.log" 2>&1 &
PCEM=$!

# 3. RasPi で iperf3 client を **直後に開始** (UDP 30Mbps × 4 stream)
ssh gochiuma@192.168.4.103 \
  "iperf3 -c 192.168.4.160 -p 5201 -t 300 -u -b 30M -P 4 \
     --bind-dev wlan0 > /tmp/iperf3_$TAG.log 2>&1"

wait $PCEM $PPS
```

### 11.4 観察項目

| 指標 | source | 期待 (idle) | 期待 (負荷下) |
|---|---|---|---|
| PPS Δt median / sd | `tools/m2k_pps_diff/analyze.py` | 各 §2.18 baseline | 変化大なら HID esp_timer は外部 RF 影響を受ける |
| rx_dl delivery rate | pc_emulator listen-metrics | ~95% (port 一致時) | 大幅落ち = AP downlink starve |
| HID hb dropped counter | metrics broadcast | 0 | >0 で HID ring overflow |
| iperf3 報告 BW | iperf3 log | ~AP capacity (数十-数百 Mbps) | sustained 帯域 |
| AP→HID air frame 数 | sniffer.csv (sniffer_runner) | ~sent count | 大幅減 = air drop |
| AP→broadcast 数 | sniffer.csv | beacon 程度 | ~変化なし |

### 11.5 帯域パターンの示唆

- **UDP 30 Mbps × 4 = 120 Mbps** はスマホ 4K (15-25 Mbps) より大幅に重い負荷。AP queue を確実に飽和できる
- **TCP モード (`-u` なし)** は AP の retry / queue dynamics で予測不能になりやすい。データ取得目的なら **UDP 固定 rate** が再現性高
- **`-R` (downlink)** は AIPC → AP → RasPi の方向。HID 宛 downlink と同方向の queue を取り合う = より直接的な競合実験
- 別パターン: スマホ動画と同時に iperf3 → AP 飽和を確実化

## 11. RPi OS 移行時の差分まとめ

| 項目 | Ubuntu 25.10 (現在) | Raspberry Pi OS (移行後) |
|---|---|---|
| パッケージ管理 | apt | apt (共通) |
| ネットワーク | netplan | NetworkManager (`nmcli`) or dhcpcd |
| eth0 PHC | macb `/dev/ptp0` (確認済) | **要確認** (`ethtool -T eth0`) |
| **EC25J (LTE)** | ModemManager + libqmi | 同じ (Debian 共通)、driver は kernel 標準 |
| USB-Eth driver | `ax88179_178a` (kernel 標準) | kernel 標準 (共通) |
| chrony / linuxptp | apt 同梱 | apt 同梱 (共通) |
| esptool | pipx 5.x | pipx 5.x (共通) |
| sudo NOPASSWD | `/etc/sudoers.d/` | 同じ |

→ **OS 非依存なツール (chrony, linuxptp, tcpdump, esptool, python, ModemManager)
はそのまま**。要注意は **eth0 PHC の有無** のみ。§9 のチェックリストで確認すれば
移行完了。(旧 AX210 の iwlwifi firmware は EC25J 置換で不要に。)

## 関連ドキュメント

- `docs/measurement_architecture.md` §1 (機器役割)、§8 (NTP master 化)
- `docs/phase3_findings.md` §1 (ハードウェア配線)、§2.10 (NTP master 実測)
- `docs/lessons_learned.md` §C.9 (subnet 分離)、§C.14 (promisc 必須)
