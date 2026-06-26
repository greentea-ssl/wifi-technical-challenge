#!/usr/bin/env python3
"""
SanRei_HID OTA Update Tool
ESP32C5Controller / WioDisplay 用OTAファームウェア更新GUIツール
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import threading
import socket
import socketserver
import http.server
import os
import time
from pathlib import Path

# OTA UDPコマンド (両ターゲット共通)
CMD_OTA_UPDATE = 0x30

# ターゲット定義
# - ESP32C5: ポート 40999 (EMSと共有), bin = ESP32C5Controller.ino.bin
# - WioDisplay: ポート 41000 (専用), bin = WioDisplay.ino.bin
TARGETS = {
    "ESP32C5Controller": {
        "udp_port": 40999,
        "default_firmware_rel": Path("src") / "ESP32C5Controller" / "ESP32C5Controller.ino.bin",
    },
    "WioDisplay": {
        "udp_port": 41000,
        "default_firmware_rel": Path("src") / "WioDisplay" / "WioDisplay.ino.bin",
    },
}


class OTAHandler(http.server.BaseHTTPRequestHandler):
    """ファームウェア配信用HTTPハンドラ"""

    firmware_path = None
    log_callback = None

    def log_message(self, format, *args):
        """HTTPサーバーのログ出力"""
        if OTAHandler.log_callback:
            OTAHandler.log_callback(f"[HTTP] {args[0]}")

    def do_GET(self):
        """GETリクエスト処理"""
        if self.path == '/firmware.bin':
            try:
                with open(self.firmware_path, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', len(content))
                self.end_headers()
                self.wfile.write(content)
                if OTAHandler.log_callback:
                    OTAHandler.log_callback(f"[HTTP] Sent firmware.bin ({len(content):,} bytes)")
            except FileNotFoundError:
                self.send_error(404, "Firmware not found")
                if OTAHandler.log_callback:
                    OTAHandler.log_callback(f"[HTTP] Error: Firmware not found at {self.firmware_path}")
        else:
            self.send_error(404, "Not found")
            if OTAHandler.log_callback:
                OTAHandler.log_callback(f"[HTTP] 404: {self.path}")


class OTAUpdateTool:
    def __init__(self, root):
        self.root = root
        self.root.title("SanRei_HID OTA Update Tool")
        self.root.geometry("700x720")
        self.root.resizable(True, True)

        # スクリプトのディレクトリを取得
        self.script_dir = Path(__file__).parent

        # サーバー状態
        self.server_running = False
        self.http_server = None
        self.server_thread = None
        self.device_count = 0

        # UI作成
        self.create_widgets()

        # 初期ターゲット設定 (UI構築後)
        self.on_target_changed()

    def create_widgets(self):
        # タイトルフレーム
        title_frame = ttk.Frame(self.root, padding="10")
        title_frame.pack(fill=tk.X)

        title_label = ttk.Label(
            title_frame,
            text="SanRei_HID OTA Update Tool",
            font=("Arial", 16, "bold")
        )
        title_label.pack()

        # ターゲット選択フレーム
        target_frame = ttk.LabelFrame(self.root, text="Target Device", padding="10")
        target_frame.pack(fill=tk.X, padx=10, pady=5)

        self.target_var = tk.StringVar(value="ESP32C5Controller")
        for name in TARGETS.keys():
            ttk.Radiobutton(
                target_frame,
                text=name,
                variable=self.target_var,
                value=name,
                command=self.on_target_changed,
            ).pack(side=tk.LEFT, padx=10)

        # ファームウェア選択フレーム
        fw_frame = ttk.LabelFrame(self.root, text="Firmware Selection", padding="10")
        fw_frame.pack(fill=tk.X, padx=10, pady=5)

        self.firmware_var = tk.StringVar()

        fw_entry = ttk.Entry(fw_frame, textvariable=self.firmware_var, font=("Consolas", 9))
        fw_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        browse_button = ttk.Button(
            fw_frame,
            text="Browse...",
            command=self.browse_firmware,
            width=12
        )
        browse_button.pack(side=tk.LEFT)

        # ファームウェア情報フレーム
        info_frame = ttk.LabelFrame(self.root, text="Firmware Information", padding="10")
        info_frame.pack(fill=tk.X, padx=10, pady=5)

        self.info_label = ttk.Label(info_frame, text="No firmware selected")
        self.info_label.pack(anchor=tk.W)

        # 設定フレーム
        config_frame = ttk.LabelFrame(self.root, text="Settings", padding="10")
        config_frame.pack(fill=tk.X, padx=10, pady=5)

        # HTTPポート
        port_frame = ttk.Frame(config_frame)
        port_frame.pack(fill=tk.X, pady=2)
        ttk.Label(port_frame, text="HTTP Port:").pack(side=tk.LEFT)
        self.port_var = tk.IntVar(value=8080)
        port_spinbox = ttk.Spinbox(
            port_frame,
            from_=1024,
            to=65535,
            textvariable=self.port_var,
            width=10
        )
        port_spinbox.pack(side=tk.LEFT, padx=5)

        # ブロードキャストアドレス（手動設定可能）
        bc_frame = ttk.Frame(config_frame)
        bc_frame.pack(fill=tk.X, pady=2)
        ttk.Label(bc_frame, text="Target IP:").pack(side=tk.LEFT)
        self.broadcast_var = tk.StringVar(value=self.get_broadcast_address())
        broadcast_entry = ttk.Entry(
            bc_frame,
            textvariable=self.broadcast_var,
            width=18
        )
        broadcast_entry.pack(side=tk.LEFT, padx=5)

        ttk.Button(
            bc_frame,
            text="Auto",
            command=self.refresh_network_info,
            width=8
        ).pack(side=tk.LEFT)

        # ローカルIP
        ip_frame = ttk.Frame(config_frame)
        ip_frame.pack(fill=tk.X, pady=2)
        ttk.Label(ip_frame, text="Local IP:").pack(side=tk.LEFT)
        self.local_ip_label = ttk.Label(ip_frame, text=self.get_local_ip())
        self.local_ip_label.pack(side=tk.LEFT, padx=5)

        # UDPポート（ターゲットにより自動切替）
        udp_frame = ttk.Frame(config_frame)
        udp_frame.pack(fill=tk.X, pady=2)
        ttk.Label(udp_frame, text="UDP Port:").pack(side=tk.LEFT)
        self.udp_port_label = ttk.Label(udp_frame, text="-")
        self.udp_port_label.pack(side=tk.LEFT, padx=5)

        # デバイスカウンター
        device_frame = ttk.Frame(config_frame)
        device_frame.pack(fill=tk.X, pady=2)
        ttk.Label(device_frame, text="Devices Updated:").pack(side=tk.LEFT)
        self.device_count_label = ttk.Label(
            device_frame,
            text="0",
            font=("Arial", 12, "bold"),
            foreground="blue"
        )
        self.device_count_label.pack(side=tk.LEFT, padx=5)

        # ボタンフレーム (ウィンドウ下部に固定 — log領域より先にpackして
        # 画面が低くてもボタンが押し出されないようにする)
        button_frame = ttk.Frame(self.root, padding="10")
        button_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.start_button = ttk.Button(
            button_frame,
            text="Start OTA Update",
            command=self.start_ota,
            width=18
        )
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = ttk.Button(
            button_frame,
            text="Stop",
            command=self.stop_ota,
            width=15,
            state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=5)

        self.clear_button = ttk.Button(
            button_frame,
            text="Clear Log",
            command=self.clear_log,
            width=12
        )
        self.clear_button.pack(side=tk.LEFT, padx=5)

        ttk.Button(
            button_frame,
            text="Close",
            command=self.on_close,
            width=12
        ).pack(side=tk.RIGHT, padx=5)

        # ログ出力エリア (残り領域を埋める)
        log_frame = ttk.LabelFrame(self.root, text="Log", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            state=tk.DISABLED
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        """ログを出力"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.root.update_idletasks()

    def clear_log(self):
        """ログをクリア"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.device_count = 0
        self.device_count_label.config(text="0")

    def get_local_ip(self):
        """ローカルIPアドレスを取得"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def get_broadcast_address(self):
        """ブロードキャストアドレスを取得"""
        local_ip = self.get_local_ip()
        parts = local_ip.split('.')
        parts[3] = '255'
        return '.'.join(parts)

    def refresh_network_info(self):
        """ネットワーク情報を更新"""
        self.local_ip_label.config(text=self.get_local_ip())
        self.broadcast_var.set(self.get_broadcast_address())

    def browse_firmware(self):
        """ファームウェアファイルを選択"""
        filename = filedialog.askopenfilename(
            title="Select Firmware File",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")]
        )
        if filename:
            self.firmware_var.set(filename)
            self.update_firmware_info()

    def update_firmware_info(self):
        """ファームウェア情報を更新"""
        fw_path = self.firmware_var.get()
        if os.path.exists(fw_path):
            size = os.path.getsize(fw_path)
            note = ""
            # WioDisplay (SAMD51) はステージング方式の都合でアプリ ~252KB が上限
            if self.target_var.get() == "WioDisplay" and size > 252 * 1024:
                note = "  WARNING: > 252 KB (may not fit SAMD51 staging area)"
            self.info_label.config(
                text=f"Size: {size:,} bytes | Path: {os.path.basename(fw_path)}{note}"
            )
        else:
            self.info_label.config(text="File not found")

    def on_target_changed(self):
        """ターゲット切替時の処理: ポートとデフォルトbinパス更新"""
        target = self.target_var.get()
        cfg = TARGETS[target]
        # UDPポート表示更新
        self.udp_port_label.config(text=str(cfg["udp_port"]))
        # デフォルトファームウェアを設定
        default_fw = self.script_dir / cfg["default_firmware_rel"]
        if default_fw.exists():
            self.firmware_var.set(str(default_fw))
        else:
            self.firmware_var.set("")
        self.update_firmware_info()

    def send_ota_command(self, port):
        """UDPブロードキャストでOTA更新指令を送信"""
        local_ip = self.get_local_ip()
        url = f"http://{local_ip}:{port}/firmware.bin"

        # UDPパケット作成: [CMD_OTA_UPDATE][URL文字列]
        message = bytes([CMD_OTA_UPDATE]) + url.encode('utf-8')

        # ユーザーが入力したIPアドレスを使用
        target_ip = self.broadcast_var.get()
        target_udp_port = TARGETS[self.target_var.get()]["udp_port"]

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        try:
            sock.sendto(message, (target_ip, target_udp_port))
            self.log(f"[UDP] Sent OTA command to {target_ip}:{target_udp_port}")
            self.log(f"[UDP] URL: {url}")
        finally:
            sock.close()

    def run_http_server(self, port, firmware_path, timeout):
        """HTTPサーバーを実行"""
        OTAHandler.firmware_path = firmware_path
        OTAHandler.log_callback = self.log

        class ThreadedHTTPServer(socketserver.TCPServer):
            allow_reuse_address = True

        with ThreadedHTTPServer(("", port), OTAHandler) as httpd:
            self.http_server = httpd
            self.log(f"[HTTP] Server started on port {port}")
            self.log(f"[HTTP] Serving: {firmware_path}")

            httpd.timeout = 1

            start_time = time.time()
            while self.server_running and time.time() - start_time < timeout:
                httpd.handle_request()

            self.log("[HTTP] Server stopped")

    def start_ota(self):
        """OTA更新を開始"""
        firmware_path = self.firmware_var.get()

        if not os.path.exists(firmware_path):
            messagebox.showerror("Error", "Firmware file not found!")
            return

        self.update_firmware_info()

        self.server_running = True
        self.device_count = 0
        self.device_count_label.config(text="0")

        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)

        # ログをクリアして開始
        self.clear_log()
        self.log("=" * 60)
        self.log(f"Starting OTA Update [Target: {self.target_var.get()}]...")
        self.log("=" * 60)

        # HTTPサーバーを別スレッドで起動
        port = self.port_var.get()
        timeout = 120  # 2分

        self.server_thread = threading.Thread(
            target=self.run_http_server,
            args=(port, firmware_path, timeout),
            daemon=True
        )
        self.server_thread.start()

        # 少し待ってからOTAコマンドを送信
        self.root.after(1000, lambda: self.send_ota_command(port))

    def stop_ota(self):
        """OTA更新を停止
        run_http_server() は handle_request() を timeout=1 でループしているため、
        server_running フラグを下げれば最大1秒で自然終了する。
        httpd.shutdown() は serve_forever() 用で、handle_request ループで呼ぶと
        無限ブロックするため使わない。
        """
        self.server_running = False
        self.log("[OTA] Stopping... (up to ~1s)")
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)

    def on_close(self):
        """閉じるボタン"""
        if self.server_running:
            if messagebox.askyesno("Confirm", "OTA update is running. Stop and close?"):
                self.stop_ota()
                self.root.after(1000, self.root.destroy)
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    app = OTAUpdateTool(root)
    root.mainloop()


if __name__ == "__main__":
    main()
