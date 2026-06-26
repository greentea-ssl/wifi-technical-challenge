#!/usr/bin/env python3
# gtnlv-rpid v0 — minimal OWD logger
#
# Inputs:
#   1. UDP 52000 + robot_id  (radio_metrics rx_dl / tx_ul / hb)
#   2. Sniffer USB-UART (binary protocol, sniffer.ino), optional
#
# Output:
#   - phase1_results/owd_dl.csv : per-packet downlink OWD
#   - phase1_results/owd_ul.csv : per-packet uplink OWD (with time-proximity pair)
#   - phase1_results/metrics_raw.csv : all received 52000+id messages
#   - phase1_results/sniffer.csv : sniffer captures (if --sniffer-port specified)
#   - phase1_results/bridge.csv  : RT↔TSF bridge fit points (sniffer broadcasts)
#
# OWD computation (v0, dev-environment friendly):
#   - For each rx_dl: owd_dl_approx = t_rpid_recv_rt − corr_unix_time
#     - t_rpid_recv_rt = host CLOCK_REALTIME when the rx_dl UDP arrived
#     - corr_unix_time = AIPC send time embedded in payload offset 38-45
#     - This approximation INCLUDES the HID rx_dl emission + WiFi broadcast trip
#       back to the rpid host. On dev (same host as AIPC) it overstates true OWD
#       by ~1-2 ms (the rx_dl trip latency).
#   - Proper computation needs t_hid_rx_tsf converted to RasPi RT via bridge
#     (sniffer broadcast cross-correlation). Implemented when sniffer port given.
#
# Usage (loopback test, no SPAN, AIPC=this host):
#   python3 gtnlv_rpid.py --robot-ids 0,1,2 --duration 60 \
#       --out-dir phase1_results
#
# Usage (with sniffer):
#   python3 gtnlv_rpid.py --robot-ids 1 --duration 60 \
#       --sniffer-port /dev/ttyUSB0 --out-dir phase1_results

import argparse
import collections
import csv
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path

try:
    import serial
except ImportError:
    serial = None

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `import store`


# Same binary protocol as tools/sniffer_runner/run.py
SYNC = bytes([0xC5, 0xC5])
TYPE_FRAME = 0x01
TYPE_HB    = 0x02
TYPE_PPS   = 0x04
LEN_FRAME_PAYLOAD = 44     # v1: Entry=42 + 2 (dropped_lo)、cycle_count 無し
LEN_FRAME_PAYLOAD_V2 = 48  # v2: Entry=46 (cycle_count uint32 付き) + 2 (dropped_lo)
LEN_FRAME_PAYLOAD_V3 = 49  # v3: Entry=47 (cycle_count + robot_id uint8) + 2 (dropped_lo)
LEN_HB_PAYLOAD    = 16
LEN_PPS_PAYLOAD   = 16  # u64 tsf_us + u64 esp_timer_us
FRAME_STRUCT = struct.Struct(
    "<I I I Q B B B b H H 6s 6s B B H"   # Q = uint64 tsf_us (v1)
)
FRAME_STRUCT_V2 = struct.Struct(
    "<I I I Q B B B b H H 6s 6s B B I H"  # ...fc_hi, cycle_count(I), dropped_lo(H) (v2)
)
FRAME_STRUCT_V3 = struct.Struct(
    "<I I I Q B B B b H H 6s 6s B B I B H"  # ...cycle_count(I), robot_id(B), dropped_lo(H) (v3)
)
CYCLE_INVALID = 0xFFFFFFFF  # sniffer が parse 失敗時に入れる sentinel
ROBOT_ID_INVALID = 0xFF     # sniffer が robot_id を取れなかった時の sentinel
HB_STRUCT = struct.Struct("<I I I i")
PPS_STRUCT = struct.Struct("<Q Q")        # tsf_us, esp_timer_us at PPS edge


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02X}" for x in b)


def now_unix() -> float:
    return time.time()


def bind_to_iface(sock, iface, port):
    """SO_BINDTODEVICE で受信 NIC を固定 (二重受信対策、Stage 3b)。
    root/CAP_NET_RAW が要る。権限が無ければ warning して素通り (best-effort)。"""
    if not iface:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode() + b"\0")
    except (OSError, PermissionError) as e:
        print(f"[bind:{port}] SO_BINDTODEVICE({iface}) 失敗: {e} "
              "(root/CAP_NET_RAW が必要 — 全 NIC 受信にフォールバック)", file=sys.stderr)


class RotatingCSVWriter:
    """Live ビュワー向け CSV writer。`keep_recent_s > 0` の時は
    in-memory deque (max N rows) に貯めて、`flush_interval` 秒ごとに file を
    完全 overwrite する。これで file size を直近 N 秒 + ε に抑え、SD card 摩耗
    と stoarge 消費を回避できる (tmpfs に書く前提)。

    `keep_recent_s == 0` の時は通常の append-only writer (overnight 用)。
    """

    def __init__(self, path: Path, header: list[str],
                 use_dict: bool = True,
                 keep_recent_s: float = 0.0,
                 expected_rate_hz: float = 100.0,
                 flush_interval_s: float = 1.0):
        self.path = path
        self.header = header
        self.use_dict = use_dict
        self.keep_recent_s = keep_recent_s
        self.flush_interval_s = flush_interval_s
        self.lock = threading.Lock()
        self.f = open(path, "w", newline="", buffering=1)
        if use_dict:
            self.w = csv.DictWriter(self.f, fieldnames=header, extrasaction="ignore")
            self.w.writeheader()
        else:
            self.w = csv.writer(self.f)
            self.w.writerow(header)
        self.f.flush()
        if keep_recent_s > 0:
            max_n = max(200, int(keep_recent_s * expected_rate_hz * 1.5))
            self.buffer = collections.deque(maxlen=max_n)
            self.last_flush = time.time()
        else:
            self.buffer = None

    def write_row(self, row):
        """row: dict (use_dict=True) or list (use_dict=False)"""
        with self.lock:
            if self.buffer is None:
                if self.use_dict:
                    self.w.writerow(row)
                else:
                    self.w.writerow(row)
                return
            self.buffer.append(row)
            now = time.time()
            if now - self.last_flush >= self.flush_interval_s:
                self._rewrite_locked()
                self.last_flush = now

    def _rewrite_locked(self):
        """deque の現在の中身で file 全体を上書き (header から)。lock 保持前提。"""
        self.f.seek(0)
        self.f.truncate()
        if self.use_dict:
            self.w = csv.DictWriter(self.f, fieldnames=self.header, extrasaction="ignore")
            self.w.writeheader()
            for r in self.buffer:
                self.w.writerow(r)
        else:
            self.w = csv.writer(self.f)
            self.w.writerow(self.header)
            for r in self.buffer:
                self.w.writerow(r)
        self.f.flush()

    def close(self):
        with self.lock:
            if self.buffer is not None:
                self._rewrite_locked()
            self.f.close()


class UplinkListener(threading.Thread):
    """Listen on UDP 50000+robot_id for production uplink JSON. Just timestamp arrival."""

    def __init__(self, robot_id, stop_evt, raw_writer, iface=None):
        super().__init__(daemon=True)
        self.robot_id = robot_id
        self.port = 50000 + robot_id
        self.stop_evt = stop_evt
        self.raw_writer = raw_writer
        self.iface = iface
        self.n = 0

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        bind_to_iface(sock, self.iface, self.port)
        try:
            sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            print(f"[uplink:{self.port}] bind 失敗: {e}", file=sys.stderr)
            return
        sock.settimeout(0.5)
        print(f"[uplink:{self.port}] 待受開始 (robot_id={self.robot_id})", file=sys.stderr)
        while not self.stop_evt.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            t_recv = now_unix()
            self.n += 1
            self.raw_writer(self.robot_id, addr[0], t_recv, len(data))
        sock.close()
        print(f"[uplink:{self.port}] 停止 n={self.n}", file=sys.stderr)


class MetricsListener(threading.Thread):
    """Listen on UDP 52000+robot_id, dispatch rx_dl / tx_ul / hb to CSVs and OWD logic."""

    def __init__(self, robot_id, stop_evt, raw_writer, owd_dl_writer, tx_ul_records, iface=None):
        super().__init__(daemon=True)
        self.robot_id = robot_id
        self.port = 52000 + robot_id
        self.stop_evt = stop_evt
        self.raw_writer = raw_writer
        self.owd_dl_writer = owd_dl_writer
        self.tx_ul_records = tx_ul_records  # shared list for tx_ul pairing
        self.iface = iface
        self.n_rx_dl = 0
        self.n_tx_ul = 0
        self.n_hb = 0
        self.n_rx_dlb = 0          # 受信した rx_dlb (batch) フレーム数
        self.n_batch_lost = 0      # bseq 連番抜けから推定した欠落 batch 数
        self._last_bseq = None     # batch 損失検出用 (直前 bseq)

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        bind_to_iface(sock, self.iface, self.port)
        try:
            sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            print(f"[metrics:{self.port}] bind 失敗: {e}", file=sys.stderr)
            return
        sock.settimeout(0.5)
        print(f"[metrics:{self.port}] 待受開始 (robot_id={self.robot_id})", file=sys.stderr)
        while not self.stop_evt.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            t_recv = now_unix()
            for line in data.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "rx_dl":
                    self.n_rx_dl += 1
                    self._handle_rx_dl(msg, addr, t_recv)
                elif mtype == "rx_dlb":
                    self.n_rx_dlb += 1
                    self._handle_rx_dlb(msg, addr, t_recv)
                elif mtype == "tx_ul":
                    self.n_tx_ul += 1
                    self._handle_tx_ul(msg, addr, t_recv)
                elif mtype == "hb":
                    self.n_hb += 1
                self.raw_writer(self.robot_id, addr[0], t_recv, msg)
        sock.close()
        print(f"[metrics:{self.port}] 停止 (rx_dl={self.n_rx_dl} rx_dlb={self.n_rx_dlb}"
              f" batch_lost={self.n_batch_lost} tx_ul={self.n_tx_ul} hb={self.n_hb})",
              file=sys.stderr)

    def _handle_rx_dl(self, msg, addr, t_recv):
        corr_unix_time = msg.get("corr_unix_time")
        if corr_unix_time is None:
            return
        owd_dl_approx_us = (t_recv - corr_unix_time) * 1e6
        self.owd_dl_writer({
            "robot_id": self.robot_id,
            "hid_ip": addr[0],
            "hid_seq": msg.get("hid_seq"),
            "dl_seq": msg.get("dl_seq"),
            "aipc_seq": msg.get("aipc_seq"),
            "cycle_count": msg.get("cycle_count"),
            "corr_unix_time": corr_unix_time,
            "t_rpid_recv_unix": t_recv,
            "t_hid_rx_tsf_us": msg.get("t_rx_tsf_us"),
            "t_hid_rx_esp_us": msg.get("t_rx_esp_timer_us"),
            # spec v2.1.0: rx_dl 自身を broadcast する直前の送信アンカー (上り OWD HID→air leg 用)
            "t_hid_tx_tsf_us": msg.get("t_tx_tsf_us"),
            "t_hid_tx_esp_us": msg.get("t_tx_esp_timer_us"),
            "frame_size": msg.get("frame_size"),
            "rssi": msg.get("rssi"),   # spec v2.1.0: HID が下り受信時に読んだ WiFi.RSSI (dBm)
            "owd_dl_approx_us": owd_dl_approx_us,
        })

    def _handle_rx_dlb(self, msg, addr, t_recv):
        # rx_dlb (radio_metrics.md §3.1.1、type 0x04): 複数 rx_dl レコードの batch。
        # recs[i] = [cycle_count, t_rx_tsf−base(µs), rssi] を従来 rx_dl 行に展開する。
        # 下りは per-record で完全保持 (t_rx_tsf は受信時刻なので batch 遅延 ≤0.5s は OWD 非影響)。
        # 上り HID→air アンカー tx は batch 単位 (全 record 共通、~2Hz/台 に間引き)。
        bseq = msg.get("bseq")
        base = msg.get("base")
        tx   = msg.get("tx")
        recs = msg.get("recs")
        if base is None or not isinstance(recs, list):
            return
        # batch (UDP) 損失検出: bseq 連番抜け → その区間の下り損失計算から除外できるよう記録。
        # broadcast 無再送で batch を 1 個落とすと ~K 件が一度に欠落する (報告損失 ≠ 真の下り損失)。
        if bseq is not None and self._last_bseq is not None:
            gap = (bseq - self._last_bseq - 1) & 0xFFFFFFFF
            if 0 < gap < 1000:   # 妥当な抜けのみ計上 (起動/再起動の wrap は無視)
                self.n_batch_lost += gap
        if bseq is not None:
            self._last_bseq = bseq
        for rec in recs:
            if not isinstance(rec, (list, tuple)) or len(rec) < 3:
                continue
            cycle, dt, rssi = rec[0], rec[1], rec[2]
            t_rx_tsf = (base + dt) if (dt is not None) else None
            self.owd_dl_writer({
                "robot_id": self.robot_id,
                "hid_ip": addr[0],
                "hid_seq": msg.get("hid_seq"),   # meta = batch フレーム seq
                "dl_seq": None,
                "aipc_seq": None,
                "cycle_count": cycle,
                "corr_unix_time": None,          # batch は corr_unix_time を持たない (§3.1.1)
                "t_rpid_recv_unix": t_recv,      # batch 到着時刻 (鮮度のみ、OWD には未使用)
                "t_hid_rx_tsf_us": t_rx_tsf,     # base+delta で復元した下り受信 TSF (OWD キー)
                "t_hid_rx_esp_us": None,
                "t_hid_tx_tsf_us": tx,           # batch 送信アンカー (上り HID→air leg、batch 単位)
                "t_hid_tx_esp_us": None,
                "frame_size": None,
                "rssi": rssi,
                "owd_dl_approx_us": None,        # corr_unix_time 無しのため近似 OWD は出さない
                "batch_seq": bseq,               # batch 連番 (損失分離キー)
                "batch_rxc": msg.get("rxc"),     # 累積 rx_dl 受信数 (冗長チェック)
            })
            self.n_rx_dl += 1   # 進捗/健全性カウンタを per-record で進める (watchdog 整合)

    def _handle_tx_ul(self, msg, addr, t_recv):
        rec = {
            "robot_id": self.robot_id,
            "hid_ip": addr[0],
            "hid_seq": msg.get("hid_seq"),
            "ul_seq": msg.get("ul_seq"),
            "tx_port": msg.get("tx_port"),
            "t_hid_tx_tsf_us": msg.get("t_tx_tsf_us"),
            "t_rpid_recv_unix": t_recv,
            "frame_size": msg.get("frame_size"),
        }
        if isinstance(self.tx_ul_records, tuple):
            lock, writer = self.tx_ul_records
            with lock:
                writer({
                    "robot_id": rec["robot_id"], "hid_ip": rec["hid_ip"],
                    "hid_seq": rec["hid_seq"], "ul_seq": rec["ul_seq"],
                    "tx_port": rec["tx_port"],
                    "t_hid_tx_tsf_us": rec["t_hid_tx_tsf_us"],
                    "t_rpid_recv_unix": f"{rec['t_rpid_recv_unix']:.6f}",
                    "frame_size": rec["frame_size"],
                })
        else:
            self.tx_ul_records.append(rec)


class SnifferReader(threading.Thread):
    """Read binary records from sniffer.ino over UART (frame / hb / pps)."""

    def __init__(self, port, baud, stop_evt, frame_writer, hb_writer, pps_uart_writer=None,
                 auto_recover=True, recover_silence_s=8.0, recover_cooldown_s=20.0):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.stop_evt = stop_evt
        self.frame_writer = frame_writer
        self.hb_writer = hb_writer
        self.pps_uart_writer = pps_uart_writer
        self.n_pps = 0
        self.n_records = 0   # watchdog 用 (frame/hb/pps 全 record の通し数)
        self._cmd_lock = threading.Lock()
        self._cmd_queue = []  # host→sniffer に送る生バイト列 (run thread で write)
        # 自動復旧: sniffer firmware は持続高負荷や ~CFG 再associate で稀に hang する
        # (frame/hb/pps が全停止)。watchdog は検知のみで復旧できないため、
        # この thread 自身が serial の RTS をパルスして C5 を reboot する
        # (reboot 後は NVS 保存 SSID で自動的に capture 再開)。
        self.auto_recover = auto_recover
        self.recover_silence_s = recover_silence_s    # 無音がこの秒数続いたら reboot
        self.recover_cooldown_s = recover_cooldown_s  # 連続 reboot 抑止
        self.n_recover = 0
        self._force_recover = False   # ctrl thread からの強制 RTS reboot 要求

    def send_cfg(self, ssid: str, password: str = "") -> None:
        """sniffer の対象 AP を切替える UART コマンドを enqueue (run thread が write)。"""
        line = ("~CFG " + ssid + "\t" + (password or "") + "\n").encode("utf-8")
        with self._cmd_lock:
            self._cmd_queue.append(line)

    def request_recover(self) -> None:
        """ctrl thread 等から RTS reboot を強制要求 (実際の RTS は run thread が実行)。
        起動時 hang (n_records==0) で watchdog が発火しないケースの明示復旧用。"""
        with self._cmd_lock:
            self._force_recover = True

    def is_progressing(self, probe_s: float = 1.0) -> bool:
        """probe_s 秒で n_records が進むか = sniffer UART が生きているか (生存確認)。
        ctrl thread から呼ぶ (hot path ではない)。frame/hb/pps いずれかが来れば True。"""
        a = self.n_records
        time.sleep(probe_s)
        return self.n_records > a

    def run(self):
        if serial is None:
            print("[sniffer] pyserial 未 install", file=sys.stderr)
            return
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.5)
        except OSError as e:
            print(f"[sniffer] open 失敗: {e}", file=sys.stderr)
            return
        print(f"[sniffer] 読取開始 {self.port} @ {self.baud}", file=sys.stderr)
        buf = bytearray()
        n_frame = 0
        n_hb = 0
        last_progress_t = now_unix()   # 最後に record が来た時刻 (自動復旧用)
        last_recover_t = 0.0
        prev_records = 0
        while not self.stop_evt.is_set():
            # --- 自動復旧: 無音化 or 強制要求で RTS パルス reboot ---
            if self.auto_recover:
                with self._cmd_lock:
                    forced = self._force_recover
                    self._force_recover = False
                if self.n_records > prev_records:
                    prev_records = self.n_records
                    last_progress_t = now_unix()
                silent = now_unix() - last_progress_t
                cooled = now_unix() - last_recover_t >= self.recover_cooldown_s
                # n_records==0 の起動時 hang も対象 (旧実装は n_records>0 を要求し発火せず)。
                # 強制要求 (forced) は cooldown を無視して即 reboot。
                if forced or (silent >= self.recover_silence_s and cooled):
                    self.n_recover += 1
                    why = "強制要求" if forced else f"{silent:.0f}s 無音"
                    print(f"[sniffer] ⚠ {why} → RTS パルスで reboot (#{self.n_recover})",
                          file=sys.stderr)
                    try:
                        ser.setDTR(False); ser.setRTS(True)
                        time.sleep(0.15); ser.setRTS(False)
                    except OSError as e:
                        print(f"[sniffer] RTS reboot 失敗: {e}", file=sys.stderr)
                    buf.clear()
                    last_recover_t = now_unix()
                    last_progress_t = now_unix()  # boot 猶予
            # host→sniffer コマンド (対象 AP 切替) を write (この thread が serial を所有)
            if self._cmd_queue:
                with self._cmd_lock:
                    pending = self._cmd_queue
                    self._cmd_queue = []
                for line in pending:
                    try:
                        ser.write(line)
                        print(f"[sniffer] CFG 送信: {line.decode('utf-8', 'replace').strip()}",
                              file=sys.stderr)
                    except OSError as e:
                        print(f"[sniffer] CFG 送信失敗: {e}", file=sys.stderr)
            # in_waiting ベースの即時 read。read(4096)+timeout だと 0.5s 単位の
            # batch になり、chunk 内 frame の t_rpid_recv が read 完了時刻に量子化
            # されて transport delay が 0-500ms に見えていた (物理 transport は速い)。
            navail = ser.in_waiting
            chunk = ser.read(navail if navail > 0 else 1)
            if chunk:
                buf.extend(chunk)
            while True:
                idx = buf.find(SYNC)
                if idx < 0:
                    if len(buf) > 1:
                        del buf[:-1]
                    break
                if idx > 0:
                    # garbage / boot text
                    for line in bytes(buf[:idx]).decode("utf-8", errors="replace").splitlines():
                        if line.startswith("#"):
                            print(f"[sniffer-fw] {line}", file=sys.stderr)
                    del buf[:idx]
                if len(buf) < 4:
                    break
                rtype = buf[2]
                rlen = buf[3]
                total = 4 + rlen
                if rtype == TYPE_FRAME and rlen not in (LEN_FRAME_PAYLOAD, LEN_FRAME_PAYLOAD_V2, LEN_FRAME_PAYLOAD_V3):
                    del buf[:2]; continue
                if rtype == TYPE_HB and rlen != LEN_HB_PAYLOAD:
                    del buf[:2]; continue
                if rtype == TYPE_PPS and rlen != LEN_PPS_PAYLOAD:
                    del buf[:2]; continue
                if rtype not in (TYPE_FRAME, TYPE_HB, TYPE_PPS):
                    del buf[:2]; continue
                if len(buf) < total:
                    break
                payload = bytes(buf[4:total])
                del buf[:total]
                t_recv = now_unix()
                self.n_records += 1   # watchdog 進捗
                if rtype == TYPE_FRAME:
                    robot_id = None
                    if rlen == LEN_FRAME_PAYLOAD_V3:
                        (rx_seq, t_local_us_lo, rx_timestamp_us, tsf_us,
                         bb_format, rate, channel, rssi, sig_len, hdr_seq,
                         src, dst, fc_lo, fc_hi, cyc_raw, rid_raw, dropped_lo) = FRAME_STRUCT_V3.unpack(payload)
                        cycle_count = None if cyc_raw == CYCLE_INVALID else cyc_raw
                        robot_id = None if rid_raw == ROBOT_ID_INVALID else rid_raw
                    elif rlen == LEN_FRAME_PAYLOAD_V2:
                        (rx_seq, t_local_us_lo, rx_timestamp_us, tsf_us,
                         bb_format, rate, channel, rssi, sig_len, hdr_seq,
                         src, dst, fc_lo, fc_hi, cyc_raw, dropped_lo) = FRAME_STRUCT_V2.unpack(payload)
                        cycle_count = None if cyc_raw == CYCLE_INVALID else cyc_raw
                    else:
                        (rx_seq, t_local_us_lo, rx_timestamp_us, tsf_us,
                         bb_format, rate, channel, rssi, sig_len, hdr_seq,
                         src, dst, fc_lo, fc_hi, dropped_lo) = FRAME_STRUCT.unpack(payload)
                        cycle_count = None
                    self.frame_writer({
                        "t_rpid_recv_unix": t_recv,
                        "rx_seq": rx_seq,
                        "t_local_us_lo": t_local_us_lo,
                        "rx_timestamp_us": rx_timestamp_us,
                        "tsf_us": tsf_us,
                        "bb_format": bb_format,
                        "rate": rate,
                        "channel": channel,
                        "rssi": rssi,
                        "sig_len": sig_len,
                        "hdr_seq": hdr_seq,
                        "src": mac_str(src),
                        "dst": mac_str(dst),
                        "fc_lo": fc_lo,
                        "fc_hi": fc_hi,
                        "cycle_count": cycle_count,
                        "robot_id": robot_id,
                        "dropped_lo": dropped_lo,
                    })
                    n_frame += 1
                elif rtype == TYPE_HB:
                    cap, drop, t_now_lo, rssi_now = HB_STRUCT.unpack(payload)
                    self.hb_writer({
                        "t_rpid_recv_unix": t_recv,
                        "captured_total": cap,
                        "dropped_total": drop,
                        "t_now_us_lo": t_now_lo,
                        "rssi_now": rssi_now,
                    })
                    n_hb += 1
                elif rtype == TYPE_PPS:
                    tsf_us, esp_us = PPS_STRUCT.unpack(payload)
                    if self.pps_uart_writer is not None:
                        self.pps_uart_writer({
                            "t_rpid_recv_unix": t_recv,
                            "tsf_us": tsf_us,
                            "esp_us": esp_us,
                        })
                    self.n_pps += 1
        ser.close()
        print(f"[sniffer] 停止 frames={n_frame} hb={n_hb} pps={self.n_pps}", file=sys.stderr)


class PpsGpioReader(threading.Thread):
    """Read /dev/pps* assert events via ppstest subprocess, write to CSV.

    Uses ppstest (pps-tools) to avoid Python ioctl binding for RFC 2783.
    Each stdout line:
        source 0 - assert 1779971074.250828948, sequence: 1752 - clear ...
    """

    def __init__(self, device, stop_evt, gpio_writer):
        super().__init__(daemon=True)
        self.device = device
        self.stop_evt = stop_evt
        self.gpio_writer = gpio_writer
        self.n = 0

    def run(self):
        import re, subprocess, shutil
        if shutil.which("ppstest") is None:
            print("[pps-gpio] ppstest が見つかりません (apt install pps-tools)", file=sys.stderr)
            return
        # sudo は環境次第。NOPASSWD 前提で sudo 経由
        cmd = ["sudo", "-n", "ppstest", self.device]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 bufsize=1, text=True)
        except OSError as e:
            print(f"[pps-gpio] 起動失敗: {e}", file=sys.stderr)
            return
        print(f"[pps-gpio] 読取開始 {self.device}", file=sys.stderr)
        pat = re.compile(r"assert\s+(\d+)\.(\d+),\s+sequence:\s+(\d+)")
        try:
            while not self.stop_evt.is_set():
                line = p.stdout.readline()
                if not line:
                    break
                m = pat.search(line)
                if not m:
                    continue
                sec = int(m.group(1))
                nsec = int(m.group(2))
                seq = int(m.group(3))
                unix_assert = sec + nsec / 1e9
                self.gpio_writer({
                    "unix_assert": unix_assert,
                    "sequence": seq,
                })
                self.n += 1
        finally:
            try:
                p.terminate(); p.wait(timeout=2)
            except Exception:
                p.kill()
        print(f"[pps-gpio] 停止 events={self.n}", file=sys.stderr)


class WireReader(threading.Thread):
    """eth0 SPAN mirror で AIPC→AP downlink frame を AF_PACKET 捕捉し、
    SO_TIMESTAMPING の software RX 時刻 (CLOCK_REALTIME) を wire 到達時刻とする。
    payload から robot_id(offset2) / t_tx_unix(38-45) / cycle_count(51-53) を抽出し
    `wire` table へ記録 → 3 区間化 (host+有線 / 有線→air=AP queue) に使う。
    AF_PACKET は root/CAP_NET_RAW 必須。"""

    _SO_TIMESTAMPING = 37
    _FLAGS = (1 << 2) | (1 << 3) | (1 << 4) | (1 << 6)  # RX_HW|RX_SW|SW|RAW_HW
    _ETH_P_IP = 0x0800

    def __init__(self, iface, robot_ids, stop_evt, wire_writer):
        super().__init__(daemon=True)
        self.iface = iface
        # DL (40000+id, unicast→HID) / UL production (50000+id) / radio_metrics
        # (52000+id, hb/tx_ul/rx_dl、meta 付き) を取得。UL/metrics は dst_ip が subnet
        # broadcast (.255) になるので datasource 側で区別する。
        self.dst_ports = (set(40000 + int(r) for r in robot_ids)
                          | set(50000 + int(r) for r in robot_ids)
                          | set(52000 + int(r) for r in robot_ids))
        self.stop_evt = stop_evt
        self.wire_writer = wire_writer
        self.n = 0

    @staticmethod
    def _sw_unix(ancdata):
        for level, type_, cdata in ancdata:
            if level == 1 and type_ == 37 and len(cdata) >= 16:
                sec, nsec = struct.unpack("qq", cdata[:16])  # sw timespec が先頭
                if sec or nsec:
                    return sec + nsec / 1e9
        return None

    def run(self):
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(self._ETH_P_IP))
            s.bind((self.iface, 0))
            # SPAN destination の eth0 を promisc に。無いとカーネルが自MAC宛以外を破棄し、
            # broadcast(上り 52000→.255)しか取れず、下り unicast(40000→robot MAC)を取りこぼす。
            # PACKET_MR_PROMISC は socket close で自動解除されるので ip link の状態を汚さない。
            try:
                ifindex = socket.if_nametoindex(self.iface)
                mreq = struct.pack("IHH8s", ifindex, 1, 0, b"")  # PACKET_MR_PROMISC=1
                s.setsockopt(263, 1, mreq)                        # SOL_PACKET=263, ADD_MEMBERSHIP=1
            except OSError as e:
                print(f"[wire] promisc 設定失敗(下り取りこぼす可能性): {e}", file=sys.stderr)
            s.setsockopt(socket.SOL_SOCKET, self._SO_TIMESTAMPING, self._FLAGS)
            s.settimeout(0.5)
        except (OSError, PermissionError) as e:
            print(f"[wire] open 失敗 {self.iface}: {e} (root/CAP_NET_RAW 必要)", file=sys.stderr)
            return
        print(f"[wire] 読取開始 {self.iface} dst_ports={sorted(self.dst_ports)}", file=sys.stderr)
        while not self.stop_evt.is_set():
            try:
                data, ancdata, _flags, _addr = s.recvmsg(2048, 256)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 20:
                continue
            # Ethernet header skip (macb driver は IP 先頭の場合あり)
            if data[0] != 0x45 and len(data) >= 14:
                if struct.unpack("!H", data[12:14])[0] != self._ETH_P_IP:
                    continue
                ip = data[14:]
            else:
                ip = data
            if len(ip) < 28:
                continue
            ihl = (ip[0] & 0x0F) * 4
            if (ip[0] >> 4) != 4 or ihl < 20 or len(ip) < ihl + 8 or ip[9] != 17:
                continue
            src_ip = socket.inet_ntoa(ip[12:16])
            dst_ip = socket.inet_ntoa(ip[16:20])
            src_port, dst_port, ulen, _c = struct.unpack("!HHHH", ip[ihl:ihl + 8])
            if dst_port not in self.dst_ports:
                continue
            payload = ip[ihl + 8:ihl + ulen]
            # radio_metrics frame (52000+id) は先頭が {"meta":"RM..(hex)..."}。
            # meta (payload offset 9〜、ASCII hex、radio_metrics.md §3.0) から hid_seq を
            # 取り出し、上り/hb の air/wire join key として cycle_count 列に格納する。
            # それ以外 (DL command 等) は従来どおり offset 51-53 の binary cycle_count。
            if (len(payload) >= 27 and payload[0:9] == b'{"meta":"'
                    and payload[9:13] == b'524D'):
                try:
                    cycle_count = int(payload[19:27].decode("ascii", "replace"), 16)
                except ValueError:
                    continue
                robot_id = (dst_port - 52000) & 0x0F
                t_tx_unix = 0.0
            else:
                # meta 無しは DL command (40000+id) のみを binary parse する。
                # DL command は先頭 0xFF 0xC3 (downlink_command.md HEADER_1/2)。これを検証しないと
                # 50000/52000 の非 meta payload (CU 上り JSON 等) を DL と誤 parse し wire に
                # garbage cycle_count/robot_id/t_tx を注入してしまう (join 破壊)。
                if len(payload) < 54 or payload[0] != 0xFF or payload[1] != 0xC3:
                    continue
                robot_id = payload[2] & 0x0F
                try:
                    t_tx_unix = struct.unpack_from("<d", payload, 38)[0]
                except struct.error:
                    continue
                cycle_count = payload[51] | (payload[52] << 8) | (payload[53] << 16)
            t_wire = self._sw_unix(ancdata)
            if t_wire is None:
                t_wire = now_unix()
            self.wire_writer({
                "cycle_count": cycle_count, "robot_id": robot_id,
                "t_tx_unix": t_tx_unix, "t_wire_phc": t_wire,
                "src": src_ip, "dst": dst_ip, "frame_size": len(payload),
            })
            self.n += 1
        s.close()
        print(f"[wire] 停止 n={self.n}", file=sys.stderr)


class WatchdogThread(threading.Thread):
    """収集 thread の進捗カウンタを監視し、**一度動き出した後に無音化**したら
    warning を出す (Stage 3a)。観測専用 (hot path には触れない)。
    SnifferReader hang (file mtime は進むが内容が止まる) のような事象を検知する。

    checks: list of (name, getter() -> int(monotonic counter), timeout_s)。
    """

    def __init__(self, checks, stop_evt, poll_s=2.0):
        super().__init__(daemon=True)
        self.checks = checks
        self.stop_evt = stop_evt
        self.poll_s = poll_s

    def run(self):
        now = time.time()
        # 各 check の (前回値, 最後に増加した時刻, 一度でも動いたか, 警告済み)
        state = {name: [getter(), now, False, False] for name, getter, _ in self.checks}
        while not self.stop_evt.wait(self.poll_s):
            now = time.time()
            for name, getter, timeout_s in self.checks:
                try:
                    cur = getter()
                except Exception:
                    continue
                st = state[name]
                if cur != st[0]:
                    st[0] = cur
                    st[1] = now
                    st[2] = True       # 一度動いた
                    if st[3]:          # 復帰
                        print(f"[watchdog] {name} 復帰 (count={cur})", file=sys.stderr)
                        st[3] = False
                elif st[2] and not st[3] and (now - st[1]) > timeout_s:
                    print(f"[watchdog] ⚠ {name} が {now - st[1]:.0f}s 無音 "
                          f"(count={cur} で停止)。thread hang / 接続断の可能性", file=sys.stderr)
                    st[3] = True
        # 終了時サマリ
        for name, getter, _ in self.checks:
            try:
                print(f"[watchdog] {name} final count={getter()}", file=sys.stderr)
            except Exception:
                pass


class ControlServer(threading.Thread):
    """unix socket で録画制御 (start_record / stop_record / status) を受ける (Stage 4)。
    1 行 JSON request → 1 行 JSON response。capture hot path とは別 thread。"""

    def __init__(self, sock_path, recorder, stop_evt, sniffer=None):
        super().__init__(daemon=True)
        self.sock_path = sock_path
        self.recorder = recorder
        self.stop_evt = stop_evt
        self.sniffer = sniffer   # SnifferReader (対象 AP 切替コマンド送出用)

    def run(self):
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(self.sock_path)
            try:
                os.chmod(self.sock_path, 0o666)  # daemon=root / dashboard=一般ユーザ が接続できるよう
            except OSError:
                pass
            srv.listen(4)
            srv.settimeout(0.5)
        except OSError as e:
            print(f"[ctrl] socket bind 失敗 {self.sock_path}: {e}", file=sys.stderr)
            return
        print(f"[ctrl] 録画制御 socket 待受開始: {self.sock_path}", file=sys.stderr)
        while not self.stop_evt.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(1.0)
                buf = b""
                while b"\n" not in buf and len(buf) < 8192:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                resp = self._dispatch(buf)
                conn.sendall((json.dumps(resp) + "\n").encode())
            except (OSError, ValueError):
                pass
            finally:
                conn.close()
        try:
            srv.close()
            os.unlink(self.sock_path)
        except OSError:
            pass
        print("[ctrl] 停止", file=sys.stderr)

    def _ensure_sniffer_ready(self, recover_wait_s: float = 12.0) -> dict:
        """録画開始前の sniffer 健全性チェック。n_records が進まなければ (UART hang)
        RTS reboot を強制要求し、回復するまで待つ。結果を dict で返す。
        state: absent(未起動) / ok(健全) / recovered(復旧成功) / failed(復旧せず)。"""
        sn = self.sniffer
        if sn is None:
            return {"state": "absent", "ok": True}
        if sn.is_progressing(1.0):
            return {"state": "ok", "ok": True, "n_records": sn.n_records, "n_recover": sn.n_recover}
        # hang 検知 → 強制 reboot し回復待ち
        print("[ctrl] sniffer 無音検知 → RTS reboot 要求", file=sys.stderr)
        sn.request_recover()
        deadline = now_unix() + recover_wait_s
        while now_unix() < deadline:
            if sn.is_progressing(1.0):
                print(f"[ctrl] sniffer 復旧 (n_records={sn.n_records})", file=sys.stderr)
                return {"state": "recovered", "ok": True, "n_records": sn.n_records,
                        "n_recover": sn.n_recover}
        print("[ctrl] ⚠ sniffer 復旧せず — air/PPS データ欠落の可能性", file=sys.stderr)
        return {"state": "failed", "ok": False, "n_records": sn.n_records,
                "n_recover": sn.n_recover,
                "warn": "sniffer UART 無音のまま — air 区間分解/PPS bridge が欠落する可能性"}

    def _dispatch(self, buf: bytes) -> dict:
        try:
            req = json.loads(buf.decode().splitlines()[0])
        except (ValueError, IndexError):
            return {"ok": False, "error": "invalid json"}
        cmd = req.get("cmd")
        try:
            if cmd == "start_record":
                # 録画開始前に sniffer 生存を確認し、hang なら RTS で復旧 (air/PPS 欠落防止)
                sniffer_status = self._ensure_sniffer_ready()
                r = self.recorder.start_record(req.get("tag"))
                print(f"[ctrl] 録画開始 tag={r.get('tag')} preroll={r.get('preroll_rows')} 行 "
                      f"sniffer={sniffer_status.get('state')}", file=sys.stderr)
                return {"ok": True, **r, "sniffer": sniffer_status}
            if cmd == "stop_record":
                r = self.recorder.stop_record()
                print(f"[ctrl] 録画停止 tag={r.get('tag')}", file=sys.stderr)
                return {"ok": True, **r}
            if cmd == "status":
                return {"ok": True, **self.recorder.status()}
            if cmd == "sniffer_cfg":
                if self.sniffer is None:
                    return {"ok": False, "error": "sniffer 未起動 (--sniffer-port 無し)"}
                ssid = (req.get("ssid") or "").strip()
                if not ssid:
                    return {"ok": False, "error": "ssid 必須"}
                self.sniffer.send_cfg(ssid, req.get("password") or "")
                print(f"[ctrl] sniffer 対象 AP 切替要求: ssid={ssid}", file=sys.stderr)
                return {"ok": True, "ssid": ssid}
        except Exception as e:  # 制御 thread は capture を巻き込まない
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"unknown cmd: {cmd}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robot-ids", default="1",
                    help="comma-separated robot_ids to listen on (default '1')")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--sniffer-port", default=None,
                    help="optional sniffer USB-UART port (e.g. /dev/ttyUSB0)")
    ap.add_argument("--sniffer-baud", type=int, default=2000000)
    ap.add_argument("--pps-device", default=None,
                    help="optional /dev/pps* for sniffer GPIO PPS (e.g. /dev/pps0、"
                         "boot 順で番号変動するため `cat /sys/class/pps/*/name` で要確認)")
    ap.add_argument("--out-dir", default="phase1_results")
    ap.add_argument("--keep-recent-s", type=float, default=0.0,
                    help="(legacy Live viewer mode) >0 で各 CSV を in-memory deque で N 秒分のみ保持、"
                         "1秒ごとに file overwrite。tmpfs (/dev/shm/...) と組合せて SD 消費ゼロ。"
                         "0 (default) は通常の append-only (overnight 用)")
    # ---- v2 Recorder (store.py) flags ----
    ap.add_argument("--live", action="store_true",
                    help="(v2) SQLite live store (tmpfs) を有効化。WebUI/analyzer が SELECT で読む。"
                         "--keep-recent-s/--out-dir の legacy CSV 経路を bypass")
    ap.add_argument("--live-keep-s", type=float, default=300.0,
                    help="(v2) live ring の保持秒 (default 300)")
    ap.add_argument("--live-db", default="/dev/shm/gtnlv_live.db",
                    help="(v2) live SQLite path (tmpfs)")
    ap.add_argument("--record", default=None,
                    help="(v2) 永続記録 CSV の出力 dir。指定時のみ record sink 有効 (提出データ)")
    ap.add_argument("--iface", default=None,
                    help="(v2) 計測 UDP socket を SO_BINDTODEVICE で固定する NIC (例 eth1)。"
                         "二重受信対策、root/CAP_NET_RAW 必須")
    ap.add_argument("--ctrl-sock", default=None,
                    help="(v2 Stage4) 録画制御 unix socket path。指定時のみ制御 thread 起動 "
                         "(start_record/stop_record/status)")
    ap.add_argument("--record-base", default="runs",
                    help="(v2 Stage4) UI 起点録画の on-demand 出力先の親 dir (default runs)")
    ap.add_argument("--record-format", choices=["csv", "parquet"], default="csv",
                    help="録画形式。parquet=外部化用 (zstd圧縮~1/5、DuckDB直読、SD非消費)。default csv")
    ap.add_argument("--record-spool", default="/dev/shm/gtnlv_spool",
                    help="parquet 録画の tmpfs spool dir。完成セグメントを --record 先(NAS等)へ move (default /dev/shm/gtnlv_spool)")
    ap.add_argument("--run-note", default=None,
                    help="測定条件の自由記述 (run_meta.json の conditions_note へ。例 'home 4hub, AP=LN6001 ch40, 6robot idle')")
    ap.add_argument("--no-watchdog", action="store_true",
                    help="(v2 Stage3a) 収集 thread の無音検出 watchdog を無効化")
    ap.add_argument("--no-sniffer-auto-recover", action="store_true",
                    help="sniffer hang 時の RTS パルス自動 reboot を無効化 (既定: 有効)")
    ap.add_argument("--wire", default=None,
                    help="(v2 3区間) eth0 SPAN mirror NIC。AF_PACKET で AIPC→AP downlink を捕捉し "
                         "wire 到達時刻を記録 (root 必須)。host+有線 / 有線→air(AP queue) 分離用")
    args = ap.parse_args()

    ids = [int(x) for x in args.robot_ids.split(",") if x.strip()]
    use_recorder = bool(args.live or args.record)
    out_dir = Path(args.out_dir)
    if not use_recorder:
        out_dir.mkdir(parents=True, exist_ok=True)

    stop_evt = threading.Event()
    def handler(sig, frame):
        stop_evt.set()
    signal.signal(signal.SIGINT, handler)

    # ---------- Output writers: Recorder (v2 sink) or RotatingCSVWriter (legacy) ----------
    keep_s = args.keep_recent_s
    pps_uart_records = []   # PPS bridge join 用 (両モードで populate)
    pps_gpio_records = []
    recorder = None
    legacy_writers = []

    if use_recorder:
        import store
        live_sink = None
        if args.live:
            Path(args.live_db).parent.mkdir(parents=True, exist_ok=True)
            live_sink = store.LiveSink(args.live_db, keep_s=args.live_keep_s)
        recorder = store.Recorder(live_sink, None, record_base=args.record_base,
                                   record_format=args.record_format, record_spool=args.record_spool)
        record_sink = recorder.make_record_sink(args.record, enabled=True) if args.record else None
        recorder.record = record_sink
        recorder._recording = record_sink is not None
        print(f"[rpid] recorder: live={'on('+args.live_db+')' if args.live else 'off'} "
              f"record={args.record or 'off'}({args.record_format}) (SQLite live + 永続記録)", file=sys.stderr)
        # 測定条件マニフェスト (run_meta.json) を録画 dir に併置 (best-effort、NAS でも可)
        try:
            import run_meta
            # dashboard 起動の start_record でも書けるよう meta_writer を注入
            recorder.meta_writer = lambda d: run_meta.write_run_meta(d, args=args, extra_notes=args.run_note)
            if args.record:
                p = run_meta.write_run_meta(args.record, args=args, extra_notes=args.run_note)
                print(f"[rpid] run_meta: {p}", file=sys.stderr)
        except Exception as e:
            print(f"[rpid] run_meta 書込失敗(続行): {e}", file=sys.stderr)

        def write_raw(robot_id, hid_ip, t, msg):
            recorder.put("metrics_raw", {"robot_id": robot_id, "hid_ip": hid_ip,
                "t_rpid_recv_unix": f"{t:.6f}",
                "json": json.dumps(msg, separators=(",", ":"))})

        def write_owd_dl(row):
            recorder.put("rx_dl", row)

        def write_snf(row):
            recorder.put("sniffer_frame", row)

        def write_hb(row):
            recorder.put("sniffer_hb", row)

        def write_uplink(robot_id, hid_ip, t, size):
            recorder.put("uplink", {"robot_id": robot_id, "hid_ip": hid_ip,
                "t_rpid_recv_unix": f"{t:.6f}", "size_bytes": size})

        def write_pps_uart(row):
            recorder.put("pps_uart", row)
            pps_uart_records.append((row["t_rpid_recv_unix"], row["tsf_us"]))

        def write_pps_gpio(row):
            recorder.put("pps_gpio", row)
            pps_gpio_records.append((row["unix_assert"], row["sequence"]))

        def write_wire(row):
            recorder.put("wire", row)

        # tx_ul は (lock, writer) tuple 経路で live 即時記録
        tx_ul_records = (threading.Lock(), lambda r: recorder.put("tx_ul", r))
    else:
        raw_w_obj = RotatingCSVWriter(out_dir / "metrics_raw.csv",
            header=["robot_id", "hid_ip", "t_rpid_recv_unix", "json"],
            use_dict=False, keep_recent_s=keep_s, expected_rate_hz=300)
        owd_w_obj = RotatingCSVWriter(out_dir / "owd_dl.csv",
            header=["robot_id", "hid_ip", "hid_seq", "dl_seq", "aipc_seq",
                    "corr_unix_time", "t_rpid_recv_unix",
                    "t_hid_rx_tsf_us", "frame_size", "owd_dl_approx_us"],
            keep_recent_s=keep_s, expected_rate_hz=200)
        snf_w_obj = RotatingCSVWriter(out_dir / "sniffer.csv",
            header=["t_rpid_recv_unix", "rx_seq", "t_local_us_lo", "rx_timestamp_us",
                    "tsf_us", "bb_format", "rate", "channel", "rssi", "sig_len",
                    "hdr_seq", "src", "dst", "fc_lo", "fc_hi", "dropped_lo"],
            keep_recent_s=keep_s, expected_rate_hz=200)
        snf_hb_w_obj = RotatingCSVWriter(out_dir / "sniffer_hb.csv",
            header=["t_rpid_recv_unix", "captured_total", "dropped_total", "t_now_us_lo", "rssi_now"],
            keep_recent_s=keep_s, expected_rate_hz=2)
        uplink_w_obj = RotatingCSVWriter(out_dir / "uplink_arrivals.csv",
            header=["robot_id", "hid_ip", "t_rpid_recv_unix", "size_bytes"],
            use_dict=False, keep_recent_s=keep_s, expected_rate_hz=100)
        pps_uart_w_obj = RotatingCSVWriter(out_dir / "pps_uart.csv",
            header=["t_rpid_recv_unix", "tsf_us", "esp_us"],
            keep_recent_s=keep_s, expected_rate_hz=1)
        pps_gpio_w_obj = None
        if args.pps_device:
            pps_gpio_w_obj = RotatingCSVWriter(out_dir / "pps_gpio.csv",
                header=["unix_assert", "sequence"],
                keep_recent_s=keep_s, expected_rate_hz=1)
        legacy_writers = [raw_w_obj, owd_w_obj, snf_w_obj, snf_hb_w_obj,
                          uplink_w_obj, pps_uart_w_obj]
        if pps_gpio_w_obj is not None:
            legacy_writers.append(pps_gpio_w_obj)

        def write_raw(robot_id, hid_ip, t, msg):
            raw_w_obj.write_row([robot_id, hid_ip, f"{t:.6f}", json.dumps(msg, separators=(",", ":"))])

        def write_owd_dl(row):
            row["t_rpid_recv_unix"] = f"{row['t_rpid_recv_unix']:.6f}"
            row["corr_unix_time"] = f"{row['corr_unix_time']:.6f}"
            row["owd_dl_approx_us"] = f"{row['owd_dl_approx_us']:.1f}"
            owd_w_obj.write_row(row)

        def write_snf(row):
            row["t_rpid_recv_unix"] = f"{row['t_rpid_recv_unix']:.6f}"
            snf_w_obj.write_row(row)

        def write_hb(row):
            row["t_rpid_recv_unix"] = f"{row['t_rpid_recv_unix']:.6f}"
            snf_hb_w_obj.write_row(row)

        def write_uplink(robot_id, hid_ip, t, size):
            uplink_w_obj.write_row([robot_id, hid_ip, f"{t:.6f}", size])

        def write_pps_uart(row):
            r = {"t_rpid_recv_unix": f"{row['t_rpid_recv_unix']:.6f}",
                 "tsf_us": row["tsf_us"], "esp_us": row["esp_us"]}
            pps_uart_w_obj.write_row(r)
            pps_uart_records.append((row["t_rpid_recv_unix"], row["tsf_us"]))

        def write_pps_gpio(row):
            if pps_gpio_w_obj is None:
                return
            r = {"unix_assert": f"{row['unix_assert']:.9f}", "sequence": row["sequence"]}
            pps_gpio_w_obj.write_row(r)
            pps_gpio_records.append((row["unix_assert"], row["sequence"]))

        tx_ul_records = []  # shared list; persist at shutdown

    # ---------- Spin listeners ----------
    listeners = []
    uplink_listeners = []
    for rid in ids:
        l = MetricsListener(rid, stop_evt, write_raw, write_owd_dl, tx_ul_records, iface=args.iface)
        l.start()
        listeners.append(l)
        u = UplinkListener(rid, stop_evt, write_uplink, iface=args.iface)
        u.start()
        uplink_listeners.append(u)

    sniffer_thread = None
    if args.sniffer_port:
        sniffer_thread = SnifferReader(args.sniffer_port, args.sniffer_baud,
                                       stop_evt, write_snf, write_hb, write_pps_uart,
                                       auto_recover=not args.no_sniffer_auto_recover)
        sniffer_thread.start()

    pps_gpio_thread = None
    if args.pps_device:
        pps_gpio_thread = PpsGpioReader(args.pps_device, stop_evt, write_pps_gpio)
        pps_gpio_thread.start()

    ctrl_thread = None
    if args.ctrl_sock and use_recorder:
        ctrl_thread = ControlServer(args.ctrl_sock, recorder, stop_evt, sniffer=sniffer_thread)
        ctrl_thread.start()

    wire_thread = None
    if args.wire and use_recorder:
        wire_thread = WireReader(args.wire, ids, stop_evt, write_wire)
        wire_thread.start()

    # ---------- Watchdog (Stage 3a、観測専用の無音検出) ----------
    wd_thread = None
    if not args.no_watchdog:
        checks = []
        for l in listeners:
            checks.append((f"metrics:{l.port}",
                           lambda l=l: l.n_rx_dl + l.n_tx_ul + l.n_hb, 6.0))
        for u in uplink_listeners:
            checks.append((f"uplink:{u.port}", lambda u=u: u.n, 10.0))
        if sniffer_thread is not None:
            checks.append(("sniffer", lambda: sniffer_thread.n_records, 5.0))
        if pps_gpio_thread is not None:
            checks.append(("pps_gpio", lambda: pps_gpio_thread.n, 8.0))
        if wire_thread is not None:
            checks.append(("wire", lambda: wire_thread.n, 6.0))
        wd_thread = WatchdogThread(checks, stop_evt)
        wd_thread.start()

    dur_text = "無制限" if args.duration <= 0 else f"{args.duration}s"
    if use_recorder:
        mode_text = "v2 recorder (SQLite live + 永続 CSV)"
    elif keep_s > 0:
        mode_text = f"legacy keep_recent={keep_s}s (live viewer)"
    else:
        mode_text = "legacy append-only (record)"
    iface_text = f" iface={args.iface}" if args.iface else ""
    print(f"[rpid] ids={ids} duration={dur_text} out={out_dir}{iface_text}  [{mode_text}]", file=sys.stderr)
    t_start = time.monotonic()
    last_print = t_start
    try:
        while not stop_evt.is_set() and (
            args.duration <= 0 or (time.monotonic() - t_start) < args.duration
        ):
            time.sleep(0.5)
            now = time.monotonic()
            if now - last_print >= 5.0:
                last_print = now
                summary = ", ".join(
                    f"id{l.robot_id}: dl={l.n_rx_dl}"
                    + (f"(b{l.n_rx_dlb}/L{l.n_batch_lost})" if l.n_rx_dlb else "")
                    + f" ul={l.n_tx_ul} hb={l.n_hb}"
                    for l in listeners)
                print(f"[rpid] 経過={now - t_start:.1f}s  {summary}", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    stop_evt.set()
    for l in listeners:
        l.join(timeout=2.0)
    if sniffer_thread:
        sniffer_thread.join(timeout=2.0)
    if pps_gpio_thread:
        pps_gpio_thread.join(timeout=3.0)
    if ctrl_thread:
        ctrl_thread.join(timeout=2.0)
    if wire_thread:
        wire_thread.join(timeout=2.0)
    if wd_thread:
        wd_thread.join(timeout=3.0)

    for w in legacy_writers:
        w.close()

    # 後処理 (bridge / tx_ul) 出力先: record 優先、無ければ live db dir、legacy は out_dir
    if use_recorder:
        post_dir = Path(args.record) if args.record else Path(args.live_db).parent
    else:
        post_dir = out_dir
    post_dir.mkdir(parents=True, exist_ok=True)

    # ---------- PPS bridge join (PPS GPIO event ↔ sniffer UART PPS marker) ----------
    # 仕様: PPS は 1 Hz、両方とも sniffer 発の同じ TSF 境界由来。
    # GPIO 側は CLOCK_REALTIME assert (unix_assert)、UART 側は tsf_us (esp_timer dispatch
    # jitter 含む) + 到達時刻 (t_rpid_recv_unix)。bridge_offset = unix_assert - tsf_us/1e6。
    # 突合は「同一パルスの unix 時刻が近い」ことを使う最近傍 join (1 対 1)。index 対応だと
    # どちらかが 1 パルス取りこぼすと以降全行が ~1s ずれて系統破綻するため (issue #1)。
    if pps_gpio_records and pps_uart_records:
        bridge_path = post_dir / "pps_bridge.csv"
        PAIR_WINDOW_S = 0.5   # 1Hz パルスの半周期。これを超える差は別パルス = 未対応
        with bridge_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["unix_assert", "tsf_us", "bridge_offset_s",
                        "uart_recv_unix", "uart_delay_ms"])
            gpio_sorted = sorted(pps_gpio_records, key=lambda r: r[0])     # (unix_assert, seq)
            uart_sorted = sorted(pps_uart_records, key=lambda r: r[0])     # (t_rpid_recv_unix, tsf)
            offsets = []
            j = 0
            n_unmatched = 0
            for ua, _seq in gpio_sorted:
                # uart 側を ua との時間差で最近傍探索 (両系列ソート済、単調前進)
                while j < len(uart_sorted) and uart_sorted[j][0] < ua - PAIR_WINDOW_S:
                    j += 1   # ua より古すぎる uart は対応相手なし (取りこぼし) → 捨てる
                if j >= len(uart_sorted) or uart_sorted[j][0] > ua + PAIR_WINDOW_S:
                    n_unmatched += 1
                    continue  # この gpio パルスに対応する uart が窓内に無い
                ur, tsf = uart_sorted[j]
                j += 1        # 1 対 1: 使った uart は consume
                bridge = ua - tsf / 1e6
                offsets.append(bridge)
                w.writerow([f"{ua:.9f}", tsf, f"{bridge:.9f}",
                            f"{ur:.6f}", f"{(ur - ua) * 1000:.3f}"])
            if n_unmatched:
                print(f"[pps-bridge] {n_unmatched} gpio パルスが {PAIR_WINDOW_S}s 窓内に "
                      "対応 uart 無し (取りこぼし) → skip", file=sys.stderr)
        # 簡易統計
        if offsets:
            offs_sorted = sorted(offsets)
            med = offs_sorted[len(offs_sorted) // 2]
            print(f"[pps-bridge] n={len(offsets)} median bridge_offset={med:.9f}s  "
                  f"({bridge_path}) を出力", file=sys.stderr)
    elif args.pps_device:
        print(f"[pps-bridge] pps_gpio={len(pps_gpio_records)} pps_uart={len(pps_uart_records)} "
              "→ join 不可 (片方ゼロ)", file=sys.stderr)

    if use_recorder:
        # tx_ul は run 中に recorder へ即時記録済み。recorder を flush/close。
        recorder.close()
        print(f"[rpid] recorder 終了。出力: live={args.live_db if args.live else '-'} "
              f"record={args.record or '-'} post_dir={post_dir}", file=sys.stderr)
    else:
        # Persist tx_ul records to CSV (legacy, was in-memory only)
        tx_ul_path = post_dir / "tx_ul.csv"
        with tx_ul_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "robot_id", "hid_ip", "hid_seq", "ul_seq", "tx_port",
                "t_hid_tx_tsf_us", "t_rpid_recv_unix", "frame_size"])
            w.writeheader()
            for r in tx_ul_records:
                r2 = dict(r)
                if r2.get("t_rpid_recv_unix") is not None:
                    r2["t_rpid_recv_unix"] = f"{r2['t_rpid_recv_unix']:.6f}"
                w.writerow(r2)
        print(f"[rpid] {tx_ul_path} に書込 ({len(tx_ul_records)} tx_ul records)", file=sys.stderr)
        print(f"[rpid] 出力先 {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
