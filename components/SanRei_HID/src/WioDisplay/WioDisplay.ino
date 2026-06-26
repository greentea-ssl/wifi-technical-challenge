/**
 * WioDisplay - I2C Slave Display Controller for WioTerminal
 *
 * Receives display commands from ESP32C5/S3 Master via I2C
 * Handles button input and sends events back to Master
 *
 * OTA: WiFi via RTL8720DN (rpcWiFi). Listens UDP/41000 for OTA command,
 *      downloads firmware over HTTP, writes to internal flash via
 *      InternalStorage (JAndrassy/ArduinoOTA), then reboots.
 *
 * Debug: 全ログを USB CDC (Serial) と 外部 USB-serial 変換器 (Serial3: D0=TX/D1=RX,
 *        SERCOM4) の両方へ出力 (LogMux)。VCC 非結線の変換器でログを取ることで、
 *        PowerBoard の給電制御を阻害せず電源投入直後の挙動を観測できる。
 *
 * Fix(1.4.0): 電源投入時の "xiao disconnected" / "PWR No Response" 対策。
 *   - serviceWiFi() の毎ループ WiFi.status() (rpcWiFi/RTL8720) を撤去 (接続失敗後に
 *     当該 RPC がブロックしメインループ停止→I2C 取りこぼしの主因)。接続状態は
 *     onWiFiEvent のイベント駆動 flag を使用。
 *   - I2C 受信を単一バッファ→リングバッファ化し Xiao の READY 再送バーストを吸収。
 *
 * Version: 1.4.0
 */

#include <Wire.h>
#include <TFT_eSPI.h>

// rpcWiFi must be included before ArduinoOTA
#include <rpcWiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>
#include <RPCmDNS.h>

// Use only InternalStorage from ArduinoOTA (no built-in network/port handling)
#define NO_OTA_NETWORK
#define NO_OTA_PORT
#include <ArduinoOTA.h>

#include "Free_Fonts.h"
#include "config.h"

#include "wiring_private.h"  // pinPeripheral()

// ============================================================================
// Debug log UART (外部 USB-serial 変換器, D0/D1, SERCOM4)
// ----------------------------------------------------------------------------
// 目的: VCC を結線しない USB-serial 変換器経由でログを取得できるようにする。
//       これにより USB から Wio へ給電されず、PowerBoard の給電制御を阻害せずに
//       電源投入直後の挙動 (xiao disconnected 等) を観測できる。
// 制約: SAMD51 の UART TX は PAD0/PAD2 のみ。
//       D0=PB08=SERCOM4/PAD0, D1=PB09=SERCOM4/PAD1 のため TX=D0 / RX=D1 が唯一の構成。
//       配線: 変換器 RX <- Wio D0 (TX) / 変換器 TX -> Wio D1 (RX, 本用途では未使用)。
// 注意: core は SERCOM4_x_Handler を Serial1 用に定義済み。既定ビルド (ROLE=0) では
//       Serial1 は SERCOM2 に割当たり SERCOM4 は空きだが、D1(RX) のノイズで SERCOM4 の
//       RXC 割り込みが立つと core ハンドラが Serial1(別SERCOM) を叩いてフラグを消せず
//       割り込みストームになる。送信はポーリングで割り込み不要なため、begin 後に
//       SERCOM4 の NVIC IRQ を無効化する (setup() 参照)。
static Uart Serial3(&sercom4, D1, D0, SERCOM_RX_PAD_1, UART_TX_PAD_0);

// USB CDC (Serial) と デバッグ UART (sercom4 = D0/D1) の両方へ出力するロガー。
//
// デバッグ UART への送信は「ポーリング書き込み」で行う (dbgByte)。
//   理由: SERCOM4 の割り込みハンドラは core の Wire.cpp が Wire1(gyro,sercom4) 用に
//   非weak で定義しており、Serial3 へ割り込みを向けられない (多重定義になる)。
//   Serial3.write() は割り込み駆動の TX リングを使うため、ハンドラが Serial3 のリングを
//   排出せず write が詰まる (旧実装が無音/ハングした真因)。そこで Uart オブジェクトは
//   begin() による sercom4 の UART 設定だけに使い、送信は DRE を待って DATA へ直接書く
//   (割り込み不要)。1 バイト約 87us 待つが有限で、ハングしない。
// USB CDC は USBDevice.connected() が真のときだけ書く (未接続時の write ブロック回避)。
// デバッグ UART を主経路として先に書く。
class LogMux : public Print {
    static inline void dbgByte(uint8_t b) {
        while (!sercom4.isDataRegisterEmptyUART()) { /* wait TX DRE */ }
        sercom4.writeDataUART(b);
    }
public:
    size_t write(uint8_t c) override {
        dbgByte(c);
        if (USBDevice.connected()) Serial.write(c);
        return 1;
    }
    size_t write(const uint8_t *buf, size_t len) override {
        for (size_t i = 0; i < len; i++) dbgByte(buf[i]);
        if (USBDevice.connected()) Serial.write(buf, len);
        return len;
    }
    void flush() {
        if (USBDevice.connected()) Serial.flush();
    }
};
static LogMux Log;

// ============================================================================
// Watchdog (SAMD51 WDT, ~16s)
// rpcWiFi/RTL8720 が稀にループをブロックしてハングする (実運用構成でも発生)。
// 一定時間 loop が回らないと WDT が自動リセットし、再起動→再接続で自己復帰する。
// ============================================================================
#define WDT_PET() (WDT->CLEAR.reg = WDT_CLEAR_CLEAR_KEY)

// ============================================================================
// Constants
// ============================================================================
#define I2C_SLAVE_ADDR 0x08
#define WIO_VERSION "LOCAL"

// OTA
#define OTA_LISTEN_PORT 41000
#define OTA_CMD_BYTE    0x30
#define OTA_HTTP_TIMEOUT_MS 60000

// I2C Commands (Master -> Slave)
#define CMD_UPDATE_STATUS   0x01
#define CMD_SET_ROBOT_ID    0x02
#define CMD_UPDATE_EMO      0x03
#define CMD_FULL_REFRESH    0x05
#define CMD_UPDATE_NETWORK  0x06  // SSID and version (lightweight)
#define CMD_OTA_PROGRESS    0x07  // OTA 進行状況 (payload: [source, phase, percent])
// CMD_OTA_PROGRESS payload[0] = source (どの基板の OTA か)
#define OTA_SRC_XIAO        0     // Xiao(C5) 自身の OTA
#define OTA_SRC_POWERBOARD  1     // 電源基板の OTA (C5 が CAN 0x318 を中継)
// CMD_OTA_PROGRESS payload[1] = phase
#define OTA_PHASE_START     0     // 接続中
#define OTA_PHASE_DOWNLOAD  1     // ダウンロード中 (payload[2] = percent 0-100)
#define OTA_PHASE_APPLY     2     // 書込/適用中
#define OTA_PHASE_DONE      3     // 完了 (再起動)
#define OTA_PHASE_FAIL      4     // 失敗

// I2C Commands (Slave -> Master)
#define CMD_ID_CONFIRM       0x81
// 0x82 (CMD_EMO_TOGGLE) removed: manual EMO is no longer supported
#define CMD_READY            0x83  // WioTerminal ready notification
// 0x84 (CMD_ENTER_MANUAL) deprecated: 旧 5-way 起動押下用専用イベント。
//     現在は CMD_BOOT_BUTTONS (0x86) で 5-way ビットを送る。
//     C5 側は古い Wio との互換のため受信ハンドラを残してある。
// 0x85 (CMD_BOOT_IGNORE_PROT) removed: 専用イベントから bitmap 形式 (0x86) に統一
#define CMD_BOOT_BUTTONS     0x86  // 起動時に押されていたボタン bitmap (1byte payload)
#define CMD_BTN_LONGPRESS    0x87  // 通常動作中の長押し確定通知 bitmap (1byte payload)

// Button bitmap (CMD_BOOT_BUTTONS / CMD_BTN_LONGPRESS の payload)
// C5 側と同じ定義を共有する
#define BTN_KEY_A            0x01
#define BTN_KEY_B            0x02
#define BTN_KEY_C            0x04
#define BTN_5WAY_PRESS       0x08
// 0x10-0x80 reserved (5-way UP/DOWN/LEFT/RIGHT 等の将来拡張用)

// 長押し判定スレッショルド
#define LONGPRESS_THRESHOLD_MS  1000

// Button IDs (legacy, 現状未使用)
#define BTN_ID_KEY_A        0x01
#define BTN_ID_KEY_B        0x02
#define BTN_ID_KEY_C        0x03
// 0x04 (BTN_ID_5WAY) removed: 旧 manual EMO トグル用ID、現状未使用

// ============================================================================
// State Structure
// ============================================================================
struct DisplayState {
    // ID management
    uint8_t robotId;      // Confirmed ID (from Master)
    uint8_t setId;        // Setting ID (local, changed by buttons)

    // WiFi status
    bool wifiConnected;
    bool mdnsEnabled;
    uint8_t ipAddress[4];
    uint16_t listenPort;

    // EMO status (manual EMO removed; only remote remains)
    bool remoteEMO;

    // 動作モード (起動時 5-way 押下で MANUAL に遷移、ローカル管理)
    bool manualMode;

    // PowerBoard status
    uint8_t stopReason;
    bool powerBoardConnected;
    uint8_t powerStatus;  // 0:Stop, 1:Standby, 2:Drive

    // Xiao communication status
    bool xiaoConnected;
    unsigned long lastXiaoUpdate;

    // Strings
    char ssid[33];
    char espVersion[16];

    // Dirty flag for display update
    bool needsUpdate;
};

// ============================================================================
// Global Variables
// ============================================================================
TFT_eSPI tft;
DisplayState state;

// Button state tracking
// prev_pressed: 前回ループでの押下状態 (true = 押下)
// longpress_fired: 現在の押下サイクルで CMD_BTN_LONGPRESS を発行済みか
// press_start_ms: 押下が始まった時刻
struct ButtonTrack {
    bool prev_pressed;
    bool longpress_fired;
    uint32_t press_start_ms;
};
ButtonTrack btnA  = {false, false, 0};
ButtonTrack btnB  = {false, false, 0};
ButtonTrack btnC  = {false, false, 0};
ButtonTrack btn5W = {false, false, 0};

// I2C receive buffer
// I2C receive ring buffer.
// 旧実装は単一バッファ + i2cDataReady フラグで、メインループが1パケット処理する前に
// 次パケットが届くと overrun (取りこぼし) していた。特に Xiao の READY 再送バースト
// (status/network/robotid/emo を連続送信) を捌けず、xiao disconnected → READY 要求 →
// 再びバースト… の悪循環に陥っていた。数段のリングで吸収する。
#define I2C_RX_SLOTS     8
#define I2C_RX_SLOT_SIZE 40   // CMD+LEN+payload(<=32) に余裕
typedef struct {
    uint8_t len;
    uint8_t data[I2C_RX_SLOT_SIZE];
} I2cRxPacket;
volatile I2cRxPacket i2cRxRing[I2C_RX_SLOTS];
volatile uint8_t i2cRxHead = 0;     // receiveEvent (ISR) が書く
volatile uint8_t i2cRxTail = 0;     // processI2CCommands (loop) が読む
volatile uint8_t i2cRxDropped = 0;  // リング満杯で破棄した数 (loop でログ)

// Event queue for Master
volatile uint8_t eventQueue[16];
volatile uint8_t eventQueueHead = 0;
volatile uint8_t eventQueueTail = 0;

// OTA / WiFi
WiFiUDP otaUdp;
bool wifiConnected = false;
bool otaUdpListening = false;
volatile bool otaInProgress = false;
unsigned long lastWifiAttemptMs = 0;
char otaUdpBuffer[256];
char wioOwnIp[16] = "0.0.0.0";       // Wio 自身の WiFi IP (GOT_IP で更新, heartbeat で出力)
unsigned long lastWifiHbMs = 0;       // WiFi ハートビート出力時刻
unsigned long splashUntilMs = 0;      // この時刻まではスプラッシュ画面を保持 (updateDisplay スキップ)

// OTA 進捗表示 (CMD_OTA_PROGRESS)。進行中は通常表示・切断判定を抑制する。
#define XIAO_OTA_TIMEOUT_MS 90000  // 取りこぼし時の保険 (最終フォールバック)
#define XIAO_OTA_FINAL_MS    4000  // DONE/FAIL 表示後、通常表示へ戻すまで
bool xiaoOtaActive = false;
uint8_t xiaoOtaSource = OTA_SRC_XIAO;  // 表示中の OTA の対象 (Xiao / PowerBoard)
uint8_t xiaoOtaPhase = OTA_PHASE_START;
unsigned long xiaoOtaLastMs = 0;

// mDNS (wio{robotId}.local)
char wioMdnsName[16];
bool wioMdnsStarted = false;
uint8_t wioMdnsRobotId = 0xFF;

// ============================================================================
// I2C Slave Callbacks
// ============================================================================
void receiveEvent(int howMany) {
    if (howMany < 2) return;  // Need at least CMD + LEN

    uint8_t next = (uint8_t)((i2cRxHead + 1) % I2C_RX_SLOTS);
    if (next == i2cRxTail) {
        // ring full: drop this packet (loop が追いついていない)。ISR では serial を
        // 触らず、カウンタだけ上げて processI2CCommands でまとめてログする。
        if (i2cRxDropped < 255) i2cRxDropped++;
        while (Wire.available()) Wire.read();
        return;
    }

    volatile I2cRxPacket* slot = &i2cRxRing[i2cRxHead];
    uint8_t n = 0;
    while (Wire.available() && n < I2C_RX_SLOT_SIZE) {
        slot->data[n++] = Wire.read();
    }
    while (Wire.available()) Wire.read();  // 余剰を捨てる (スロット長超過時)
    slot->len = n;
    i2cRxHead = next;
}

void requestEvent() {
    // Send pending event to Master
    if (eventQueueHead != eventQueueTail) {
        uint8_t cmd = eventQueue[eventQueueTail];
        uint8_t dataLen = eventQueue[(eventQueueTail + 1) % sizeof(eventQueue)];
        if (dataLen > 32) dataLen = 32;  // 防御的 clamp: buffer[34] (CMD+LEN+32) 溢れ防止
        uint8_t totalLen = 2 + dataLen;  // CMD + LEN + DATA

        // Build buffer to send
        uint8_t buffer[34];  // Max: CMD(1) + LEN(1) + DATA(32)
        buffer[0] = cmd;
        buffer[1] = dataLen;
        for (uint8_t i = 0; i < dataLen; i++) {
            buffer[2 + i] = eventQueue[(eventQueueTail + 2 + i) % sizeof(eventQueue)];
        }

        Wire.write(buffer, totalLen);

        // Advance tail
        eventQueueTail = (eventQueueTail + totalLen) % sizeof(eventQueue);
    }
}

// ============================================================================
// Event Queue Functions
// ============================================================================
void queueEvent(uint8_t cmd, uint8_t dataLen, const uint8_t* data) {
    if (dataLen > 32) dataLen = 32;          // payload 上限 (プロトコル/バッファ上限)
    uint8_t totalLen = 2 + dataLen;          // CMD + LEN + DATA

    // requestEvent() は I2C onRequest ISR で head/tail/配列を読むため、書込中の割込で
    // 半端なイベントが master へ出ないよう短いクリティカルセクションで保護する。
    noInterrupts();
    // 空き容量で満杯判定する (旧実装の「tail にぴったり着地」判定は tail を跨ぐ上書きを
    // 検出できず既存イベントを破壊し得た)。head==tail を空とする運用なので最大 size-1。
    uint8_t used = (uint8_t)((eventQueueHead - eventQueueTail + sizeof(eventQueue)) % sizeof(eventQueue));
    uint8_t freeSpace = (uint8_t)(sizeof(eventQueue) - 1 - used);
    if (totalLen > freeSpace) {
        interrupts();
        return;  // Queue full → drop (上書きせず破棄)
    }

    eventQueue[eventQueueHead] = cmd;
    eventQueue[(eventQueueHead + 1) % sizeof(eventQueue)] = dataLen;
    for (uint8_t i = 0; i < dataLen; i++) {
        eventQueue[(eventQueueHead + 2 + i) % sizeof(eventQueue)] = data[i];
    }
    eventQueueHead = (eventQueueHead + totalLen) % sizeof(eventQueue);
    interrupts();
}

// ============================================================================
// Helper: Check if state changed
// ============================================================================
// Get power status string from status byte
const char* getPowerStatusText(const DisplayState& s) {
    if (!s.powerBoardConnected) return "No Response";
    switch (s.powerStatus) {
        case 0: return "STOP";
        case 1: return "STANDBY";
        case 2: return "DRIVE";
        default: return "UNKNOWN";
    }
}

// Get stop reason string from stopReason byte
const char* getStopReasonText(uint8_t stopReason) {
    if (stopReason == 0) return "";
    if (stopReason & 0x10) return "LOW_BAT";
    if (stopReason & 0x08) return "OVERCUR";
    if (stopReason & 0x04) return "REMOTE";
    if (stopReason & 0x02) return "LOCAL";
    if (stopReason & 0x01) return "MAIN";
    return "UNKNOWN";
}

bool hasStateChanged(const DisplayState& current, const DisplayState& prev) {
    if (current.wifiConnected != prev.wifiConnected) return true;
    if (current.mdnsEnabled != prev.mdnsEnabled) return true;
    if (current.robotId != prev.robotId) return true;
    if (current.setId != prev.setId) return true;
    if (current.remoteEMO != prev.remoteEMO) return true;
    if (current.listenPort != prev.listenPort) return true;
    if (current.powerBoardConnected != prev.powerBoardConnected) return true;
    if (current.stopReason != prev.stopReason) return true;
    if (current.powerStatus != prev.powerStatus) return true;
    if (current.xiaoConnected != prev.xiaoConnected) return true;
    if (current.manualMode != prev.manualMode) return true;
    for (int i = 0; i < 4; i++) {
        if (current.ipAddress[i] != prev.ipAddress[i]) return true;
    }
    if (strcmp(current.ssid, prev.ssid) != 0) return true;
    if (strcmp(current.espVersion, prev.espVersion) != 0) return true;
    return false;
}

// ============================================================================
// Command Handlers
// ============================================================================
void handleUpdateStatus(const uint8_t* data, uint8_t len) {
    if (len < 10) return;

    // Save previous state for comparison
    DisplayState prev = state;

    uint8_t status = data[0];
    state.wifiConnected = (status & 0x01) != 0;
    state.mdnsEnabled = (status & 0x02) != 0;
    state.powerBoardConnected = (status & 0x04) != 0;

    state.ipAddress[0] = data[1];
    state.ipAddress[1] = data[2];
    state.ipAddress[2] = data[3];
    state.ipAddress[3] = data[4];

    state.listenPort = (data[5] << 8) | data[6];
    state.stopReason = data[7];

    // Power Status (0:Stop, 1:Standby, 2:Drive)
    state.powerStatus = data[8];
    // data[9] reserved

    // Mark Xiao as connected
    state.xiaoConnected = true;
    state.lastXiaoUpdate = millis();

    Log.printf("UPDATE_STATUS: wifi=%d xiao=%d IP=%d.%d.%d.%d stop=0x%02X status=%d\n",
        state.wifiConnected, state.xiaoConnected,
        state.ipAddress[0], state.ipAddress[1], state.ipAddress[2], state.ipAddress[3],
        state.stopReason, state.powerStatus);

    // Only update if state changed
    if (hasStateChanged(state, prev)) {
        state.needsUpdate = true;
    }
}

void handleSetRobotId(const uint8_t* data, uint8_t len) {
    if (len < 1) return;

    Log.printf("SET_ROBOT_ID: id=%d (prev=%d)\n", data[0], state.robotId);

    // Only update if robotId changed
    if (state.robotId != data[0]) {
        state.robotId = data[0];
        state.setId = data[0];  // Sync setId with confirmed robotId
        state.needsUpdate = true;
    }
}

void handleUpdateEmo(const uint8_t* data, uint8_t len) {
    if (len < 1) return;

    bool newRemote = (data[0] != 0);

    Log.printf("UPDATE_EMO: remote=%d\n", newRemote);

    // Only update if EMO state changed
    if (state.remoteEMO != newRemote) {
        state.remoteEMO = newRemote;
        state.needsUpdate = true;
    }
}

void handleFullRefresh(const uint8_t* data, uint8_t len) {
    if (len < 11) return;

    // Save previous state for comparison
    DisplayState prev = state;

    // Parse fixed fields
    uint8_t status = data[0];
    state.wifiConnected = (status & 0x01) != 0;
    state.mdnsEnabled = (status & 0x02) != 0;
    state.powerBoardConnected = (status & 0x04) != 0;

    state.ipAddress[0] = data[1];
    state.ipAddress[1] = data[2];
    state.ipAddress[2] = data[3];
    state.ipAddress[3] = data[4];

    state.listenPort = (data[5] << 8) | data[6];
    state.robotId = data[7];
    state.setId = data[7];
    state.remoteEMO = (data[8] != 0);
    state.stopReason = data[9];

    // Mark Xiao as connected
    state.xiaoConnected = true;
    state.lastXiaoUpdate = millis();

    // Parse SSID
    uint8_t ssidLen = data[10];
    if (ssidLen > 32) ssidLen = 32;
    if (len >= 11 + ssidLen) {
        memcpy(state.ssid, &data[11], ssidLen);
        state.ssid[ssidLen] = '\0';
    }

    // Parse ESP version
    if (len >= 12 + ssidLen) {
        uint8_t verLen = data[11 + ssidLen];
        if (verLen > 15) verLen = 15;
        if (len >= 12 + ssidLen + verLen) {
            memcpy(state.espVersion, &data[12 + ssidLen], verLen);
            state.espVersion[verLen] = '\0';
        }
    }

    Log.printf("FULL_REFRESH: wifi=%d xiao=%d SSID=%s ver=%s\n",
        state.wifiConnected, state.xiaoConnected, state.ssid, state.espVersion);

    // Only update if state changed
    if (hasStateChanged(state, prev)) {
        state.needsUpdate = true;
    }
}

void handleUpdateNetwork(const uint8_t* data, uint8_t len) {
    if (len < 2) return;  // At least SSID len + version len

    // Save previous state for comparison
    DisplayState prev = state;

    uint8_t idx = 0;

    // Parse SSID
    uint8_t ssidLen = data[idx++];
    if (ssidLen > 32) ssidLen = 32;
    if (len >= 1 + ssidLen) {
        memcpy(state.ssid, &data[idx], ssidLen);
        state.ssid[ssidLen] = '\0';
        idx += ssidLen;
    }

    // Parse ESP version
    if (len >= 2 + ssidLen) {
        uint8_t verLen = data[idx++];
        if (verLen > 15) verLen = 15;
        if (len >= 2 + ssidLen + verLen) {
            memcpy(state.espVersion, &data[idx], verLen);
            state.espVersion[verLen] = '\0';
        }
    }

    // Mark Xiao as connected
    state.xiaoConnected = true;
    state.lastXiaoUpdate = millis();

    Log.printf("UPDATE_NETWORK: SSID=%s ver=%s\n", state.ssid, state.espVersion);

    // Only update if state changed
    if (hasStateChanged(state, prev)) {
        state.needsUpdate = true;
    }
}

// ============================================================================
// WiFi / OTA
// ============================================================================
static void otaShowMessage(const char* line1, const char* line2) {
    tft.fillScreen(TFT_BLACK);
    tft.setFreeFont(FMB12);
    tft.setTextColor(TFT_CYAN, TFT_BLACK);
    tft.drawString(line1, 20, 80);
    if (line2 != nullptr) {
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
        tft.drawString(line2, 20, 120);
    }
}

static void otaShowProgress(int written, int total) {
    char buf[64];
    int pct = (total > 0) ? (int)((int64_t)written * 100 / total) : 0;
    sprintf(buf, "%d / %d B (%d%%)", written, total, pct);
    tft.fillRect(20, 160, 280, 30, TFT_BLACK);
    tft.setFreeFont(FMB9);
    tft.setTextColor(TFT_YELLOW, TFT_BLACK);
    tft.drawString(buf, 20, 160);
}

// OTA 進捗画面。phase 変化時 (または新規セッション) のみ全描画し、
// DOWNLOAD 中はバーと % だけ更新してチラつきを防ぐ。source で見出しを切替える。
static void drawXiaoOtaScreen(uint8_t source, uint8_t phase, uint8_t pct, bool forceFull) {
    static int lastPhase = -1;
    const char* title = (source == OTA_SRC_POWERBOARD) ? "PowerBoard OTA" : "Xiao OTA";
    const char* sub;
    uint16_t subColor = TFT_WHITE;
    switch (phase) {
        case OTA_PHASE_START:    sub = "Connecting...";   break;
        case OTA_PHASE_DOWNLOAD: sub = "Downloading...";  break;
        case OTA_PHASE_APPLY:    sub = "Applying...";     break;
        case OTA_PHASE_DONE:     sub = "Done. Rebooting"; subColor = TFT_GREEN; break;
        case OTA_PHASE_FAIL:     sub = "FAILED";          subColor = TFT_RED;   break;
        default:                 sub = "";                break;
    }
    if (forceFull || (int)phase != lastPhase) {
        lastPhase = (int)phase;
        tft.fillScreen(TFT_BLACK);
        tft.setFreeFont(FMB12);
        tft.setTextColor(TFT_CYAN, TFT_BLACK);
        tft.drawString(title, 20, 50);
        tft.setTextColor(subColor, TFT_BLACK);
        tft.drawString(sub, 20, 95);
        if (phase == OTA_PHASE_DOWNLOAD) {
            tft.drawRect(20, 150, 280, 28, TFT_WHITE);  // バー枠
        }
    }
    if (phase == OTA_PHASE_DOWNLOAD) {
        if (pct > 100) pct = 100;
        int w = (276 * pct) / 100;
        tft.fillRect(22, 152, w, 24, TFT_GREEN);
        char buf[16];
        sprintf(buf, "%d%%", pct);
        tft.fillRect(20, 190, 140, 26, TFT_BLACK);
        tft.setFreeFont(FMB9);
        tft.setTextColor(TFT_YELLOW, TFT_BLACK);
        tft.drawString(buf, 20, 190);
    }
}

// CMD_OTA_PROGRESS: OTA 進捗を受信して画面表示。payload = [source, phase, percent]。
// (旧 2byte [phase, percent] も Xiao 扱いで受理)
void handleOtaProgress(uint8_t* data, uint8_t len) {
    uint8_t source, phase, pct;
    if (len >= 3) {
        source = data[0]; phase = data[1]; pct = data[2];
    } else if (len == 2) {
        source = OTA_SRC_XIAO; phase = data[0]; pct = data[1];
    } else {
        return;
    }
    bool newSession = !xiaoOtaActive || (source != xiaoOtaSource);  // 対象が変われば全描画
    xiaoOtaActive = true;
    xiaoOtaSource = source;
    xiaoOtaPhase  = phase;
    xiaoOtaLastMs = millis();
    drawXiaoOtaScreen(source, phase, pct, newSession);
}

void onWiFiEvent(WiFiEvent_t event) {
    switch (event) {
        case SYSTEM_EVENT_STA_GOT_IP:
            WiFi.localIP().toString().toCharArray(wioOwnIp, sizeof(wioOwnIp));
            // DHCP 取得結果を詳細にログ: IP が想定サブネット (192.168.4.x) か、gw/mask/dns が
            // 整合するか、接続 SSID/RSSI を確認する (誤サブネット/DHCP 失敗の切り分け用)。
            Log.printf("WiFi got IP: %s gw=%s mask=%s dns=%s rssi=%ld ssid=%s\n",
                       wioOwnIp,
                       WiFi.gatewayIP().toString().c_str(),
                       WiFi.subnetMask().toString().c_str(),
                       WiFi.dnsIP().toString().c_str(),
                       (long)WiFi.RSSI(),
                       WiFi.SSID().c_str());
            wifiConnected = true;
            break;
        case SYSTEM_EVENT_STA_DISCONNECTED:
            Log.println("WiFi disconnected");
            wifiConnected = false;
            otaUdpListening = false;
            break;
        default:
            break;
    }
}

// 切断理由コード (system_event_sta_disconnected_t.reason) を出力する診断用ハンドラ。
// フラップ原因の切り分け (例: 8=ASSOC_LEAVE, 200=BEACON_TIMEOUT, 15=4WAY_HANDSHAKE_TIMEOUT 等) に使う。
void onWiFiSysEvent(system_event_t* e) {
    if (e && e->event_id == SYSTEM_EVENT_STA_DISCONNECTED) {
        Log.printf("WiFi disconnect reason=%u\n", e->event_info.disconnected.reason);
    }
}

// === 非同期 WiFi 接続タスク ===
// rpcWiFi(RTL8720)の mode/disconnect/begin は RPC でブロックし、AP 不在時は WDT 上限
// (~16s)を超えてブロックし得る。これを loop で直接呼ぶと「接続待ちの間に loop が
// WDT_PET に戻れず WDT リセット → 再起動ループ (スプラッシュから進まない / OTA 不可)」に
// なる (実機で確認)。接続シーケンスを専用 FreeRTOS タスクへ逃がし、loop は wifiConnected
// (onWiFiEvent が更新) を参照するだけにする。loop は常時回って WDT を蹴れるので、AP 不在で
// begin が長くブロックしても誤リセットしない。rpcUnified は起動時に vTaskStartScheduler 済み
// なので xTaskCreate が使える。
static TaskHandle_t wifiTaskHandle = NULL;

static void wifiConnectOnce() {
    Log.println("WiFi: connecting...");
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);   // WiFi 省電力(modem sleep)無効化。間欠フラップ(beacon取りこぼし
                            // による定期切断)対策。常時受信で接続安定性を優先する。
    WiFi.disconnect(true);
    delay(1000);   // RTL8720 settle (参考 greentea-ssl/WioReceiveUDP は disconnect 後 1000ms 待つ)
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    WiFi.setAutoReconnect(true);   // ドロップ時は RTL8720 が自動再接続。フラップ時の復帰を高速化し
                                   // 手動 full 再接続 (disconnect+1s) の頻度を下げる。
}

static void wifiTask(void* pv) {
    WiFi.onEvent(onWiFiEvent);
    WiFi.onEvent(onWiFiSysEvent, SYSTEM_EVENT_STA_DISCONNECTED);  // 切断理由ログ(診断)
    wifiConnectOnce();  // 初回接続 (setSleep(false)+setAutoReconnect(true) を実施)
    for (;;) {
        if (otaInProgress) { vTaskDelay(pdMS_TO_TICKS(500)); continue; }
        if (wifiConnected) {
            vTaskDelay(pdMS_TO_TICKS(1000));  // 接続中アイドル。ドロップは auto-reconnect が高速復帰
        } else {
            // 切断: まず auto-reconnect の復帰を ~8s 待つ (フラップ毎の full 再接続を避ける)。
            // 戻らなければ手動で full 再接続 (disconnect+begin)。
            for (int i = 0; i < 80 && !wifiConnected && !otaInProgress; ++i) {
                vTaskDelay(pdMS_TO_TICKS(100));
            }
            if (!wifiConnected && !otaInProgress) {
                wifiConnectOnce();
                for (int i = 0; i < 150 && !wifiConnected && !otaInProgress; ++i) {
                    vTaskDelay(pdMS_TO_TICKS(100));
                }
                if (!wifiConnected) vTaskDelay(pdMS_TO_TICKS(3000));  // backoff
            }
        }
    }
}

void startWiFi() {
    // 接続シーケンスは wifiTask に委譲 (loop を一切ブロックしない)。一度だけ生成。
    if (wifiTaskHandle) return;
    xTaskCreate(wifiTask, "wifi", 4096, NULL, 1, &wifiTaskHandle);
}

void serviceWiFi() {
    if (otaInProgress) return;

    // wifiConnected は onWiFiEvent (GOT_IP / DISCONNECTED) でイベント駆動更新される。
    // 以前はここで毎ループ WiFi.status() を呼んでいたが、RTL8720 (rpcWiFi) が接続失敗後に
    // 当該 RPC でブロックするとメインループ全体が停止し、I2C 受信を引き取れず overrun 連発
    // (= xiao disconnected / PWR No Response) になっていた。毎ループのブロッキング RPC を
    // 廃止してループを止めないようにする (接続状態はイベントflagで把握)。

    if (wifiConnected && !otaUdpListening) {
        // Begin UDP listener once connected.
        // rpcWiFi(RTL8720) の WiFiUDP は port のみの begin(port) だと受信できないことがある。
        // 参考 greentea-ssl/WioReceiveUDP に倣い begin(localIP, port) で bind する。
        if (otaUdp.begin(WiFi.localIP(), OTA_LISTEN_PORT)) {
            Log.printf("OTA UDP listener started on %s:%d\n",
                       WiFi.localIP().toString().c_str(), OTA_LISTEN_PORT);
            otaUdpListening = true;
        } else {
            Log.println("OTA UDP begin failed");
        }
    }

    // 未接続時の再接続は wifiTask が backoff 付きで担当する (loop をブロックしないため、
    // ここでは begin を呼ばない)。
}

// Maintain wio{robotId}.local mDNS name. Re-register if robotId changes.
void serviceMdns() {
    if (otaInProgress) return;
    if (!wifiConnected) return;
    if (state.robotId == 0xFF) return;  // robotId not yet confirmed

    if (!wioMdnsStarted || state.robotId != wioMdnsRobotId) {
        if (wioMdnsStarted) {
            MDNS.end();
            wioMdnsStarted = false;
        }
        sprintf(wioMdnsName, "wio%d", state.robotId);
        if (MDNS.begin(wioMdnsName)) {
            Log.printf("mDNS started: %s.local\n", wioMdnsName);
            wioMdnsStarted = true;
            wioMdnsRobotId = state.robotId;
        } else {
            Log.println("mDNS begin failed");
        }
    }
}

void performOTA(const char* url) {
    Log.printf("[OTA] Starting from: %s\n", url);
    otaShowMessage("OTA Update", "Connecting...");

    // Stop I2C to avoid bus contention during long-running download
    Wire.end();
    Log.println("[OTA] I2C stopped");

    HTTPClient http;
    http.setTimeout(OTA_HTTP_TIMEOUT_MS);

    if (!http.begin(url)) {
        Log.println("[OTA] http.begin failed");
        otaShowMessage("OTA Failed", "http.begin");
        delay(3000);
        NVIC_SystemReset();
        return;
    }

    int httpCode = http.GET();
    if (httpCode != HTTP_CODE_OK) {
        Log.printf("[OTA] HTTP GET failed: %d\n", httpCode);
        otaShowMessage("OTA Failed", "HTTP GET");
        http.end();
        delay(3000);
        NVIC_SystemReset();
        return;
    }

    int contentLength = http.getSize();
    Log.printf("[OTA] Content-Length: %d\n", contentLength);

    if (contentLength <= 0) {
        Log.println("[OTA] Invalid content length");
        otaShowMessage("OTA Failed", "Bad length");
        http.end();
        delay(3000);
        NVIC_SystemReset();
        return;
    }

    if (!InternalStorage.open(contentLength)) {
        Log.println("[OTA] InternalStorage.open failed");
        otaShowMessage("OTA Failed", "Open storage");
        http.end();
        delay(3000);
        NVIC_SystemReset();
        return;
    }

    otaShowMessage("OTA Update", "Downloading...");

    WiFiClient* stream = http.getStreamPtr();
    int written = 0;
    unsigned long lastProgressMs = 0;
    unsigned long lastDataMs = millis();
    uint8_t buf[512];

    while (http.connected() && (contentLength < 0 || written < contentLength)) {
        WDT_PET();   // ダウンロード中も WDT を蹴り続ける (長時間 DL での誤リセット防止)
        size_t avail = stream->available();
        if (avail) {
            size_t toRead = avail > sizeof(buf) ? sizeof(buf) : avail;
            int n = stream->readBytes(buf, toRead);
            for (int i = 0; i < n; i++) {
                InternalStorage.write(buf[i]);
            }
            written += n;
            lastDataMs = millis();

            if (millis() - lastProgressMs > 250) {
                lastProgressMs = millis();
                otaShowProgress(written, contentLength);
                Log.printf("[OTA] %d / %d\n", written, contentLength);
            }
        } else {
            if (millis() - lastDataMs > 15000) {
                Log.println("[OTA] Stream timeout");
                break;
            }
            delay(1);
        }
    }

    http.end();
    InternalStorage.close();

    Log.printf("[OTA] Total written: %d / %d\n", written, contentLength);

    if (written != contentLength) {
        Log.println("[OTA] Size mismatch - aborting");
        otaShowMessage("OTA Failed", "Size mismatch");
        delay(3000);
        NVIC_SystemReset();
        return;
    }

    otaShowMessage("OTA Success", "Applying...");
    delay(500);
    Log.println("[OTA] Applying update and rebooting");
    Log.flush();
    InternalStorage.apply();  // does not return
}

void handleOtaCommand(const char* url) {
    if (otaInProgress) {
        Log.println("[OTA] Already in progress");
        return;
    }
    otaInProgress = true;
    performOTA(url);
    // performOTA does not return on success; on failure it resets
}

void serviceOtaUdp() {
    if (!otaUdpListening || otaInProgress) return;

    int sz = otaUdp.parsePacket();
    if (sz <= 0) return;

    int len = otaUdp.read(otaUdpBuffer, sizeof(otaUdpBuffer) - 1);
    if (len <= 0) return;
    otaUdpBuffer[len] = '\0';

    if ((uint8_t)otaUdpBuffer[0] != OTA_CMD_BYTE || len < 2) {
        Log.printf("[OTA] Ignored packet (len=%d, cmd=0x%02X)\n",
                      len, (uint8_t)otaUdpBuffer[0]);
        return;
    }

    Log.println("========================================");
    Log.println("[OTA] Command received");
    Log.printf("  URL: %s\n", &otaUdpBuffer[1]);
    Log.println("========================================");

    handleOtaCommand(&otaUdpBuffer[1]);
}

void processI2CCommands() {
    if (i2cRxDropped) {
        uint8_t d = i2cRxDropped;
        i2cRxDropped = 0;
        Log.printf("WARNING: I2C ring full - %u packet(s) dropped\n", d);
    }

    // リング内の保留パケットを「すべて」処理する (バーストを1ループで吸収)。
    while (i2cRxTail != i2cRxHead) {
        // ISR が触らない tail スロットをローカルへコピーしてから dispatch。
        uint8_t buf[I2C_RX_SLOT_SIZE];
        volatile I2cRxPacket* slot = &i2cRxRing[i2cRxTail];
        uint8_t total = slot->len;
        if (total > I2C_RX_SLOT_SIZE) total = I2C_RX_SLOT_SIZE;
        for (uint8_t i = 0; i < total; i++) buf[i] = slot->data[i];
        i2cRxTail = (uint8_t)((i2cRxTail + 1) % I2C_RX_SLOTS);

        if (total < 2) continue;
        uint8_t cmd = buf[0];
        uint8_t len = buf[1];

        Log.printf("I2C received: cmd=0x%02X len=%d\n", cmd, len);

        if (total < (uint8_t)(2 + len)) continue;

        // Xiao 自身の OTA 中に C5 が通常コマンドを再開 (=再起動完了) したら進捗画面を終了。
        // PowerBoard OTA 中は C5 が status を出し続けるため、ここでは解除しない (DONE/FAIL or timeout で解除)。
        if (cmd != CMD_OTA_PROGRESS && xiaoOtaActive && xiaoOtaSource == OTA_SRC_XIAO) {
            xiaoOtaActive = false;
            state.needsUpdate = true;
        }

        switch (cmd) {
            case CMD_UPDATE_STATUS:
                handleUpdateStatus(&buf[2], len);
                break;
            case CMD_OTA_PROGRESS:
                handleOtaProgress(&buf[2], len);
                break;
            case CMD_SET_ROBOT_ID:
                handleSetRobotId(&buf[2], len);
                break;
            case CMD_UPDATE_EMO:
                handleUpdateEmo(&buf[2], len);
                break;
            case CMD_FULL_REFRESH:
                handleFullRefresh(&buf[2], len);
                break;
            case CMD_UPDATE_NETWORK:
                handleUpdateNetwork(&buf[2], len);
                break;
        }
    }
}

// ============================================================================
// Display Functions
// ============================================================================
void updateDisplay() {
    if (millis() < splashUntilMs) return;  // スプラッシュ保持中は通常表示を描画しない
    if (!state.needsUpdate) return;
    state.needsUpdate = false;

    tft.fillScreen(TFT_BLACK);

    // Line 1: ID setting
    tft.setFreeFont(FMB12);
    tft.setTextColor(TFT_YELLOW, TFT_BLACK);
    char buf[64];
    sprintf(buf, "set  +    -  sel: %d", state.setId);
    tft.drawString(buf, 20, 10);

    // Line 2: SSID or Xiao Disconnected
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    if (!state.xiaoConnected) {
        // Xiao communication not established
        tft.setTextColor(TFT_RED, TFT_BLACK);
        tft.drawString("Xiao Disconnected", 20, 40);
    } else {
        sprintf(buf, "SSID: %s", state.ssid);
        tft.drawString(buf, 20, 40);
    }

    // Line 3: EMO status (remote only)
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.drawString("EMO:", 20, 66);

    if (state.remoteEMO) {
        tft.setTextColor(TFT_WHITE, TFT_RED);
        tft.drawString("Remote", 100, 66);
    } else {
        tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
        tft.drawString("Remote", 100, 66);
    }

    // Line 4: IP Address or disconnected
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    if (state.wifiConnected) {
        sprintf(buf, "IP: %d.%d.%d.%d",
                state.ipAddress[0], state.ipAddress[1],
                state.ipAddress[2], state.ipAddress[3]);
        tft.drawString(buf, 20, 92);
    } else {
        tft.setTextColor(TFT_RED, TFT_BLACK);
        tft.drawString("IP: disconnected", 20, 92);
    }

    // Line 5: Listen Port
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    sprintf(buf, "Listen Port: %d", state.listenPort);
    tft.drawString(buf, 20, 118);

    // Line 6: Robot ID (+ MANUAL 表示)
    if (state.robotId == 0xFF) {
        sprintf(buf, "Robot ID: ---");
    } else {
        sprintf(buf, "Robot ID: %d", state.robotId);
    }
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.drawString(buf, 20, 144);
    if (state.manualMode) {
        // Robot ID 文字列の右側に橙背景で MANUAL を表示
        int x = 20 + tft.textWidth(buf) + 10;
        tft.setTextColor(TFT_BLACK, TFT_ORANGE);
        tft.drawString(" MANUAL ", x, 144);
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
    }

    // Line 7: PowerBoard status and Stop Reason
    const char* pwrStatus = getPowerStatusText(state);
    if (state.powerBoardConnected) {
        if (state.stopReason == 0) {
            // STANDBY or DRIVE
            if (state.powerStatus == 2) {  // DRIVE
                tft.setTextColor(TFT_CYAN, TFT_BLACK);  // Cyan for DRIVE
            } else if (state.powerStatus == 1) {  // STANDBY
                tft.setTextColor(TFT_GREEN, TFT_BLACK);  // Green for STANDBY
            } else {
                tft.setTextColor(TFT_YELLOW, TFT_BLACK);  // Yellow for STOP
            }
            sprintf(buf, "PWR: %s", pwrStatus);
            tft.drawString(buf, 20, 170);
        } else {
            tft.setTextColor(TFT_WHITE, TFT_RED);
            sprintf(buf, "PWR: %s %s", pwrStatus, getStopReasonText(state.stopReason));
            tft.drawString(buf, 20, 170);
        }
    } else {
        tft.setTextColor(TFT_ORANGE, TFT_BLACK);
        tft.drawString("PWR: No Response", 20, 170);
    }

    // Line 8-10: Version info (3 lines)
    tft.setFreeFont(FMB9);
    tft.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
    sprintf(buf, "ESP: %s", state.espVersion);
    tft.drawString(buf, 20, 196);
    sprintf(buf, "WIO: %s", WIO_VERSION);
    tft.drawString(buf, 20, 210);
    tft.drawString("", 20, 224);
}

// ============================================================================
// Button Handling
// ============================================================================
// 1ボタン分の状態更新。
// - 押下立ち上がり (rising edge of pressed) で onShortPress を呼ぶ (NULL なら何もしない)
// - 押下継続が LONGPRESS_THRESHOLD_MS 超えた最初のループで longpressBitmap に mask を立てる
// - 同一押下サイクルで長押しは1回のみ
// - 短押しと長押しは同一押下中に両方発火し得る (短押し先, 長押し後)。
//   現状の用途では衝突しない (KEY_A/B/C 短押し→ID, 5-way 長押し→ローカル停止)。
static inline void updateButton(int pin, ButtonTrack &t, uint8_t mask,
                                uint8_t &longpressBitmap,
                                void (*onShortPress)()) {
    bool pressed = (digitalRead(pin) == LOW);
    uint32_t now = millis();

    // 立ち上がり (押下開始)
    if (pressed && !t.prev_pressed) {
        t.press_start_ms = now;
        t.longpress_fired = false;
        if (onShortPress) onShortPress();
    }
    // 押下継続中の長押し判定 (1回のみ)
    if (pressed && t.prev_pressed && !t.longpress_fired &&
        (now - t.press_start_ms >= LONGPRESS_THRESHOLD_MS)) {
        t.longpress_fired = true;
        longpressBitmap |= mask;
    }
    t.prev_pressed = pressed;
}

static void onKeyA_Short() {
    // ID--
    state.setId = (state.setId > 0) ? state.setId - 1 : 255;
    state.needsUpdate = true;
}
static void onKeyB_Short() {
    // ID++
    state.setId = (state.setId < 255) ? state.setId + 1 : 0;
    state.needsUpdate = true;
}
static void onKeyC_Short() {
    // ID Confirm -> Master
    uint8_t data[1] = { state.setId };
    queueEvent(CMD_ID_CONFIRM, 1, data);
    Log.printf("ID Confirm: %d\n", state.setId);
}

void handleButtons() {
    uint8_t longpressBitmap = 0;

    updateButton(WIO_KEY_A,    btnA,  BTN_KEY_A,      longpressBitmap, onKeyA_Short);
    updateButton(WIO_KEY_B,    btnB,  BTN_KEY_B,      longpressBitmap, onKeyB_Short);
    updateButton(WIO_KEY_C,    btnC,  BTN_KEY_C,      longpressBitmap, onKeyC_Short);
    // 5-way 押下は通常時に短押しアクションを持たない (起動時のみ用途)
    updateButton(WIO_5S_PRESS, btn5W, BTN_5WAY_PRESS, longpressBitmap, NULL);

    if (longpressBitmap != 0) {
        Log.printf("Long-press: 0x%02X -> queue CMD_BTN_LONGPRESS\n", longpressBitmap);
        queueEvent(CMD_BTN_LONGPRESS, 1, &longpressBitmap);
    }
}

// ============================================================================
// Setup and Loop
// ============================================================================
void setup() {
    // === Phase 1: I2C Slave を最優先で起動 ===
    // ESP32C5 は WioDisplay の起動を待たずに I2C 通信を始める可能性があるため、
    // state とコールバックを最初に整えてから Wire.begin() し、READY をキューする。
    memset(&state, 0xFF, sizeof(state));  // Set all to 0xFF (invalid)
    state.setId = 0;
    state.xiaoConnected = false;
    state.lastXiaoUpdate = 0;
    strcpy(state.espVersion, "---");
    strcpy(state.ssid, "---");
    state.needsUpdate = true;

    Wire.begin(I2C_SLAVE_ADDR);
    Wire.onReceive(receiveEvent);
    Wire.onRequest(requestEvent);
    queueEvent(CMD_READY, 0, NULL);  // 次の master request で送出される

    // === Phase 2: ボタン・シリアル (軽量、I2C 受信に影響しない) ===
    pinMode(WIO_KEY_A, INPUT_PULLUP);
    pinMode(WIO_KEY_B, INPUT_PULLUP);
    pinMode(WIO_KEY_C, INPUT_PULLUP);
    pinMode(WIO_5S_PRESS, INPUT_PULLUP);

    Serial.begin(115200);

    // デバッグ UART (D0=TX / D1=RX, SERCOM4) を起動。
    // Uart::begin は g_APinDescription の型 (D0/D1 は PIO_ANALOG) でピンを設定するため、
    // begin 後に SERCOM4 機能 (mux D = PIO_SERCOM_ALT) へ明示的に再割当てする。
    Serial3.begin(115200);                  // sercom4 を UART として設定 (送信は dbgUartWriteByte でポーリング)
    pinPeripheral(D0, PIO_SERCOM_ALT);  // PB08 -> SERCOM4/PAD0 (TX)
    pinPeripheral(D1, PIO_SERCOM_ALT);  // PB09 -> SERCOM4/PAD1 (RX, 未使用)
    // 送信はポーリングのため SERCOM4 割り込みは不要。むしろ RXC 割り込みが立つと
    // core(Wire.cpp) の SERCOM4 ハンドラ(Wire1 用)が走るため、NVIC で無効化しておく。
    NVIC_DisableIRQ(SERCOM4_0_IRQn);
    NVIC_DisableIRQ(SERCOM4_1_IRQn);
    NVIC_DisableIRQ(SERCOM4_2_IRQn);
    NVIC_DisableIRQ(SERCOM4_3_IRQn);

    Log.printf("\n=== WioDisplay Ready === I2C Slave: 0x%02X\n", I2C_SLAVE_ADDR);

    // === Phase 2.5: 起動時押下ボタン検知 ===
    // 50ms 連続 LOW (押下) なら確定。bitmap で C5 へ通知し、ボタンの意味付けは C5 側で行う。
    // ボタンの意味付けを Wio に持たせない事で、新しい起動チョードを追加する際に
    // C5 のみ更新すれば済むようになる (Wio 旧 fw のままでも C5 は新しい解釈ができる)。
    bool a_held = true, b_held = true, c_held = true, w_held = true;
    for (int i = 0; i < 10; i++) {
        if (digitalRead(WIO_KEY_A)    != LOW) a_held = false;
        if (digitalRead(WIO_KEY_B)    != LOW) b_held = false;
        if (digitalRead(WIO_KEY_C)    != LOW) c_held = false;
        if (digitalRead(WIO_5S_PRESS) != LOW) w_held = false;
        delay(5);
    }
    uint8_t bootBtns = 0;
    if (a_held) bootBtns |= BTN_KEY_A;
    if (b_held) bootBtns |= BTN_KEY_B;
    if (c_held) bootBtns |= BTN_KEY_C;
    if (w_held) bootBtns |= BTN_5WAY_PRESS;

    if (bootBtns != 0) {
        Log.printf("Boot buttons: 0x%02X -> queue CMD_BOOT_BUTTONS\n", bootBtns);
        queueEvent(CMD_BOOT_BUTTONS, 1, &bootBtns);
    }

    // 起動時押下中だったボタンは「既に押下サイクル中」として登録しておく。
    // - prev_pressed=true により短押し falling-edge 発火を抑止
    // - longpress_fired=true により当該サイクル中の長押し再発火を抑止
    // ユーザがリリースして再度押下すれば通常動作 (短押し / 長押し) が走る。
    {
        uint32_t now0 = millis();
        if (a_held) { btnA.prev_pressed  = true; btnA.longpress_fired  = true; btnA.press_start_ms  = now0; }
        if (b_held) { btnB.prev_pressed  = true; btnB.longpress_fired  = true; btnB.press_start_ms  = now0; }
        if (c_held) { btnC.prev_pressed  = true; btnC.longpress_fired  = true; btnC.press_start_ms  = now0; }
        if (w_held) { btn5W.prev_pressed = true; btn5W.longpress_fired = true; btn5W.press_start_ms = now0; }
    }

    // 表示用ローカル判定 (manualMode は state にも残し、loop() の表示更新で使う)
    state.manualMode = w_held;

    // === Phase 3: TFT 初期化 + スタートアップ表示 ===
    tft.begin();
    tft.setRotation(3);
    tft.fillScreen(TFT_BLACK);
    tft.setFreeFont(FMB12);
    tft.setTextColor(TFT_CYAN, TFT_BLACK);
    tft.drawString("WioDisplay Starting...", 20, 100);
    tft.setFreeFont(FMB9);
    tft.drawString("WIO: " + String(WIO_VERSION), 20, 130);
    if (bootBtns & BTN_5WAY_PRESS) {
        tft.setTextColor(TFT_ORANGE, TFT_BLACK);
        tft.drawString("MANUAL MODE", 20, 155);
    }
    if (bootBtns & BTN_KEY_C) {
        tft.setTextColor(TFT_RED, TFT_BLACK);
        tft.drawString("IGNORE OC/OD", 20, 180);
    }
    // スプラッシュ画面を ~1 秒保持してから通常表示へ切替える (updateDisplay をこの間スキップ)。
    // delay は使わず非ブロッキングにし、保持中も loop は I2C を処理する (取りこぼし防止)。
    splashUntilMs = millis() + 1000;

    // === Phase 4: WiFi は loop() 初回で遅延起動 ===
    // rpcWiFi の begin() は RTL8720DN への RPC で数百ms〜数秒ブロックするため
    // setup から外す。I2C は割り込み駆動なので WiFi 初期化中も応答可能。

    // === Phase 5: ウォッチドッグ起動 (~16s) ===
    // 以後 loop が ~16s 回らない (rpcWiFi ハング等) と自動リセットして自己復帰する。
    WDT->CTRLA.reg = 0;
    while (WDT->SYNCBUSY.reg) {}
    WDT->CONFIG.reg = WDT_CONFIG_PER_CYC16384;  // 16384 / 1.024kHz ≒ 16s
    WDT->EWCTRL.reg = 0;
    WDT->CTRLA.reg = WDT_CTRLA_ENABLE;
    while (WDT->SYNCBUSY.reg) {}
}

void loop() {
    WDT_PET();   // ウォッチドッグを蹴る (ここに ~16s 戻らなければ自動リセット)

    // OTA in progress: do not run any other work; performOTA() blocks until reboot
    if (otaInProgress) {
        return;
    }

    // 初回のみ WiFi を遅延起動 (setup から外して I2C 即応答を優先)
    static bool wifiStartTriggered = false;
    if (!wifiStartTriggered) {
        wifiStartTriggered = true;
        startWiFi();
    }

    processI2CCommands();
    handleButtons();

    // OTA 中は専用進捗画面を保持し、通常表示・切断判定・READY 要求を抑制する
    if (xiaoOtaActive) {
        bool finalDone = (xiaoOtaPhase == OTA_PHASE_DONE || xiaoOtaPhase == OTA_PHASE_FAIL)
                         && (millis() - xiaoOtaLastMs > XIAO_OTA_FINAL_MS);
        if (finalDone || (millis() - xiaoOtaLastMs > XIAO_OTA_TIMEOUT_MS)) {
            xiaoOtaActive = false;          // DONE/FAIL 表示後 or 取りこぼし保険: 通常表示へ復帰
            state.needsUpdate = true;
        } else {
            serviceWiFi();
            serviceMdns();
            serviceOtaUdp();
            delay(10);
            return;
        }
    }

    // Check Xiao connection timeout (1 second without update = disconnected)
    if (state.xiaoConnected && (millis() - state.lastXiaoUpdate > 1000)) {
        state.xiaoConnected = false;
        state.needsUpdate = true;
    }

    // If not connected to ESP32, periodically request data
    static unsigned long lastReadyRequest = 0;
    if (!state.xiaoConnected && (millis() - lastReadyRequest > 2000)) {
        lastReadyRequest = millis();
        queueEvent(CMD_READY, 0, NULL);
    }

    updateDisplay();

    // WiFi / OTA / mDNS service (non-blocking)
    serviceWiFi();
    serviceMdns();
    serviceOtaUdp();

    // WiFi ハートビート (診断): Wio 自身の WiFi 接続状態・自IP・OTAリスナ状態を周期出力。
    // 変換器が断続的でも瞬間の窓で状態を掴めるようにする。
    if (millis() - lastWifiHbMs > 3000) {
        lastWifiHbMs = millis();
        int rssi = wifiConnected ? (int)WiFi.RSSI() : 0;  // 信号強度 (接続中のみ; フラップ要因の切り分け)
        Log.printf("WIFI-HB: conn=%d ip=%s rssi=%d otaListen=%d mdns=%d\n",
                   wifiConnected ? 1 : 0, wioOwnIp, rssi, otaUdpListening ? 1 : 0, wioMdnsStarted ? 1 : 0);
    }

    delay(10);  // Small delay to prevent tight loop
}
