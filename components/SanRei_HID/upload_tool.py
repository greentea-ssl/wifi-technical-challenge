#!/usr/bin/env python3
"""
SanRei HID Upload Tool
ファームウェアをUSB経由で書き込むGUIツール
ESP32C5 Controller と WioDisplay に対応
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import subprocess
import threading
import serial.tools.list_ports
import os
import sys
from pathlib import Path

# 設定
DEFAULT_FQBN_C5 = "esp32:esp32:XIAO_ESP32C5"
DEFAULT_FQBN_WIO = "Seeeduino:samd:seeed_wio_terminal"

KNOWN_FQBNS = {
    "XIAO ESP32-C5 (Controller)": "esp32:esp32:XIAO_ESP32C5",
    "ESP32-C5 Generic": "esp32:esp32:esp32c5",
    "Wio Terminal (Display)": "Seeeduino:samd:seeed_wio_terminal",
}


class UploadTool:
    def __init__(self, root):
        self.root = root
        self.root.title("SanRei HID Upload Tool")
        self.root.geometry("750x700")
        self.root.resizable(True, False)

        # スクリプトのディレクトリを取得
        self.script_dir = Path(__file__).parent

        # 書き込み状態
        self.uploading = False
        self.upload_process = None

        # UI作成
        self.create_widgets()

        # 初期データ設定
        self.refresh_ports()
        self.on_target_changed()  # デフォルトファームウェアを設定

    def create_widgets(self):
        # タイトルフレーム
        title_frame = ttk.Frame(self.root, padding="10")
        title_frame.pack(fill=tk.X)

        title_label = ttk.Label(
            title_frame,
            text="SanRei HID Upload Tool",
            font=("Arial", 16, "bold")
        )
        title_label.pack()

        subtitle_label = ttk.Label(
            title_frame,
            text="ESP32C5 Controller / Wio Display Firmware Upload",
            font=("Arial", 10)
        )
        subtitle_label.pack()

        # ターゲット選択フレーム
        target_frame = ttk.LabelFrame(self.root, text="Target Device", padding="10")
        target_frame.pack(fill=tk.X, padx=10, pady=5)

        self.target_var = tk.StringVar(value="ESP32C5 Controller")

        ttk.Radiobutton(
            target_frame,
            text="ESP32C5 Controller (XIAO ESP32-C5)",
            variable=self.target_var,
            value="ESP32C5 Controller",
            command=self.on_target_changed
        ).pack(anchor=tk.W, padx=10)

        ttk.Radiobutton(
            target_frame,
            text="Wio Display (Wio Terminal)",
            variable=self.target_var,
            value="Wio Display",
            command=self.on_target_changed
        ).pack(anchor=tk.W, padx=10)

        # ファームウェア選択フレーム
        fw_frame = ttk.LabelFrame(self.root, text="Firmware", padding="10")
        fw_frame.pack(fill=tk.X, padx=10, pady=5)

        self.firmware_var = tk.StringVar()

        fw_entry = ttk.Entry(fw_frame, textvariable=self.firmware_var, font=("Consolas", 9))
        fw_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        browse_button = ttk.Button(
            fw_frame,
            text="Browse...",
            command=self.browse_firmware,
            width="12"
        )
        browse_button.pack(side=tk.LEFT)

        # ボード選択フレーム
        board_frame = ttk.LabelFrame(self.root, text="Board Configuration", padding="10")
        board_frame.pack(fill=tk.X, padx=10, pady=5)

        # ポート選択
        port_frame = ttk.Frame(board_frame)
        port_frame.pack(fill=tk.X, pady=2)
        ttk.Label(port_frame, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(port_frame, textvariable=self.port_var, width=25, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=5)

        ttk.Button(
            port_frame,
            text="Refresh",
            command=self.refresh_ports,
            width="10"
        ).pack(side=tk.LEFT)

        # ボード選択（ドロップダウンではなく読み取り専用ラベル）
        board_info_frame = ttk.Frame(board_frame)
        board_info_frame.pack(fill=tk.X, pady=2)
        ttk.Label(board_info_frame, text="FQBN:").pack(side=tk.LEFT)
        self.fqbn_label = ttk.Label(
            board_info_frame,
            text=DEFAULT_FQBN_C5,
            font=("Consolas", 9)
        )
        self.fqbn_label.pack(side=tk.LEFT, padx=5)

        # 進捗フレーム
        progress_frame = ttk.LabelFrame(self.root, text="Progress", padding="10")
        progress_frame.pack(fill=tk.X, padx=10, pady=5)

        self.progress = ttk.Progressbar(
            progress_frame,
            mode='indeterminate',
            length=400
        )
        self.progress.pack()

        self.status_label = ttk.Label(progress_frame, text="Ready")
        self.status_label.pack(pady=5)

        # ログ出力エリア
        log_frame = ttk.LabelFrame(self.root, text="Log", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            state=tk.DISABLED,
            height=10
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # ボタンフレーム
        button_frame = ttk.Frame(self.root, padding="10")
        button_frame.pack(fill=tk.X)

        self.upload_button = ttk.Button(
            button_frame,
            text="Upload",
            command=self.start_upload,
            width="15"
        )
        self.upload_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = ttk.Button(
            button_frame,
            text="Stop",
            command=self.stop_upload,
            width="15",
            state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=5)

        ttk.Button(
            button_frame,
            text="Clear Log",
            command=self.clear_log,
            width="12"
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            button_frame,
            text="Build & Upload",
            command=self.build_and_upload,
            width="18"
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            button_frame,
            text="Exit",
            command=self.on_exit,
            width="12"
        ).pack(side=tk.RIGHT, padx=5)

        # 初期ターゲット設定
        self.on_target_changed()

    def on_target_changed(self):
        """ターゲット変更時の処理"""
        target = self.target_var.get()

        if target == "ESP32C5 Controller":
            self.fqbn_label.config(text=DEFAULT_FQBN_C5)
            default_fw = self.script_dir / "src" / "ESP32C5Controller" / "ESP32C5Controller.ino.bin"
        else:  # Wio Display
            self.fqbn_label.config(text=DEFAULT_FQBN_WIO)
            default_fw = self.script_dir / "src" / "WioDisplay" / "WioDisplay.ino.uf2"

        if default_fw.exists():
            self.firmware_var.set(str(default_fw))

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

    def browse_firmware(self):
        """ファームウェアファイルを選択"""
        target = self.target_var.get()

        if target == "ESP32C5 Controller":
            file_types = [("Binary files", "*.bin"), ("All files", "*.*")]
            initial_dir = str(self.script_dir / "src" / "ESP32C5Controller")
        else:  # Wio Display
            file_types = [("UF2 files", "*.uf2"), ("All files", "*.*")]
            initial_dir = str(self.script_dir / "src" / "WioDisplay")

        filename = filedialog.askopenfilename(
            title="Select Firmware File",
            filetypes=file_types,
            initialdir=initial_dir
        )

        if filename:
            # パスを正規化（バックスラッシュをスラッシュに変換）
            filename = os.path.normpath(filename)
            self.firmware_var.set(filename)
            self.log(f"Firmware selected: {filename}")
            self.root.update_idletasks()

    def refresh_ports(self):
        """シリアルポートをスキャン"""
        ports = serial.tools.list_ports.comports()
        port_list = [port.device for port in ports]
        self.port_combo['values'] = port_list

        if port_list:
            target = self.target_var.get()
            # ターゲットに応じてデバイスを優先選択
            port_found = False
            if target == "ESP32C5 Controller":
                for port in ports:
                    if "ESP32" in port.description or "XIAO" in port.description:
                        self.port_var.set(port.device)
                        port_found = True
                        break
            else:  # Wio Display
                for port in ports:
                    if "Wio" in port.description or "Seeed" in port.description or "ATSAMD" in port.description:
                        self.port_var.set(port.device)
                        port_found = True
                        break

            # 見つからない場合は最初のポート
            if not port_found and not self.port_var.get():
                self.port_var.set(port_list[0])

            self.log(f"Found {len(port_list)} serial port(s)")
        else:
            self.log("No serial ports found")
            self.port_var.set("")

    def validate_inputs(self):
        """入力値を検証"""
        firmware_path = self.firmware_var.get()
        if not firmware_path or not os.path.exists(firmware_path):
            messagebox.showerror("Error", "Firmware file not found!")
            return False

        port = self.port_var.get()
        if not port:
            messagebox.showerror("Error", "Please select a serial port!")
            return False

        return True

    def get_sketch_dir(self):
        """スケッチディレクトリを取得"""
        target = self.target_var.get()

        if target == "ESP32C5 Controller":
            return self.script_dir / "src" / "ESP32C5Controller"
        else:  # Wio Display
            return self.script_dir / "src" / "WioDisplay"

    def start_upload(self):
        """アップロードを開始"""
        if not self.validate_inputs():
            return

        if self.uploading:
            return

        firmware_path = self.firmware_var.get()
        port = self.port_var.get()
        target = self.target_var.get()

        # FQBNを取得
        if target == "ESP32C5 Controller":
            fqbn = DEFAULT_FQBN_C5
        else:  # Wio Display
            fqbn = DEFAULT_FQBN_WIO

        # スケッチディレクトリを取得
        sketch_dir = self.get_sketch_dir()

        self.uploading = True
        self.upload_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.progress.start(10)

        self.clear_log()
        self.log("=" * 60)
        self.log("Starting firmware upload...")
        self.log(f"Target: {target}")
        self.log(f"Firmware: {firmware_path}")
        self.log(f"Port: {port}")
        self.log(f"FQBN: {fqbn}")
        self.log("=" * 60)

        # Wio Displayの場合はUF2コピー方式
        if target == "Wio Display":
            self.upload_wio_uf2(firmware_path, port)
        else:
            # ESP32C5はarduino-cli upload
            self.upload_arduino(firmware_path, port, fqbn, sketch_dir)

    def upload_arduino(self, firmware_path, port, fqbn, sketch_dir):
        """arduino-cliでアップロード"""
        # ファームウェアファイルの拡張子を確認
        if firmware_path.endswith('.bin') and not firmware_path.endswith('.ino.bin'):
            # 単体の.binファイル（リリース版など）はesptoolで直接書き込み
            self.upload_esptool(firmware_path, port)
            return

        # スケッチファイルを探す
        sketch_file = sketch_dir / "ESP32C5Controller.ino"
        if not sketch_file.exists():
            self.log(f"Error: Sketch file not found: {sketch_file}")
            self.upload_error("Sketch file not found")
            return

        # アップロードコマンドを実行
        cmd = [
            "arduino-cli",
            "upload",
            "-p", port,
            "--fqbn", fqbn,
            str(sketch_file)
        ]

        self.log(f"Command: {' '.join(cmd)}")
        self.log("")

        # スレッドで実行
        self.upload_thread = threading.Thread(
            target=self.run_upload,
            args=(cmd,),
            daemon=True
        )
        self.upload_thread.start()

    def upload_esptool(self, firmware_path, port):
        """esptoolで直接.binファイルを書き込み"""
        # ESP32-C5のファームウェアアドレス（Arduinoスケッチ用）
        # Arduino IDEでビルドしたスケッチは通常0x10000から始まります
        flash_address = "0x10000"

        self.log(f"Using esptool to flash firmware directly...")
        self.log(f"Firmware: {firmware_path}")
        self.log(f"Flash address: {flash_address}")
        self.log("")

        # esptoolコマンドを実行
        # まずarduino-cliに含まれるesptoolを使うか、システムのesptoolを使う
        cmd = [
            "esptool.py",
            "--chip", "esp32c5",
            "--port", port,
            "--baud", "921600",
            "--before", "default_reset",
            "--after", "hard_reset",
            "write_flash",
            "--flash_mode", "dio",
            "--flash_freq", "80m",
            "--flash_size", "4MB",
            flash_address,
            firmware_path
        ]

        self.log(f"Command: {' '.join(cmd)}")
        self.log("")

        # スレッドで実行
        self.upload_thread = threading.Thread(
            target=self.run_upload_with_fallback,
            args=(cmd, firmware_path),
            daemon=True
        )
        self.upload_thread.start()

    def run_upload_with_fallback(self, cmd, firmware_path):
        """esptoolが見つからない場合のフォールバック処理付き実行"""
        import shutil
        import os

        # Arduino CLIのesptoolを探す
        arduino_data_dirs = [
            Path.home() / "AppData" / "Local" / "Arduino15",
            Path.home() / ".arduino15",
        ]

        esptool_exe = None
        for arduino_dir in arduino_data_dirs:
            # ESP32ツールディレクトリを探す
            esp32_tools = arduino_dir / "packages" / "esp32" / "tools" / "esptool"
            if esp32_tools.exists():
                # esptoolのバージョンディレクトリを探す
                for version_dir in esp32_tools.iterdir():
                    if version_dir.is_dir():
                        # Windowsならesptool.exe
                        exe_path = version_dir / "esptool.exe"
                        if exe_path.exists():
                            esptool_exe = str(exe_path)
                            self.log(f"Found esptool.exe: {esptool_exe}")
                            break
            if esptool_exe:
                break

        if esptool_exe:
            cmd[0] = esptool_exe
        else:
            self.log("esptool not found in Arduino CLI installation")
            self.log("Searching in common locations...")

            # システムのesptoolも試す
            esptool_path = shutil.which("esptool")
            if esptool_path:
                self.log(f"Found system esptool: {esptool_path}")
                cmd[0] = esptool_path
            else:
                self.log("esptool not found. Please install Arduino CLI or esptool.")
                self.root.after(0, lambda: self.upload_error("esptool not found"))
                return

        try:
            self.log(f"Command: {' '.join(cmd)}")
            self.log("")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace'
            )

            # 出力をリアルタイム表示
            for line in process.stdout:
                line = line.rstrip()
                if line:
                    self.log(line)

            process.wait()
            return_code = process.returncode

            self.root.after(0, lambda: self.upload_complete(return_code))

        except Exception as e:
            self.root.after(0, lambda: self.upload_error(str(e)))

    def upload_wio_uf2(self, firmware_path, port):
        """Wio TerminalにUF2をコピー"""
        self.log("Wio Terminal requires manual UF2 installation:")
        self.log("")
        self.log("1. Connect Wio Terminal while pressing POWER button twice")
        self.log("   (bootloader mode - appears as 'Wio Terminal' drive)")
        self.log("")
        self.log("2. Copy the firmware .uf2 file to the Wio Terminal drive")
        self.log("")
        self.log(f"Firmware location: {firmware_path}")
        self.log("")
        self.log("Opening file location...")

        try:
            import subprocess
            import platform

            # ファイルの場所をエクスプローラーで開く
            if platform.system() == 'Windows':
                subprocess.run(['explorer', '/select,', firmware_path])
            elif platform.system() == 'Darwin':  # macOS
                subprocess.run(['open', '-R', firmware_path])
            else:  # Linux
                subprocess.run(['xdg-open', str(Path(firmware_path).parent)])

            self.log("File explorer opened. Please copy the .uf2 file manually.")
            self.log("")
            self.upload_complete(0)  # 手動なので成功として扱う

        except Exception as e:
            self.log(f"Could not open file explorer: {e}")
            self.upload_error(str(e))

    def run_upload(self, cmd):
        """アップロードを実行"""
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace'
            )

            # 出力をリアルタイム表示
            for line in process.stdout:
                line = line.rstrip()
                if line:
                    self.log(line)

            process.wait()
            return_code = process.returncode

            self.root.after(0, lambda: self.upload_complete(return_code))

        except Exception as e:
            self.root.after(0, lambda: self.upload_error(str(e)))

    def upload_complete(self, return_code):
        """アップロード完了時の処理"""
        self.uploading = False
        self.progress.stop()
        self.upload_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)

        self.log("")
        self.log("=" * 60)

        if return_code == 0:
            self.log("Upload completed successfully!")
            self.status_label.config(text="Upload completed")
            messagebox.showinfo("Success", "Firmware uploaded successfully!")
        else:
            self.log(f"Upload completed with return code: {return_code}")
            self.status_label.config(text="Upload completed")
            # Wio Terminalの場合は手動コピーなのでreturn_codeは無視
            if self.target_var.get() == "Wio Display":
                messagebox.showinfo("Info", "Please copy the .uf2 file to Wio Terminal drive manually.")
            elif return_code != 0:
                messagebox.showerror("Error", f"Upload failed with return code: {return_code}")

    def upload_error(self, error_msg):
        """アップロードエラー時の処理"""
        self.uploading = False
        self.progress.stop()
        self.upload_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)

        self.log(f"Error: {error_msg}")
        self.status_label.config(text="Error")
        messagebox.showerror("Error", error_msg)

    def stop_upload(self):
        """アップロードを停止"""
        if self.upload_process:
            self.upload_process.kill()
            self.log("Upload stopped by user")
        self.uploading = False
        self.progress.stop()
        self.upload_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.status_label.config(text="Stopped")

    def build_and_upload(self):
        """ビルドしてアップロード"""
        target = self.target_var.get()

        port = self.port_var.get()
        if not port:
            messagebox.showerror("Error", "Please select a serial port!")
            return

        self.clear_log()
        self.log("Starting build...")
        self.log("")

        # ターゲットに応じたビルドスクリプトを実行
        if target == "ESP32C5 Controller":
            build_dir = self.script_dir / "src" / "ESP32C5Controller"
            self.log(f"Building ESP32C5 Controller...")
            self.log(f"Directory: {build_dir}")
        else:  # Wio Display
            build_dir = self.script_dir / "src" / "WioDisplay"
            self.log(f"Building Wio Display...")
            self.log(f"Directory: {build_dir}")

        try:
            # ビルド用の新しいコンソールウィンドウを開く
            build_script = build_dir / "build.bat"

            if not build_script.exists():
                # build.batが存在しない場合は作成
                self.create_build_script(build_dir, target)

            # ビルドスクリプトを実行
            subprocess.Popen(
                ["cmd", "/c", str(build_script)],
                cwd=str(build_dir),
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
            self.log("Build started in new window.")
            self.log("Please complete the build, then click Upload.")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to start build: {e}")

    def create_build_script(self, build_dir, target):
        """ビルドスクリプトを作成"""
        script_path = build_dir / "build.bat"

        with open(script_path, 'w') as f:
            f.write("@echo off\n")
            f.write("cd /d \"%~dp0\"\n")
            f.write(f"echo Building {target}...\n")

            if target == "ESP32C5 Controller":
                # ESP32C5はarduino-cli直接
                f.write("arduino-cli compile -b esp32:esp32:XIAO_ESP32C5 ESP32C5Controller.ino --output-dir ./\n")
            else:  # Wio Display
                # Wio Displayはdocker compose
                f.write("docker compose build\n")
                f.write("docker compose run build\n")

            f.write("echo.\n")
            f.write("echo Build completed!\n")
            f.write("pause\n")

    def on_exit(self):
        """終了ボタン"""
        if self.uploading:
            if messagebox.askyesno("Confirm", "Upload in progress. Stop and exit?"):
                self.stop_upload()
                self.root.after(1000, self.root.destroy)
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    app = UploadTool(root)
    root.mainloop()


if __name__ == "__main__":
    main()
