/**
 * ESP32C5Controller - I2C Master Controller for XIAO ESP32C5
 *
 * Main WiFi/UDP communication controller
 * Communicates with WioTerminal display via I2C
 * Integrates CAN communication with PowerBoard
 *
 * Debug: 全ログを USB CDC (Serial) と 外部 USB-serial 変換器 (dbgCom=Serial0/UART0:
 *        TX=GPIO9 / RX=GPIO8) の両方へ出力 (LogMux)。VCC 非結線の変換器でログを取ることで、
 *        PowerBoard の給電制御を阻害せず電源投入直後の挙動を観測できる。
 *
 * Version: 3.1.0
 */

#include <WiFi.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <Wire.h>
#include <Update.h>
#include <HTTPClient.h>
#include "esp_twai.h"
#include "esp_twai_onchip.h"
#include "config.h"
#include "metrics_radio.h"

// ============================================================================
// Board Pin Configuration for XIAO ESP32C5
// ============================================================================

#define BOARD_NAME "XIAO ESP32C5"
#define I2C_SDA 23
#define I2C_SCL 24
#define SERIAL1_TX 11
#define SERIAL1_RX 12
#define CAN_TX_PIN 1
#define CAN_RX_PIN 0
// デバッグログ用 UART (外部 USB-serial 変換器, VCC 非結線)。
// 配線: 変換器 RX <- Xiao GPIO9 (TX) / 変換器 TX -> Xiao GPIO8 (RX, 本用途では未使用)。
#define DBG_TX 9
#define DBG_RX 8

// ============================================================================
// Constants
// ============================================================================
#define I2C_SLAVE_ADDR 0x08
#define I2C_CLOCK_SPEED 100000
#define CONTROLLER_VERSION "LOCAL"

// I2C Commands (Master -> Slave)
#define CMD_UPDATE_STATUS   0x01
#define CMD_SET_ROBOT_ID    0x02
#define CMD_UPDATE_EMO      0x03
#define CMD_FULL_REFRESH    0x05
#define CMD_UPDATE_NETWORK  0x06  // SSID and version (lightweight)
#define CMD_OTA_PROGRESS    0x07  // OTA 進行状況を Wio に表示 (payload: [source, phase, percent])
// CMD_OTA_PROGRESS payload[0] = source (どの基板の OTA か)
#define OTA_SRC_XIAO        0     // この C5 (XIAO) 自身の OTA
#define OTA_SRC_POWERBOARD  1     // 電源基板の OTA (CAN 0x318 を中継)
// CMD_OTA_PROGRESS payload[1] = phase
#define OTA_PHASE_START     0     // 接続中
#define OTA_PHASE_DOWNLOAD  1     // ダウンロード中 (payload[2] = percent 0-100)
#define OTA_PHASE_APPLY     2     // 書込/適用中
#define OTA_PHASE_DONE      3     // 完了 (再起動)
#define OTA_PHASE_FAIL      4     // 失敗
// 電源基板 OTA 進捗 CAN フレーム [robot_comm_spec v2.1.0, 種別01100]
#define CAN_ID_PB_OTA_PROGRESS 0x318

// I2C Commands (Slave -> Master)
#define CMD_ID_CONFIRM       0x81
// 0x82 (CMD_EMO_TOGGLE) removed: manual EMO is no longer supported
#define CMD_READY            0x83  // WioTerminal ready notification
#define CMD_ENTER_MANUAL     0x84  // [LEGACY] Wio 旧 fw: 5-way 起動押下。新 fw は CMD_BOOT_BUTTONS で送る
// 0x85 (CMD_BOOT_IGNORE_PROT): 開発中に追加したが未デプロイのため削除し、bitmap 形式に統一
#define CMD_BOOT_BUTTONS     0x86  // 起動時に押されていたボタン bitmap (1byte payload)
#define CMD_BTN_LONGPRESS    0x87  // 通常動作中の長押し確定通知 bitmap (1byte payload)

// Button bitmap (CMD_BOOT_BUTTONS / CMD_BTN_LONGPRESS の payload)
// Wio 側の定義と完全一致させること
#define BTN_KEY_A            0x01
#define BTN_KEY_B            0x02
#define BTN_KEY_C            0x04
#define BTN_5WAY_PRESS       0x08
// 0x10-0x80 reserved (5-way UP/DOWN/LEFT/RIGHT 等の将来拡張用)

// HID Commands (CAN ID: 0x088, PowerBoard 側 config.h と一致させる) [robot_comm_spec v2.0.0]
// (定義は上部に置く: checkSlaveEvents() から参照されるため preprocessor 順序的にここで必要)
#define HID_CMD_SET_ROBOT_ID         0x01
#define HID_CMD_READ_STATUS          0x02
#define HID_CMD_STOP                 0x03
#define HID_CMD_RESUME               0x04
// 保護オーバーライド設定 (robot_comm_spec v2.0.0 で新設、CAN_LS §2.7)。
// Byte1 = ビットフラグ (bit0=過電流, bit1=過放電; 0=保護有効, 1=保護無効)。
// v1.x では 0x210 にこのコマンドが無く、HID は 0x201 PARAM_CMD_SET を直接打つ
// 暫定実装で代替していたが、v2.0.0 で正規ルートとしてこの 0x05 に統一。
#define HID_CMD_PROTECTION_OVERRIDE  0x05
#define PROTECTION_OVERRIDE_OC       0x01  // bit0: 過電流保護オーバーライド
#define PROTECTION_OVERRIDE_OD       0x02  // bit1: 過放電保護オーバーライド

// 前方宣言 (デフォルト引数付き関数は Arduino IDE の自動プロトタイプ生成対象外のため明示)
void sendCANHIDCommand(uint8_t cmd, uint8_t param = 0);
esp_err_t sendCANRaw(uint32_t id, const uint8_t* data, uint8_t dlc);

// Ports
#define BASE_LISTEN_PORT    40000
#define BASE_UPLINK_PORT    50000
#define EMS_PORT            40999
// hid_bridge (robot_comm_spec v2.0.0 / hid_bridge.md): PC <-> HID 汎用 CAN ブリッジ
#define BRIDGE_DOWN_PORT    41000   // PC -> HID: CAN 送出指示 / set_log_level (unicast)
#define BRIDGE_UP_PORT      51000   // HID -> PC: CAN テレメトリ (種別 11111) 転送 (broadcast)
// 起動時しきい値 (severity <= level を PC へ転送)。起動モードに応じて初期値を切替える:
//   通常起動         = WARN  (2)  … 通常運用、転送量を抑える
//   MANUAL / 保護OVR = TRACE (5)  … 5-way 起動押下 or KEY_C 保護オーバーライド時は詳細ログ
// いずれも揮発。PC からの set_log_level で実行時に上書き可能。
#define BRIDGE_DEFAULT_LOG_LEVEL 2  // 通常起動時しきい値 = WARN
#define BRIDGE_VERBOSE_LOG_LEVEL 5  // MANUAL / 保護オーバーライド時しきい値 = TRACE
#define BRIDGE_LOG_LEVEL_ALL    15  // F (15): promiscuous = 全 CAN フレームを raw 転送 (デバッグ)。6-14 は予約

// hid_bridge uplink バッチ送信: 複数の JSON 行 (NDJSON, \n 区切り) を 1 UDP datagram にまとめ、
// (a) 累積が MTU を超える or (b) 前回送信から FLUSH_MS 経過 で送出。パケット数を削減 (特に level F)。
#define BRIDGE_UP_FLUSH_MS  100    // 最大滞留時間 [ms]
#define BRIDGE_UP_MTU       1400   // 1 datagram の payload 上限 [byte] (WiFi MTU 1500 から余裕)
#define BRIDGE_UP_BUF_SIZE  1664   // バッファ実体 = MTU + 1 行分マージン

// OTA
#define OTA_CMD_BYTE        0x30
#define OTA_TIMEOUT_MS      60000
#define OTA_STALL_TIMEOUT_MS 10000  // ダウンロード中にデータ無進捗がこの時間続いたら中断 (WiFi 混信等)

// Timing
#define STATUS_UPDATE_INTERVAL_MS  150
#define WIFI_CONNECT_TIMEOUT_MS    10000
#define EMS_TIMEOUT_MS              3000
#define CAN_SEND_INTERVAL_MS        100  // 10Hz

// CAN IDs (HID Board) [robot_comm_spec v2.0.0]
#define CAN_ID_TX           0x088   // HID -> PowerBoard (direct command) [was 0x210]
#define CAN_ID_RX           0x0D8   // PowerBoard -> HID (direct response) [was 0x218]
#define CAN_ID_HID_STATUS   0x008   // HID -> Main 状態通知 (rev4 §1.3 / §2.1)
                                    // Byte0=動作モード(0:ノーマル/1:マニュアル/2:デバッグ), Byte1=robotId
#define CAN_ID_FW_VERSION   0x040   // Main -> HID FW バージョン応答 (rev4 §1.4 / §2.2, DLC=5)
                                    // 0x008 受信に対するレスポンスとして送信される
                                    // Byte 0-3: FW version (年7/月4/日5/シリアル16 bit pack)
                                    // Byte 4:   動作モード echo-back (0=NORMAL/1=MANUAL/2=DEBUG)

// 動作モード (CAN_ID_HID_STATUS Byte0, rev4)
#define OP_MODE_NORMAL  0x00
#define OP_MODE_MANUAL  0x01
#define OP_MODE_DEBUG   0x02

// CAN Error Recovery
#define CAN_RECOVERY_INTERVAL_MS 1000
#define MAX_TX_FAILURES_BEFORE_RECOVERY 10

// ============================================================================
// Global Variables
// ============================================================================
Preferences preferences;

// Robot ID
uint8_t robotId = 0;
uint16_t listenPort = BASE_LISTEN_PORT;
uint16_t uplinkPort = BASE_UPLINK_PORT;

// WiFi
bool wifiConnected = false;
bool mdnsEnabled = false;
char mdnsName[32];

// WiFi 接続は「デフォルト SSID (WIFI_SSID) へ直接 begin」が基本。scan ベースの
// 優先リスト方式 (#5) は撤去した (起動時の接続が遅く IP 表示が遅延するため)。
// 別 AP へ繋ぎたい場合は set_ssid downlink で SSID を明示指定する。

// set_ssid (hid_bridge downlink 41000) による手動オーバーライド (揮発)。
// 猶予 (WIFI_MANUAL_CONNECT_WINDOW_MS) 内に一度でも接続できれば、その後切断されても
// 指定 AP をリトライし続ける (sticky)。猶予内に一度も繋がらなければデフォルト SSID へ復帰。
static bool          wifiManualMode = false;
static char          wifiManualSsid[33] = "";
static char          wifiManualPass[65] = "";
static bool          wifiManualEverConnected = false;
static unsigned long wifiManualStartMs = 0;
static unsigned long wifiLastBeginMs = 0;   // 直近の WiFi.begin 時刻 (0=即時試行可)

// オープン AP (パスワード空) は 1 引数 begin、それ以外は 2 引数 begin。
// begin 前に必ず disconnect して進行中の接続/自動再接続を止める (ESP32 の
// "sta is connecting, cannot set config" を回避し、別 AP へ確実に切替えるため)。
static void wifiBeginAp(const char* ssid, const char* pass) {
    WiFi.disconnect();
    // 対象 AP へ (再)associate するたびに TSF 較正をクリア (issue #7)。AP 切替/再接続で
    // TSF が新 AP 値へジャンプするため、旧 AP の不連続ペアを回帰窓に残さない。
    metrics_on_reassociate();
    if (pass && pass[0]) WiFi.begin(ssid, pass);
    else                 WiFi.begin(ssid);
}

// UDP
WiFiUDP udp;
WiFiUDP uplink;
WiFiUDP ems;
// hid_bridge sockets (robot_comm_spec v2.0.0)
WiFiUDP bridgeRx;   // downlink listen on 41000+id
WiFiUDP bridgeUp;   // uplink broadcast on 51000+id
volatile uint8_t bridgeLogLevel = BRIDGE_DEFAULT_LOG_LEVEL;  // severity <= level を PC へ転送
char packetBuffer[255];
uint8_t sendPacketBuffer[1024];
uint16_t sendPacketBufferIndex = 0;

// UDP rx 専用 task と queue (loop の他処理から isolation して
// p95/p99 tail を縮める。ESP32-C5 は single HP core だが FreeRTOS
// preemption + 高 priority で latency 改善)
typedef struct {
    uint8_t data[256];
    size_t  len;
} rx_packet_t;
QueueHandle_t rx_packet_queue = NULL;
TaskHandle_t  udp_rx_task_handle = NULL;

// hid_bridge: CAN テレメトリ (種別 11111) を ISR で拾い、main loop で JSON 転送するための queue
typedef struct {
    uint32_t id;
    uint8_t  dlc;
    uint8_t  data[8];
} bridge_tlm_t;
QueueHandle_t bridge_tlm_queue = NULL;

static void udp_rx_task(void* arg) {
    rx_packet_t pkt;
    while (true) {
        if (!wifiConnected) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        int n = udp.parsePacket();
        if (n > 0) {
            int len = udp.read(pkt.data, sizeof(pkt.data));
            if (len > 0) {
                pkt.len = (size_t)len;
                // radio_metrics: 高 priority task 内で即記録 (TSF を最短取得)
                metrics_record_rx(pkt.data, pkt.len);
                // CU UART は main loop が処理 (UART は thread-safe でないため)
                if (rx_packet_queue) {
                    xQueueSend(rx_packet_queue, &pkt, 0);  // 溢れたら drop (OK)
                }
            }
        } else {
            vTaskDelay(1);  // 1 tick (~1ms) yield、busy loop 防止
        }
    }
}

// metrics_radio broadcast 専用 task (main loop の重い処理から isolation)
static void metrics_broadcast_task(void* arg) {
    while (true) {
        if (wifiConnected) {
            metrics_task();  // ring drain + TSF 較正 + 52000+id 送出
        }
        vTaskDelay(pdMS_TO_TICKS(2));  // 2ms 周期 (= 500Hz drain 余裕)
    }
}

// EMO (manual EMO removed; only remote EMS-driven stop remains)
bool isRemoteEMO = false;
unsigned long lastEmsPacketTime = 0;

// ローカル停止 (Wio 5-way 長押しでトグル)。
// PowerBoard の ABORT_HID_ESTOP は単一ビットを Remote EMO と共有しているため、
// C5 側で OR (isRemoteEMO || isLocalEstop) を一元管理し、エッジで CAN を発行する。
bool isLocalEstop = false;
bool prevHidEstopAsserted = false;

// HID 動作モード (CAN_ID_HID_STATUS = 0x008 Byte0 で送信)
// 5-way 起動時押下で MANUAL に遷移する。デフォルトは NORMAL。
uint8_t hidOpMode = OP_MODE_NORMAL;

// Main FW バージョン (CAN_ID_FW_VERSION = 0x040 受信で更新, rev4 §1.4 DLC=5)
// Byte 0-3: yyyyyyym mmmddddd ssssssss ssssssss (年7bit/月4bit/日5bit/シリアル16bit)
// Byte 4:   Main 側が echo-back する動作モード (0008 で送った値が反映される)
struct MainFwVersion {
    bool received;
    uint8_t raw[4];           // FW version 4 byte
    uint8_t modeEcho;         // Main がエコーバックした動作モード (rev4 §1.4)
    unsigned long lastUpdateTime;
};
volatile MainFwVersion mainFwVersion = {false, {0, 0, 0, 0}, 0xFF, 0};

// CAN フレームカウンタ (hid_status で報告)。
// v2.1.0 で Web UI (/can, /api/can) 廃止に伴い循環バッファ canLog は撤去し、通算カウントのみ維持。
static volatile uint32_t canRxCount = 0;
static volatile uint32_t canTxOkCount = 0;
static volatile uint32_t canTxFailCount = 0;

// Serial
#define mainCom Serial1   // UART1: CU (メイン基板) との UART リンク
#define dbgCom  Serial0   // UART0: デバッグログ用 (GPIO9=TX / GPIO8=RX)。USB CDC とは独立。

// USB CDC (Serial) と デバッグ UART (dbgCom) の両方へ出力するロガー。
// Phase 2 では Xiao を USB 直結と UART 変換器の両方に繋いでログを照合する。
// USB 未接続時 (debug phase) は Serial への書き込みは破棄され、UART 側のみ出力される。
class LogMux : public Print {
public:
    size_t write(uint8_t c) override {
        Serial.write(c);
        dbgCom.write(c);
        return 1;
    }
    size_t write(const uint8_t *buf, size_t len) override {
        Serial.write(buf, len);
        dbgCom.write(buf, len);
        return len;
    }
    void flush() {
        Serial.flush();
        dbgCom.flush();
    }
};
LogMux Log;

// Timing
unsigned long lastStatusUpdate = 0;
unsigned long lastCanSendTime = 0;

// CAN Error Tracking
static uint32_t canTxFailureCount = 0;
static unsigned long lastCanRecoveryTime = 0;

// OTA
volatile bool otaInProgress = false;

// 電源基板 OTA 進捗 (CAN 0x318) を ISR で受けて main loop で Wio へ中継するための退避領域
volatile bool    pbOtaPending = false;
volatile uint8_t pbOtaPhase   = 0;
volatile uint8_t pbOtaPct     = 0;

// TWAI Node Handle (ESP32-C5 new API)
static twai_node_handle_t twai_node = NULL;

// CAN RX buffer for callback
static uint8_t canRxBuffer[8];
static twai_frame_t canRxFrame = {0};

// LED Status
static unsigned long lastLedToggleTime = 0;
static bool ledState = false;

// Telemetry Data (from PowerBoard via HID protocol)
struct TelemetryData {
    uint8_t resultCode;     // 0x00=success, 0x01=fail
    uint8_t robotId;        // Robot ID from PowerBoard
    uint8_t status;         // 0:Stop, 1:Standby, 2:Drive
    uint8_t stopReason;     // Stop reason bit flags
    unsigned long lastUpdateTime;
    bool valid;
} telemetryData = {0, 0, 0, 0, 0, false};

// ============================================================================
// I2C Communication
// ============================================================================
bool sendI2CPacket(uint8_t cmd, const uint8_t* data, uint8_t len) {
    Wire.beginTransmission(I2C_SLAVE_ADDR);
    Wire.write(cmd);
    Wire.write(len);
    if (len > 0 && data != NULL) {
        Wire.write(data, len);
    }
    uint8_t error = Wire.endTransmission();

    if (error != 0) {
        Log.printf("I2C error: %d (cmd=0x%02X len=%d)\n", error, cmd, len);
        return false;
    }
    return true;
}

void sendUpdateStatus() {
    Log.println("sendUpdateStatus: sending...");
    uint8_t data[10];

    // Status bitmap
    data[0] = 0;
    if (wifiConnected) data[0] |= 0x01;
    if (mdnsEnabled) data[0] |= 0x02;
    if (telemetryData.valid && (millis() - telemetryData.lastUpdateTime < 5000)) {
        data[0] |= 0x04;  // PowerBoard connected
    }

    // IP Address
    IPAddress ip = WiFi.localIP();
    data[1] = ip[0];
    data[2] = ip[1];
    data[3] = ip[2];
    data[4] = ip[3];

    // Listen Port
    data[5] = (listenPort >> 8) & 0xFF;
    data[6] = listenPort & 0xFF;

    // Stop Reason
    data[7] = telemetryData.valid ? telemetryData.stopReason : 0;

    // Power Status (0:Stop, 1:Standby, 2:Drive)
    data[8] = telemetryData.valid ? telemetryData.status : 0;

    // Reserved
    data[9] = 0;

    uint8_t result = sendI2CPacket(CMD_UPDATE_STATUS, data, 10);
    Log.printf("sendUpdateStatus: I2C result=%d\n", result);
}

void sendSetRobotId() {
    Log.printf("sendSetRobotId: robotId=%d\n", robotId);
    sendI2CPacket(CMD_SET_ROBOT_ID, &robotId, 1);
}

void sendUpdateEmo() {
    uint8_t data[1];
    data[0] = isRemoteEMO ? 1 : 0;
    Log.printf("sendUpdateEmo: remote=%d\n", data[0]);
    sendI2CPacket(CMD_UPDATE_EMO, data, 1);
}

void sendFullRefresh() {
    uint8_t data[64];
    uint8_t idx = 0;

    // Status bitmap
    data[idx] = 0;
    if (wifiConnected) data[idx] |= 0x01;
    if (mdnsEnabled) data[idx] |= 0x02;
    if (telemetryData.valid && (millis() - telemetryData.lastUpdateTime < 5000)) {
        data[idx] |= 0x04;  // PowerBoard connected
    }
    idx++;

    // IP Address
    IPAddress ip = WiFi.localIP();
    data[idx++] = ip[0];
    data[idx++] = ip[1];
    data[idx++] = ip[2];
    data[idx++] = ip[3];

    // Listen Port
    data[idx++] = (listenPort >> 8) & 0xFF;
    data[idx++] = listenPort & 0xFF;

    // Robot ID
    data[idx++] = robotId;

    // EMO status (remote only; manual EMO removed)
    data[idx++] = isRemoteEMO ? 1 : 0;

    // Stop Reason
    data[idx++] = telemetryData.valid ? telemetryData.stopReason : 0;

    // SSID
    const char* ssid = WIFI_SSID;
    uint8_t ssidLen = strlen(ssid);
    if (ssidLen > 32) ssidLen = 32;
    data[idx++] = ssidLen;
    memcpy(&data[idx], ssid, ssidLen);
    idx += ssidLen;

    // Version
    const char* version = CONTROLLER_VERSION;
    uint8_t verLen = strlen(version);
    if (verLen > 15) verLen = 15;
    data[idx++] = verLen;
    memcpy(&data[idx], version, verLen);
    idx += verLen;

    sendI2CPacket(CMD_FULL_REFRESH, data, idx);
}

void sendUpdateNetwork() {
    uint8_t data[50];
    uint8_t idx = 0;

    // SSID
    const char* ssid = WIFI_SSID;
    uint8_t ssidLen = strlen(ssid);
    if (ssidLen > 32) ssidLen = 32;
    data[idx++] = ssidLen;
    memcpy(&data[idx], ssid, ssidLen);
    idx += ssidLen;

    // Version
    const char* version = CONTROLLER_VERSION;
    uint8_t verLen = strlen(version);
    if (verLen > 15) verLen = 15;
    data[idx++] = verLen;
    memcpy(&data[idx], version, verLen);
    idx += verLen;

    Log.printf("sendUpdateNetwork: cmd=0x%02X len=%d SSID=%s\n", CMD_UPDATE_NETWORK, idx, ssid);
    sendI2CPacket(CMD_UPDATE_NETWORK, data, idx);
}

void checkSlaveEvents() {
    // Request data from slave
    Wire.requestFrom(I2C_SLAVE_ADDR, (uint8_t)32);

    if (Wire.available() >= 2) {
        uint8_t cmd = Wire.read();
        uint8_t len = Wire.read();

        if (Wire.available() >= len) {
            switch (cmd) {
                case CMD_ID_CONFIRM:
                    if (len >= 1) {
                        uint8_t newId = Wire.read();
                        handleIdConfirm(newId);
                    }
                    break;

                case CMD_READY:
                    Log.println("WioTerminal READY notification received - resending all data");
                    handleWioReady();
                    break;

                case CMD_ENTER_MANUAL:
                    // [LEGACY] 旧 Wio fw からの 5-way 起動押下イベント。
                    // 新 Wio fw は CMD_BOOT_BUTTONS の BTN_5WAY_PRESS ビットで通知する。
                    Log.println("ENTER_MANUAL (legacy) from Wio (5-way held at boot)");
                    hidOpMode = OP_MODE_MANUAL;
                    bridgeLogLevel = BRIDGE_VERBOSE_LOG_LEVEL;  // MANUAL = TRACE 詳細ログ
                    Log.printf(" -> bridge log level = %u (TRACE, MANUAL)\n", bridgeLogLevel);
                    sendHIDStatusToMain(hidOpMode, robotId);
                    break;

                case CMD_BOOT_BUTTONS:
                    if (len >= 1) {
                        uint8_t mask = Wire.read();
                        Log.printf("Boot buttons from Wio: 0x%02X\n", mask);
                        if (mask & BTN_5WAY_PRESS) {
                            Log.println(" -> enter MANUAL mode");
                            hidOpMode = OP_MODE_MANUAL;
                            bridgeLogLevel = BRIDGE_VERBOSE_LOG_LEVEL;  // MANUAL = TRACE 詳細ログ
                            Log.printf(" -> bridge log level = %u (TRACE, MANUAL)\n", bridgeLogLevel);
                            sendHIDStatusToMain(hidOpMode, robotId);
                        }
                        if (mask & BTN_KEY_C) {
                            Log.println(" -> set PowerBoard OC/OD override via HID 0x05 (0x088)");
                            sendIgnoreOCODOverride();
                        }
                        // 未割当ビット (KEY_A, KEY_B, 予約) は明示的に無視 (将来拡張)
                    }
                    break;

                case CMD_BTN_LONGPRESS:
                    if (len >= 1) {
                        uint8_t mask = Wire.read();
                        Log.printf("Long-press from Wio: 0x%02X\n", mask);
                        if (mask & BTN_5WAY_PRESS) {
                            isLocalEstop = !isLocalEstop;
                            Log.printf(" -> local ESTOP toggled: %s\n",
                                          isLocalEstop ? "ON" : "OFF");
                            applyEstopState(false);
                        }
                        // 未割当ビットは将来用途
                    }
                    break;

                default:
                    // Unknown command, drain buffer
                    while (Wire.available()) Wire.read();
                    break;
            }
        }
    }
}

// ============================================================================
// Event Handlers
// ============================================================================
void handleIdConfirm(uint8_t newId) {
    Log.printf("ID Confirm received: %d -> %d\n", robotId, newId);

    // Save to preferences
    preferences.begin("robot", false);
    preferences.putUChar("id", newId);
    preferences.end();

    // Update runtime values
    robotId = newId;
    listenPort = BASE_LISTEN_PORT + robotId;
    uplinkPort = BASE_UPLINK_PORT + robotId;

    // Reconfigure UDP
    if (wifiConnected) {
        udp.stop();
        uplink.stop();
        ems.stop();
        bridgeRx.stop();
        bridgeUp.stop();
        udp.begin(listenPort);
        uplink.begin(uplinkPort);
        ems.begin(EMS_PORT);
        bridgeRx.begin(BRIDGE_DOWN_PORT + robotId);  // hid_bridge downlink
        bridgeUp.begin(BRIDGE_UP_PORT + robotId);    // hid_bridge uplink

        // Update mDNS
        if (mdnsEnabled) {
            MDNS.end();
            sprintf(mdnsName, "robot%d", robotId);
            if (MDNS.begin(mdnsName)) {
                MDNS.addService("http", "tcp", listenPort);
                Log.printf("mDNS: %s.local\n", mdnsName);
            } else {
                mdnsEnabled = false;
            }
        }
    }

    // Notify WioTerminal
    sendSetRobotId();

    // Notify PowerBoard of new Robot ID
    sendRobotIdToPowerBoard();

    // Notify Main of new Robot ID via 0x008 (rev4 §1.3: ID変更時に発行)
    sendHIDStatusToMain(hidOpMode, robotId);
}

// Wio READY (再起動/再接続) 時の全データ再送を非ブロッキングで行うステートマシン。
// 旧実装は delay(100)+300*3 ≈ 1s ブロックしており、その間 loop が止まって
// periodic UPDATE_STATUS が途切れ、Wio の xiaoConnected 1s タイムアウトと競合して
// READY 連発 (画面 "Xiao disconnected") を誘発し得た。loop からステップ送信する。
static uint8_t       wioResendStep   = 0;   // 0=idle, 1..4=送信ステップ
static unsigned long wioResendNextMs = 0;
#define WIO_RESEND_INTERVAL_MS 200          // 各ステップ間隔 (Wio の I2C 処理猶予)

void handleWioReady() {
    // 再送シーケンスを armするだけ (実送信は serviceWioResend が loop から行う)
    Log.println("WioTerminal READY: arming non-blocking resend");
    wioResendStep   = 1;
    wioResendNextMs = millis();   // 最初のステップは即時
}

void serviceWioResend() {
    if (wioResendStep == 0) return;
    if ((long)(millis() - wioResendNextMs) < 0) return;
    switch (wioResendStep) {
        case 1: sendUpdateStatus();  break;
        case 2: sendUpdateNetwork(); break;
        case 3: sendSetRobotId();    break;
        case 4: sendUpdateEmo();     break;
    }
    if (++wioResendStep > 4) {
        wioResendStep = 0;
        Log.println("All data resent to WioTerminal");
    } else {
        wioResendNextMs = millis() + WIO_RESEND_INTERVAL_MS;
    }
}

// ============================================================================
// CAN Communication (ESP32-C5 New TWAI API)
// ============================================================================

// Forward declaration for RX callback
static bool twai_rx_callback(twai_node_handle_t handle, const twai_rx_done_event_data_t *edata, void *user_ctx);

void initCAN() {
    if (twai_node != NULL) {
        Log.println("CAN already initialized");
        return;
    }

    // Configure TWAI node for ESP32-C5
    twai_onchip_node_config_t node_config = {};
    node_config.io_cfg.tx = (gpio_num_t)CAN_TX_PIN;
    node_config.io_cfg.rx = (gpio_num_t)CAN_RX_PIN;
    node_config.io_cfg.quanta_clk_out = GPIO_NUM_NC;
    node_config.io_cfg.bus_off_indicator = GPIO_NUM_NC;
    node_config.clk_src = TWAI_CLK_SRC_DEFAULT;
    node_config.bit_timing.bitrate = 1000000;  // 1 Mbps
    node_config.bit_timing.sp_permill = 875;   // Sample point at 87.5%
    node_config.data_timing = (twai_timing_basic_config_t){0};  // Not using CAN FD
    // 有限リトライ。-1 (無限) だと PowerBoard 不在等で ACK が返らないとき no-ACK フレームが
    // TX スロットに永久スタックし、満杯時に _node_start_trans が不正フレームを memcpy して
    // Load access fault でクラッシュした。有限化で失敗フレームを落としスロットを解放する。
    node_config.fail_retry_cnt = 3;
    node_config.tx_queue_depth = 5;
    node_config.intr_priority = 0;
    node_config.flags.enable_self_test = false;
    node_config.flags.enable_loopback = false;
    node_config.flags.enable_listen_only = false;
    node_config.flags.no_receive_rtr = false;

    esp_err_t result = twai_new_node_onchip(&node_config, &twai_node);
    if (result != ESP_OK) {
        Log.printf("CAN Driver install failed: %d\n", result);
        twai_node = NULL;
        return;
    }
    Log.println("CAN Driver installed (1Mbps, Normal mode)");

    // Register RX callback
    twai_event_callbacks_t cbs = {};
    cbs.on_rx_done = twai_rx_callback;

    result = twai_node_register_event_callbacks(twai_node, &cbs, NULL);
    if (result != ESP_OK) {
        Log.printf("CAN callback registration failed: %d\n", result);
        twai_node_delete(twai_node);
        twai_node = NULL;
        return;
    }

    // Enable the node
    result = twai_node_enable(twai_node);
    if (result != ESP_OK) {
        Log.printf("CAN Start failed: %d\n", result);
        twai_node_delete(twai_node);
        twai_node = NULL;
        return;
    }
    Log.println("CAN Started");

    canTxFailureCount = 0;
    lastCanRecoveryTime = 0;
}

void recoverCAN() {
    unsigned long now = millis();

    if (now - lastCanRecoveryTime < CAN_RECOVERY_INTERVAL_MS) {
        return;
    }
    lastCanRecoveryTime = now;

    Log.println("CAN TX failure - attempting recovery...");

    if (twai_node != NULL) {
        twai_node_disable(twai_node);
        delay(50);
        twai_node_delete(twai_node);
        twai_node = NULL;
        delay(50);
    }
    initCAN();

    Log.println("CAN recovery completed");
}

// HID_CMD_* / POWER_IGNORE_* / sendCANHIDCommand の宣言は冒頭の定数セクションに移動済み
// (checkSlaveEvents() から参照される都合)

// TX buffer for CAN frames
static uint8_t canTxBuffer[8] = {0};

// CAN フレームカウンタ更新 (ISR からも main からも呼ばれる)。dir: 0=RX, 1=TX_OK, 2=TX_FAIL。
// v2.1.0 で Web UI 廃止に伴い循環バッファへの記録を撤去し、hid_status 用の通算カウントのみ更新する。
static inline void logCanFrame(uint32_t id, const uint8_t* data, uint8_t dlc, uint8_t dir) {
    (void)id; (void)data; (void)dlc;  // ペイロードは保持しない (カウントのみ)
    if (dir == 0) __atomic_fetch_add((uint32_t*)&canRxCount, 1, __ATOMIC_RELAXED);
    else if (dir == 1) __atomic_fetch_add((uint32_t*)&canTxOkCount, 1, __ATOMIC_RELAXED);
    else __atomic_fetch_add((uint32_t*)&canTxFailCount, 1, __ATOMIC_RELAXED);
}

// 任意 CAN frame を送信 (hid_bridge の type:"can" downlink 用)
// 戻り値: ESP_OK で成功、それ以外で失敗
esp_err_t sendCANRaw(uint32_t id, const uint8_t* data, uint8_t dlc) {
    if (twai_node == NULL) return ESP_FAIL;
    if (dlc > 8) return ESP_ERR_INVALID_ARG;

    uint8_t buf[8] = {0};
    for (uint8_t i = 0; i < dlc; i++) buf[i] = data[i];

    twai_frame_t tx_frame = {
        .header = {
            .id = id,
            .dlc = dlc,
            .ide = false,
            .rtr = false,
            .fdf = false,
            .brs = false,
            .esi = false,
        },
        .buffer = buf,
        .buffer_len = dlc,
    };

    esp_err_t r = twai_node_transmit(twai_node, &tx_frame, pdMS_TO_TICKS(10));
    logCanFrame(id, buf, dlc, (r == ESP_OK) ? 1 : 2);
    return r;
}

// PowerBoard へ過電流/過放電保護オーバーライドを設定する [robot_comm_spec v2.0.0]。
// 正規ルート HID_CMD_PROTECTION_OVERRIDE (0x05) を HID 直結チャネル (CAN_ID_TX = 0x088)
// へ 1 フレームで送る。Byte1 = ビットフラグ (bit0=過電流, bit1=過放電; 1=保護無効)。
// v1.x では 0x201 PARAM_CMD_SET を 2 フレーム直送する暫定実装だったが、v2.0.0 で
// PowerBoard 側に 0x05 ハンドラが追加されたため正規ルートへ移行 (役割逸脱を解消)。
void sendIgnoreOCODOverride() {
    uint8_t flags = PROTECTION_OVERRIDE_OC | PROTECTION_OVERRIDE_OD;  // OC/OD とも無効化
    Log.printf("HID TX: PROTECTION_OVERRIDE flags=0x%02X (OC+OD)\n", flags);
    sendCANHIDCommand(HID_CMD_PROTECTION_OVERRIDE, flags);
    // 保護オーバーライドは危険動作のため詳細ログを有効化 (= TRACE)
    bridgeLogLevel = BRIDGE_VERBOSE_LOG_LEVEL;
    Log.printf(" -> bridge log level = %u (TRACE, protection override)\n", bridgeLogLevel);
}

void sendCANHIDCommand(uint8_t cmd, uint8_t param) {
    // デフォルト引数 (param=0) は冒頭の前方宣言側に書いてあるので、定義側では省略
    if (twai_node == NULL) {
        Log.println("CAN TX failed: node not initialized");
        canTxFailureCount++;
        return;
    }

    canTxBuffer[0] = cmd;
    canTxBuffer[1] = param;

    twai_frame_t tx_frame = {
        .header = {
            .id = CAN_ID_TX,
            .dlc = 2,
            .ide = false,  // Standard 11-bit ID
            .rtr = false,
            .fdf = false,  // Classic CAN (not FD)
            .brs = false,
            .esi = false,
        },
        .buffer = canTxBuffer,
        .buffer_len = 2,
    };

    Log.printf("HID TX: cmd=0x%02X param=%d\n", cmd, param);

    esp_err_t result = twai_node_transmit(twai_node, &tx_frame, pdMS_TO_TICKS(10));
    logCanFrame(CAN_ID_TX, canTxBuffer, 2, (result == ESP_OK) ? 1 : 2);
    if (result == ESP_OK) {
        canTxFailureCount = 0;
    } else {
        canTxFailureCount++;
        Log.printf("CAN TX failed: %d (count: %lu)\n", result, canTxFailureCount);

        if (canTxFailureCount >= MAX_TX_FAILURES_BEFORE_RECOVERY) {
            recoverCAN();
            canTxFailureCount = 0;
        }
    }
}

// HID -> Main: HID 状態通知 (CAN_ID_HID_STATUS = 0x008, rev4 §1.3)
// Byte0 = 動作モード (OP_MODE_NORMAL/MANUAL/DEBUG), Byte1 = robotId
// 呼び出し箇所: setup() 末尾 (起動時) / handleIdConfirm() (ID変更時) /
//              checkSlaveEvents() の CMD_ENTER_MANUAL (モード切替時)
void sendHIDStatusToMain(uint8_t opMode, uint8_t rid) {
    if (twai_node == NULL) {
        Log.println("CAN(Main) TX failed: node not initialized");
        canTxFailureCount++;
        return;
    }

    canTxBuffer[0] = opMode;
    canTxBuffer[1] = rid;

    twai_frame_t tx_frame = {
        .header = {
            .id = CAN_ID_HID_STATUS,
            .dlc = 2,
            .ide = false,
            .rtr = false,
            .fdf = false,
            .brs = false,
            .esi = false,
        },
        .buffer = canTxBuffer,
        .buffer_len = 2,
    };

    Log.printf("HID Status TX (0x008): mode=%u robotId=%u\n", opMode, rid);

    esp_err_t result = twai_node_transmit(twai_node, &tx_frame, pdMS_TO_TICKS(10));
    logCanFrame(CAN_ID_HID_STATUS, canTxBuffer, 2, (result == ESP_OK) ? 1 : 2);
    if (result == ESP_OK) {
        canTxFailureCount = 0;
    } else {
        canTxFailureCount++;
        Log.printf("CAN(Main) TX failed: %d (count: %lu)\n", result, canTxFailureCount);

        if (canTxFailureCount >= MAX_TX_FAILURES_BEFORE_RECOVERY) {
            recoverCAN();
            canTxFailureCount = 0;
        }
    }
}

// Send Robot ID to PowerBoard
void sendRobotIdToPowerBoard() {
    Log.printf("Sending Robot ID to PowerBoard: %d\n", robotId);
    sendCANHIDCommand(HID_CMD_SET_ROBOT_ID, robotId);
}

// Send combined ESTOP state to PowerBoard.
// PowerBoard の ABORT_HID_ESTOP は HID_CMD_STOP/RESUME で操作される単一ビットなので、
// Remote EMO とローカル停止 (Wio 5-way 長押し) を C5 側で OR してから送る。
// 引数 force=true で前回送信内容に関係なく再送 (起動時/再接続時用)。
void applyEstopState(bool force) {
    bool active = isRemoteEMO || isLocalEstop;
    if (!force && active == prevHidEstopAsserted) return;

    Log.printf("Apply ESTOP: remote=%d local=%d -> %s%s\n",
                  isRemoteEMO, isLocalEstop,
                  active ? "STOP" : "RESUME",
                  force ? " (forced)" : "");
    if (active) {
        sendCANHIDCommand(HID_CMD_STOP);
    } else {
        sendCANHIDCommand(HID_CMD_RESUME);
    }
    prevHidEstopAsserted = active;
}

// 既存呼び出し元との互換: 以前の sendEMOStatusToPowerBoard 名称を維持。
// Remote EMO の状態変化時 / 初期化時に呼ばれる。中身は applyEstopState に委譲。
void sendEMOStatusToPowerBoard() {
    applyEstopState(true);
}

// Send initial state to PowerBoard (called at startup and reconnection)
void sendInitialStateToPowerBoard() {
    Log.println("Sending initial state to PowerBoard...");
    delay(50);  // Small delay between commands
    sendRobotIdToPowerBoard();
    delay(50);
    sendEMOStatusToPowerBoard();
}

void sendCANPowerCommand() {
    // Send periodic status request (HID command 0x02: Read Robot ID / Status)
    // This triggers PowerBoard to respond with status via CAN ID 0x0D8
    sendCANHIDCommand(HID_CMD_READ_STATUS);
}

// TWAI RX callback (called from ISR context)
static bool twai_rx_callback(twai_node_handle_t handle, const twai_rx_done_event_data_t *edata, void *user_ctx) {
    // Prepare receive frame
    twai_frame_t rx_frame = {
        .buffer = canRxBuffer,
        .buffer_len = sizeof(canRxBuffer),
    };

    // Receive the frame
    bool woken = false;
    if (twai_node_receive_from_isr(handle, &rx_frame) == ESP_OK) {
        // すべての RX frame をカウント (hid_status 用)
        logCanFrame(rx_frame.header.id, canRxBuffer, rx_frame.buffer_len, 0);

        // hid_bridge: PC へ転送するフレームを queue へ (UDP 送信は ISR 不可、main loop で送出)。
        // [robot_comm_spec v2.1.0] 転送判定:
        //   level F(15) = 全 CAN フレーム (promiscuous/raw)
        //   11111 (テレメトリ)        = severity (Bit2-0) <= level のみ
        //   11110 (応答/診断)         = 常時
        if (bridge_tlm_queue) {
            uint8_t msgType = (rx_frame.header.id >> 6) & 0x1F;
            bool forward;
            if (bridgeLogLevel >= BRIDGE_LOG_LEVEL_ALL) {
                forward = true;                                       // F: 全フレーム
            } else if (msgType == 0x1F) {
                forward = ((rx_frame.header.id & 0x07) <= bridgeLogLevel);  // 11111: severity フィルタ
            } else if (msgType == 0x1E) {
                forward = true;                                       // 11110: 常時
            } else {
                forward = false;
            }
            if (forward) {
                bridge_tlm_t t;
                t.id  = rx_frame.header.id;
                // 実 DLC は header.dlc から取得 (buffer_len は受信前に設定した容量=8 のままで当てにならない)
                t.dlc = (rx_frame.header.dlc > 8) ? 8 : (uint8_t)rx_frame.header.dlc;
                for (uint8_t i = 0; i < t.dlc; i++) t.data[i] = canRxBuffer[i];
                BaseType_t hpw = pdFALSE;
                xQueueSendFromISR(bridge_tlm_queue, &t, &hpw);
                if (hpw == pdTRUE) woken = true;
            }
        }

        // Check if it's from PowerBoard (CAN ID: 0x0D8)
        // 長さ判定は header.dlc を使う (buffer_len は容量固定で不正確)
        if (rx_frame.header.id == CAN_ID_RX && rx_frame.header.dlc >= 4) {
            // Check if PowerBoard was previously disconnected (reconnection detection)
            bool wasDisconnected = !telemetryData.valid ||
                                   (millis() - telemetryData.lastUpdateTime > 5000);

            // Parse HID response (CAN ID: 0x0D8)
            telemetryData.resultCode = canRxBuffer[0];
            telemetryData.robotId = canRxBuffer[1];
            telemetryData.status = canRxBuffer[2];      // 0:Stop, 1:Standby, 2:Drive
            telemetryData.stopReason = canRxBuffer[3];
            telemetryData.lastUpdateTime = millis();
            telemetryData.valid = true;

            // Note: Can't print from ISR, but we can set a flag for main loop
            // For debugging, you may want to use a queue or flag system
        }
        // Check if it's FW version response from Main (CAN ID: 0x040, rev4 §1.4 DLC=5)
        else if (rx_frame.header.id == CAN_ID_FW_VERSION && rx_frame.header.dlc >= 5) {
            mainFwVersion.raw[0] = canRxBuffer[0];
            mainFwVersion.raw[1] = canRxBuffer[1];
            mainFwVersion.raw[2] = canRxBuffer[2];
            mainFwVersion.raw[3] = canRxBuffer[3];
            mainFwVersion.modeEcho = canRxBuffer[4];   // rev4: 動作モード echo-back
            mainFwVersion.lastUpdateTime = millis();
            mainFwVersion.received = true;
            // 実ログは main loop の processCANMessages() 側で出力 (ISR 制約のため)
        }
        // 電源基板 OTA 進捗 (0x318): ISR では I2C を触れないので退避し main loop で Wio へ中継
        else if (rx_frame.header.id == CAN_ID_PB_OTA_PROGRESS && rx_frame.header.dlc >= 2) {
            pbOtaPhase   = canRxBuffer[0];
            pbOtaPct     = canRxBuffer[1];
            pbOtaPending = true;
        }
    }
    return woken;  // hid_bridge queue 送信で高 priority task が起きた場合 true
}

void processCANMessages() {
    // Check if we received a message (telemetry updated by callback)
    // This function is now mainly for handling reconnection logic
    static unsigned long lastTelemetryCheck = 0;

    if (telemetryData.valid && (millis() - lastTelemetryCheck > 100)) {
        lastTelemetryCheck = millis();

        // Check if PowerBoard was previously disconnected
        static bool wasDisconnected = true;
        if (wasDisconnected && (millis() - telemetryData.lastUpdateTime < 1000)) {
            Log.printf("HID Response: result=%d id=%d status=%d stop=0x%02X\n",
                telemetryData.resultCode, telemetryData.robotId,
                telemetryData.status, telemetryData.stopReason);
            Log.println("PowerBoard reconnected - sending initial state...");
            sendInitialStateToPowerBoard();
            wasDisconnected = false;
        }

        // Update disconnection state
        if (millis() - telemetryData.lastUpdateTime > 5000) {
            wasDisconnected = true;
        }
    }

    // Main FW バージョン応答受信時のログ出力 (ISR 制約のため main loop で実施)
    // rev4 §1.4 DLC=5: byte 0-3=FW version, byte 4=動作モード echo-back
    static uint8_t lastLoggedRaw[4] = {0, 0, 0, 0};
    static uint8_t lastLoggedMode = 0xFF;
    if (mainFwVersion.received) {
        uint8_t r0 = mainFwVersion.raw[0];
        uint8_t r1 = mainFwVersion.raw[1];
        uint8_t r2 = mainFwVersion.raw[2];
        uint8_t r3 = mainFwVersion.raw[3];
        uint8_t me = mainFwVersion.modeEcho;

        // 同一バージョン+同一モードの再受信はログ抑制
        if (r0 != lastLoggedRaw[0] || r1 != lastLoggedRaw[1] ||
            r2 != lastLoggedRaw[2] || r3 != lastLoggedRaw[3] ||
            me != lastLoggedMode) {
            // bit layout (MSB first across 4 bytes): yyyyyyym mmmddddd ssssssss ssssssss
            //   year 7bit  = (r0 >> 1) & 0x7F
            //   month 4bit = ((r0 & 0x01) << 3) | ((r1 >> 5) & 0x07)
            //   day 5bit   = r1 & 0x1F
            //   serial 16bit = (r2 << 8) | r3
            uint8_t fwYear   = (r0 >> 1) & 0x7F;
            uint8_t fwMonth  = ((r0 & 0x01) << 3) | ((r1 >> 5) & 0x07);
            uint8_t fwDay    = r1 & 0x1F;
            uint16_t fwSerial = ((uint16_t)r2 << 8) | r3;
            const char* modeName = (me == OP_MODE_NORMAL) ? "NORMAL"
                                 : (me == OP_MODE_MANUAL) ? "MANUAL"
                                 : (me == OP_MODE_DEBUG)  ? "DEBUG"
                                 : "?";

            Log.printf("Main FW Version (0x040): y=%u m=%u d=%u serial=%u modeEcho=%u(%s) (raw=%02X %02X %02X %02X %02X)\n",
                fwYear, fwMonth, fwDay, fwSerial, me, modeName, r0, r1, r2, r3, me);

            // 動作モードの不一致を警告 (HID 側 hidOpMode と Main からのエコーが異なる場合)
            if (me != hidOpMode) {
                Log.printf("[WARN] Op mode mismatch! HID=%u Main(echo)=%u\n", hidOpMode, me);
            }

            lastLoggedRaw[0] = r0;
            lastLoggedRaw[1] = r1;
            lastLoggedRaw[2] = r2;
            lastLoggedRaw[3] = r3;
            lastLoggedMode = me;
        }
    }
}

// ============================================================================
// WiFi Functions
// ============================================================================
void startWiFi() {
    Log.println("Starting WiFi (non-blocking, direct begin)...");
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(false);  // 再接続は manageWiFiConnect が制御する
    wifiManualMode = false;
    // master 同等にデフォルト SSID へ scan を挟まず即 begin (数秒で接続、IP 表示が速い)。
    wifiBeginAp(WIFI_SSID, WIFI_PASSWORD);
    wifiLastBeginMs = millis();
    Log.printf("WiFi: direct begin to '%s'\n", WIFI_SSID);
}

// 未接続時の接続管理 (非ブロッキング)。
// 通常はデフォルト SSID (WIFI_SSID) へ直接接続。set_ssid 手動モード時は指定 AP に接続し、
// 猶予内に一度でも繋がれば sticky、猶予内に繋がらなければデフォルト SSID へ復帰する。
void manageWiFiConnect() {
    // begin 後 ~8s は接続完了を待つ (再 begin で邪魔しない)
    if (wifiLastBeginMs != 0 && (millis() - wifiLastBeginMs) < 8000) return;

    // --- 手動 (set_ssid) モード ---
    if (wifiManualMode) {
        if (!wifiManualEverConnected &&
            (millis() - wifiManualStartMs) > WIFI_MANUAL_CONNECT_WINDOW_MS) {
            Log.printf("WiFi: manual SSID '%s' connect window expired -> revert to default '%s'\n",
                          wifiManualSsid, WIFI_SSID);
            wifiManualMode = false;   // 以下のデフォルト接続へ続行
        } else {
            Log.printf("WiFi: (manual) connecting to '%s'\n", wifiManualSsid);
            wifiBeginAp(wifiManualSsid, wifiManualPass);
            wifiLastBeginMs = millis();
            return;
        }
    }

    // --- デフォルト SSID へ直接接続 ---
    Log.printf("WiFi: connecting to default '%s'\n", WIFI_SSID);
    wifiBeginAp(WIFI_SSID, WIFI_PASSWORD);
    wifiLastBeginMs = millis();
}

// WiFi 接続確立後の一度きり初期化 (metrics_init/pps) を GOT_IP イベント経路と
// checkWiFi ポーリング経路で共有する単一フラグ (issue #10)。別々の static フラグだと
// 両経路で metrics_init が二重実行され ring/seq リセット・gptimer 二重生成を招く。
static bool g_wifiInitDone = false;

void checkWiFi() {
    if (wifiConnected) {
        if (wifiManualMode) wifiManualEverConnected = true;  // 接続実績を記録 (sticky)
        wifiLastBeginMs = 0;
        return;  // Already connected
    }

    manageWiFiConnect();

    if (WiFi.status() == WL_CONNECTED) {
        Log.println("\nWiFi connected!");
        Log.print("IP: ");
        Log.println(WiFi.localIP());

        wifiConnected = true;

        // Start UDP
        udp.begin(listenPort);
        uplink.begin(uplinkPort);
        ems.begin(EMS_PORT);
        bridgeRx.begin(BRIDGE_DOWN_PORT + robotId);  // hid_bridge downlink
        bridgeUp.begin(BRIDGE_UP_PORT + robotId);    // hid_bridge uplink

        // radio_metrics: 52000+robotId broadcast を開始 (一度きり、共有フラグ issue #10)
        if (!g_wifiInitDone) {
            metrics_init(robotId, 0);  // subnet_third=0 → WiFi.localIP() から自動算出
            metrics_pps_enable(10);    // GPIO10 で TSF 1秒境界 PPS (docs/pps_sync_design.md)
            g_wifiInitDone = true;
            Log.printf("metrics_radio init: robot=%d, port=%d, pps=GPIO10\n", robotId, 52000 + robotId);
        } else {
            // 再接続: init は冪等のため再実行しないが、別サブネットへ移った場合に備え
            // broadcast 先だけ現在の IP から再計算する (旧サブネット宛固定を防止)。
            metrics_update_broadcast();
        }

        // Start mDNS (再接続時の重複登録を避けるため end してから begin)
        sprintf(mdnsName, "robot%d", robotId);
        MDNS.end();
        if (MDNS.begin(mdnsName)) {
            MDNS.addService("http", "tcp", listenPort);
            mdnsEnabled = true;
            Log.printf("mDNS: %s.local\n", mdnsName);
        } else {
            Log.println("mDNS failed to start");
            mdnsEnabled = false;
        }

        // Send updated status and network info to display
        sendUpdateStatus();
        sendUpdateNetwork();
        return;
    }
    // 未接続時の (再)接続は manageWiFiConnect() が優先リスト/手動モードで管理する。
}

void WiFiEvent(WiFiEvent_t event) {
    switch (event) {
        case ARDUINO_EVENT_WIFI_STA_GOT_IP:
            Log.print("WiFi Got IP: ");
            Log.println(WiFi.localIP());
            wifiConnected = true;

            // Start UDP
            udp.begin(listenPort);
            uplink.begin(uplinkPort);
            ems.begin(EMS_PORT);
            bridgeRx.begin(BRIDGE_DOWN_PORT + robotId);  // hid_bridge downlink
            bridgeUp.begin(BRIDGE_UP_PORT + robotId);    // hid_bridge uplink
            Log.printf("UDP started: listen=%d, uplink=%d, ems=%d, bridge=%d/%d\n",
                          listenPort, uplinkPort, EMS_PORT,
                          BRIDGE_DOWN_PORT + robotId, BRIDGE_UP_PORT + robotId);

            // radio_metrics: 52000+robotId broadcast を開始 (一度きり、共有フラグ issue #10)
            if (!g_wifiInitDone) {
                metrics_init(robotId, 0);
                metrics_pps_enable(10);    // GPIO10 で TSF 1秒境界 PPS
                g_wifiInitDone = true;
                Log.printf("metrics_radio init: robot=%d, port=%d, pps=GPIO10\n", robotId, 52000 + robotId);
            } else {
                // 再接続: 別サブネットへ移った場合に備え broadcast 先のみ再計算
                metrics_update_broadcast();
            }

            // Start mDNS (再接続時の重複登録を避けるため end してから begin)
            sprintf(mdnsName, "robot%d", robotId);
            MDNS.end();
            if (MDNS.begin(mdnsName)) {
                MDNS.addService("http", "tcp", listenPort);
                mdnsEnabled = true;
                Log.printf("mDNS: %s.local\n", mdnsName);
            } else {
                Log.println("mDNS failed to start");
                mdnsEnabled = false;
            }

            // Send updated status and network info to display
            sendUpdateStatus();
            sendUpdateNetwork();
            break;

        case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
            Log.println("WiFi Disconnected");
            wifiConnected = false;
            mdnsEnabled = false;
            break;

        default:
            break;
    }
}

// (Web Server / CAN Debug Web は robot_comm_spec v2.1.0 で廃止。
//  状態取得は hid_bridge の type:"hid_status"、CAN 送信は type:"can" downlink へ移行。)

// ============================================================================
// OTA Update
// ============================================================================
void handleOTACommand(const char* url) {
    // Reject OTA during DRIVE mode (telemetry status == 2)
    if (telemetryData.valid && telemetryData.status == 2) {
        Log.println("[OTA] Rejected: Robot is in DRIVE mode");
        return;
    }

    // Reject if OTA already in progress
    if (otaInProgress) {
        Log.println("[OTA] Rejected: OTA already in progress");
        return;
    }

    Log.printf("[OTA] Starting OTA from: %s\n", url);
    otaInProgress = true;
    performOTA(url);
}

// OTA 進行状況を Wio (I2C slave) へ通知する。Wio は source 別に専用画面で表示する。
void sendOtaProgressSrc(uint8_t source, uint8_t phase, uint8_t percent) {
    uint8_t data[3] = { source, phase, percent };
    sendI2CPacket(CMD_OTA_PROGRESS, data, 3);
}

// C5 (XIAO) 自身の OTA 進捗 (source = XIAO)。performOTA から呼ぶ。
void sendOtaProgress(uint8_t phase, uint8_t percent) {
    sendOtaProgressSrc(OTA_SRC_XIAO, phase, percent);
}

// Update.onProgress 用コールバック。writeStream 内から呼ばれる (main task)。
// I2C 負荷を抑えるため 5% 刻み (および 100%) でのみ Wio へ送信する。
static uint8_t s_otaLastPct = 255;
void otaProgressCb(size_t done, size_t total) {
    if (total == 0) return;
    uint8_t pct = (uint8_t)((uint64_t)done * 100 / total);
    if (pct == s_otaLastPct) return;
    if (pct < 100 && s_otaLastPct != 255 && (uint8_t)(pct - s_otaLastPct) < 5) return;
    s_otaLastPct = pct;
    sendOtaProgress(OTA_PHASE_DOWNLOAD, pct);
}

// performOTA が停止したサービス (CAN/UDP/mDNS) を復旧する。OTA 失敗時に呼ぶ。
// 成功時は ESP.restart() するため不要。これを呼ばないと OTA 失敗後 CAN/UDP が
// 停止したまま (テレメトリ・電源制御断) で物理リブートが必要になる。
static void otaRestoreServices() {
    // CAN: twai_node_enable で再開すると、performOTA の disable 時に TX キューへ
    // 残っていたフレーム (特に sendCANRaw のローカルバッファ由来 = 解放済みスタック) を
    // ドライバが _node_start_trans → memcpy で再送しようとし、dangling ポインタ参照で
    // Load access fault になる。delete + initCAN で空キューの新ノードを作り直す。
    if (twai_node != NULL) {
        twai_node_disable(twai_node);
        twai_node_delete(twai_node);
        twai_node = NULL;
    }
    initCAN();
    // UDP: performOTA で stop 済みのソケットを再 begin (stop 済みなのでリークなし)
    if (wifiConnected) {
        udp.begin(listenPort);
        uplink.begin(uplinkPort);
        ems.begin(EMS_PORT);
        bridgeRx.begin(BRIDGE_DOWN_PORT + robotId);
        bridgeUp.begin(BRIDGE_UP_PORT + robotId);
        // mDNS 再登録
        MDNS.end();
        sprintf(mdnsName, "robot%d", robotId);
        if (MDNS.begin(mdnsName)) {
            MDNS.addService("http", "tcp", listenPort);
            mdnsEnabled = true;
        } else {
            mdnsEnabled = false;
        }
    }
    // ソケットを再 begin し終えてから udp_rx_task を再開 (suspend の対)
    if (udp_rx_task_handle) vTaskResume(udp_rx_task_handle);
    Log.println("[OTA] services restored after failure");
}

// OTA 失敗時の共通後処理: Wio に FAIL 表示 → サービス復旧 → フラグ解除。
static void otaFail() {
    sendOtaProgress(OTA_PHASE_FAIL, 0);
    otaRestoreServices();
    otaInProgress = false;
}

void performOTA(const char* url) {
    Log.println("[OTA] Stopping services...");
    s_otaLastPct = 255;
    sendOtaProgress(OTA_PHASE_START, 0);  // Wio に「接続中」を表示

    // Stop CAN
    if (twai_node != NULL) {
        twai_node_disable(twai_node);
        Log.println("[OTA] CAN stopped");
    }

    // udp_rx_task (prio10) は udp ソケットで parsePacket/read を回し続けるため、
    // ここで stop/begin すると lwIP pcb を奪い合って Load access fault で落ちる。
    // OTA 中はタスクを suspend してソケットへの並行アクセスを断つ (復旧時に resume)。
    if (udp_rx_task_handle) vTaskSuspend(udp_rx_task_handle);

    // Stop UDP
    udp.stop();
    uplink.stop();
    ems.stop();
    bridgeRx.stop();
    bridgeUp.stop();
    Log.println("[OTA] UDP stopped");

    // Stop mDNS
    if (mdnsEnabled) {
        MDNS.end();
        Log.println("[OTA] mDNS stopped");
    }

    Log.println("[OTA] Downloading firmware...");

    HTTPClient http;
    http.setTimeout(OTA_TIMEOUT_MS);

    if (!http.begin(url)) {
        Log.println("[OTA] HTTP begin failed");
        otaFail();
        return;
    }

    int httpCode = http.GET();
    if (httpCode != 200) {
        Log.printf("[OTA] HTTP GET failed: %d\n", httpCode);
        http.end();
        otaFail();
        return;
    }

    int contentLength = http.getSize();
    if (contentLength <= 0) {
        Log.println("[OTA] Invalid content length");
        http.end();
        otaFail();
        return;
    }

    Log.printf("[OTA] Firmware size: %d bytes\n", contentLength);

    if (!Update.begin(contentLength)) {
        Log.printf("[OTA] Update.begin failed: %s\n", Update.errorString());
        http.end();
        otaFail();
        return;
    }

    // Write firmware。Update.writeStream は接続が途中で停止した場合 (WiFi 混信での部分
    // ダウンロード) に確実な打ち切りができず loop を数十秒〜無限にブロックし得る。
    // stall 検出付きの手動読み取りループにして、接続終了は即脱出・データ無進捗が
    // OTA_STALL_TIMEOUT_MS 続けば中断し、必ず otaFail() の復旧経路に回す。
    sendOtaProgress(OTA_PHASE_DOWNLOAD, 0);
    WiFiClient* stream = http.getStreamPtr();
    uint8_t obuf[1024];
    size_t written = 0;
    bool stalled = false;
    unsigned long lastDataMs = millis();
    while (written < (size_t)contentLength) {
        size_t avail = stream->available();
        if (avail) {
            size_t toRead = avail > sizeof(obuf) ? sizeof(obuf) : avail;
            int r = stream->readBytes(obuf, toRead);
            if (r > 0) {
                if (Update.write(obuf, (size_t)r) != (size_t)r) {
                    break;  // flash 書込エラー → 下の mismatch 判定で abort
                }
                written += (size_t)r;
                lastDataMs = millis();
                otaProgressCb(written, (size_t)contentLength);
            }
        } else if (!stream->connected()) {
            break;  // 接続終了 (これ以上来ない) → written != contentLength で mismatch
        } else if (millis() - lastDataMs > OTA_STALL_TIMEOUT_MS) {
            stalled = true;  // 接続は生きているがデータが止まった (混信/half-open)
            break;
        } else {
            delay(5);
        }
    }

    Log.printf("[OTA] Written: %u / %d bytes%s\n",
                  (unsigned)written, contentLength, stalled ? " (stalled)" : "");

    http.end();

    if (written != (size_t)contentLength) {
        Log.println(stalled ? "[OTA] Download stalled - aborting" : "[OTA] Write size mismatch");
        Update.abort();
        otaFail();
        return;
    }

    sendOtaProgress(OTA_PHASE_APPLY, 100);  // Wio に「適用中」を表示
    if (!Update.end(true)) {
        Log.printf("[OTA] Update.end failed: %s\n", Update.errorString());
        otaFail();
        return;
    }

    Log.println("[OTA] Update successful! Rebooting...");
    sendOtaProgress(OTA_PHASE_DONE, 100);  // Wio に「完了」を表示
    Log.flush();
    delay(500);
    ESP.restart();
}

// ============================================================================
// LED Status Control
// ============================================================================
void updateLED() {
    unsigned long currentTime = millis();

    if (otaInProgress) {
        // OTA in progress: 5Hz fast blink (100ms ON, 100ms OFF)
        if (currentTime - lastLedToggleTime >= 100) {
            lastLedToggleTime = currentTime;
            ledState = !ledState;
            digitalWrite(LED_BUILTIN, ledState ? HIGH : LOW);
        }
    } else if (!wifiConnected) {
        // WiFi connecting: 1Hz blink (500ms ON, 500ms OFF)
        if (currentTime - lastLedToggleTime >= 500) {
            lastLedToggleTime = currentTime;
            ledState = !ledState;
            digitalWrite(LED_BUILTIN, ledState ? HIGH : LOW);
        }
    } else if (canTxFailureCount > 0) {
        // WiFi connected but CAN error: 10Hz fast blink (100ms ON, 100ms OFF)
        if (currentTime - lastLedToggleTime >= 100) {
            lastLedToggleTime = currentTime;
            ledState = !ledState;
            digitalWrite(LED_BUILTIN, ledState ? HIGH : LOW);
        }
    } else {
        // WiFi connected + CAN OK: steady ON
        digitalWrite(LED_BUILTIN, HIGH);
        ledState = true;
    }
}

// ============================================================================
// UDP Communication
// ============================================================================
// ============================================================================
// hid_bridge: PC <-> HID 汎用 CAN ブリッジ (robot_comm_spec v2.0.0 / hid_bridge.md)
//   Downlink (PC->HID, port 41000+id): JSON で CAN 送出指示 / ログレベル設定
//   Uplink   (HID->PC, port 51000+id): CAN テレメトリ (種別 11111) を JSON broadcast
//   この HID は単一の Classic-CAN (低速バス) ノードのため bus=1 (LS) のみ対応する。
// ============================================================================

// flat JSON オブジェクトから "key" の値トークンを取り出す簡易抽出。
// 文字列値はクォートを外し、数値/トークンはそのまま out へコピー (NUL 終端)。
static bool jsonGetValue(const char* json, const char* key, char* out, size_t outSize) {
    char pat[40];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char* p = strstr(json, pat);
    if (!p) return false;
    p += strlen(pat);
    while (*p == ' ' || *p == '\t') p++;
    if (*p != ':') return false;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    size_t i = 0;
    if (*p == '"') {
        p++;
        while (*p && *p != '"' && i < outSize - 1) out[i++] = *p++;
    } else {
        while (*p && *p != ',' && *p != '}' && *p != ' ' && *p != '\t' &&
               *p != '\r' && *p != '\n' && i < outSize - 1) out[i++] = *p++;
    }
    out[i] = '\0';
    return true;
}

// severity 値 (0-5) -> syslog 風名称 (hid_bridge.md)。6-7 は予約。
static const char* severityName(uint8_t sev) {
    switch (sev) {
        case 0: return "FATAL";
        case 1: return "ERROR";
        case 2: return "WARN";
        case 3: return "INFO";
        case 4: return "DEBUG";
        case 5: return "TRACE";
        default: return "RESERVED";
    }
}

// ---- hid_bridge uplink バッチ送信 (NDJSON, \n 区切り) ----
// 複数の JSON 行を 1 UDP datagram にまとめ、MTU 超過 or FLUSH_MS 経過で送出してパケット数を削減。
static uint8_t      bridgeUpBuf[BRIDGE_UP_BUF_SIZE];
static size_t       bridgeUpLen = 0;
static unsigned long bridgeUpLastFlush = 0;

// バッファ内容を 1 datagram として broadcast 送出し、バッファを空にする。
static void bridgeUpFlush() {
    bridgeUpLastFlush = millis();
    if (bridgeUpLen == 0) return;
    IPAddress broadcast = WiFi.localIP();
    broadcast[3] = 255;
    metrics_record_tx(BRIDGE_UP_PORT + robotId, (uint16_t)bridgeUpLen);
    bridgeUp.beginPacket(broadcast, BRIDGE_UP_PORT + robotId);
    bridgeUp.write(bridgeUpBuf, bridgeUpLen);
    bridgeUp.endPacket();
    bridgeUpLen = 0;
}

// 1 行 (JSON + '\n') を batch バッファへ追加。入りきらなければ先に flush する。
static void bridgeUpAppend(const char* line, size_t len) {
    if (len == 0) return;
    if (len >= BRIDGE_UP_MTU) {
        // 単一行が MTU 以上 (通常起こらない): バッファを掃いてから単独送出。
        bridgeUpFlush();
        IPAddress broadcast = WiFi.localIP();
        broadcast[3] = 255;
        metrics_record_tx(BRIDGE_UP_PORT + robotId, (uint16_t)len);
        bridgeUp.beginPacket(broadcast, BRIDGE_UP_PORT + robotId);
        bridgeUp.write((const uint8_t*)line, len);
        bridgeUp.endPacket();
        bridgeUpLastFlush = millis();
        return;
    }
    if (bridgeUpLen + len > BRIDGE_UP_MTU) bridgeUpFlush();  // 入りきらない → 先に送る
    memcpy(bridgeUpBuf + bridgeUpLen, line, len);
    bridgeUpLen += len;
}

// hid_status (downlink type:"hid_status" 要求への応答) [hid_bridge.md v2.1.0]。
// HID 自身の稼働状態を 1 JSON にまとめ、uplink (51000+id) に 1 回 broadcast する。
// Web UI の旧 /api/status 置き換え。pull 方式 (要求時のみ応答) で通信量を抑える。
void sendHidStatus() {
    char json[384];
    int n = snprintf(json, sizeof(json),
        "{\"type\":\"hid_status\",\"robot_id\":%u,\"fw_version\":\"%s\","
        "\"op_mode\":%u,\"wifi\":%s,\"emo_remote\":%s,\"estop_local\":%s,"
        "\"log_level\":%u,\"can_rx\":%lu,\"can_tx_ok\":%lu,\"can_tx_fail\":%lu,"
        "\"can_fail_streak\":%lu,\"main_fw_received\":%s,\"main_mode_echo\":%d,"
        "\"ota\":%s,\"ts_ms\":%lu}\n",
        (unsigned)robotId, CONTROLLER_VERSION,
        (unsigned)hidOpMode,
        wifiConnected ? "true" : "false",
        isRemoteEMO ? "true" : "false",
        isLocalEstop ? "true" : "false",
        (unsigned)bridgeLogLevel,
        (unsigned long)canRxCount, (unsigned long)canTxOkCount, (unsigned long)canTxFailCount,
        (unsigned long)canTxFailureCount,
        mainFwVersion.received ? "true" : "false",
        mainFwVersion.received ? (int)mainFwVersion.modeEcho : 0xFF,
        otaInProgress ? "true" : "false",
        (unsigned long)millis());
    if (n <= 0) return;

    bridgeUpFlush();  // 滞留中のテレメトリを先に送ってから状態応答を出す (順序維持)
    IPAddress broadcast = WiFi.localIP();
    broadcast[3] = 255;
    metrics_record_tx(BRIDGE_UP_PORT + robotId, (uint16_t)n);
    bridgeUp.beginPacket(broadcast, BRIDGE_UP_PORT + robotId);
    bridgeUp.write((const uint8_t*)json, n);
    bridgeUp.endPacket();
    Log.printf("[bridge] hid_status sent (%d bytes)\n", n);
}

// Downlink (PC -> HID, port 41000+id) の 1 JSON オブジェクトを処理する。
void handleBridgeDownlink(const char* json) {
    char typeBuf[24];
    if (!jsonGetValue(json, "type", typeBuf, sizeof(typeBuf))) {
        return;  // type 無し → 破棄
    }

    if (strcmp(typeBuf, "can") == 0) {
        // CAN フレーム送出指示
        char busBuf[8], idBuf[16], payloadBuf[48], dlcBuf[8];
        int bus = jsonGetValue(json, "bus", busBuf, sizeof(busBuf)) ? atoi(busBuf) : 1;
        if (bus != 1) {
            // この HID は低速バス (Classic CAN) のみ接続。高速バス (FD) は非対応。
            Log.printf("[bridge] unsupported bus=%d (this HID only drives LS bus 1)\n", bus);
            return;
        }
        if (!jsonGetValue(json, "canid", idBuf, sizeof(idBuf))) return;
        uint32_t id = strtoul(idBuf, NULL, 0);  // "0x.." / 10進 どちらも可

        uint8_t bytes[8] = {0};
        uint8_t dlc = 0;
        if (jsonGetValue(json, "payload", payloadBuf, sizeof(payloadBuf))) {
            // hex 文字列 (区切り任意) を詰める
            char hex[20]; uint8_t hn = 0;
            for (const char* c = payloadBuf; *c && hn < sizeof(hex) - 1; c++) {
                if ((*c >= '0' && *c <= '9') || (*c >= 'a' && *c <= 'f') || (*c >= 'A' && *c <= 'F'))
                    hex[hn++] = *c;
            }
            hex[hn] = '\0';
            for (uint8_t i = 0; i + 1 < hn && dlc < 8; i += 2) {
                char h[3] = {hex[i], hex[i + 1], 0};
                bytes[dlc++] = (uint8_t)strtoul(h, NULL, 16);
            }
        }
        if (jsonGetValue(json, "dlc", dlcBuf, sizeof(dlcBuf))) {
            int d = atoi(dlcBuf);
            if (d >= 0 && d <= 8) dlc = (uint8_t)d;  // 明示 DLC があれば優先
        }
        esp_err_t r = sendCANRaw(id, bytes, dlc);
        Log.printf("[bridge] CAN TX id=0x%X dlc=%d -> %s\n", (unsigned)id, dlc,
                      (r == ESP_OK) ? "OK" : "FAIL");
    }
    else if (strcmp(typeBuf, "set_log_level") == 0) {
        char lvlBuf[8];
        if (jsonGetValue(json, "level", lvlBuf, sizeof(lvlBuf))) {
            // "F"/"f"/"0xF" も 15 として受理 (10進 "15" も可)
            int lvl = (lvlBuf[0] == 'F' || lvlBuf[0] == 'f') ? 15 : (int)strtol(lvlBuf, NULL, 0);
            if (lvl < 0) lvl = 0;
            if (lvl >= BRIDGE_LOG_LEVEL_ALL) lvl = BRIDGE_LOG_LEVEL_ALL;  // F = 全フレーム (promiscuous)
            else if (lvl > 5) lvl = 5;                                   // 6-14 は予約 → TRACE 相当に丸める
            bridgeLogLevel = (uint8_t)lvl;
            const char* lname = (lvl == BRIDGE_LOG_LEVEL_ALL) ? "ALL(raw)" : severityName((uint8_t)lvl);
            Log.printf("[bridge] log level -> %d (%s)\n", lvl, lname);
        }
    }
    else if (strcmp(typeBuf, "hid_status") == 0) {
        // HID 自身の状態を uplink(51000) に 1 回 broadcast 応答 [hid_bridge.md v2.1.0]
        sendHidStatus();
    }
    else if (strcmp(typeBuf, "set_ssid") == 0) {
        // 接続先 WiFi AP を切替える (任意 AP 指定。password 省略=オープン) [hid_bridge.md v2.1.0]
        char ssidBuf[33], passBuf[65];
        if (jsonGetValue(json, "ssid", ssidBuf, sizeof(ssidBuf)) && ssidBuf[0]) {
            strncpy(wifiManualSsid, ssidBuf, sizeof(wifiManualSsid) - 1);
            wifiManualSsid[sizeof(wifiManualSsid) - 1] = '\0';
            if (!jsonGetValue(json, "password", passBuf, sizeof(passBuf))) passBuf[0] = '\0';
            strncpy(wifiManualPass, passBuf, sizeof(wifiManualPass) - 1);
            wifiManualPass[sizeof(wifiManualPass) - 1] = '\0';
            wifiManualMode = true;
            wifiManualEverConnected = false;
            wifiManualStartMs = millis();
            wifiLastBeginMs = 0;             // manageWiFiConnect が即時に切替
            Log.printf("[bridge] set_ssid -> '%s' (switching; revert to default after %lus if no connect)\n",
                          wifiManualSsid, (unsigned long)(WIFI_MANUAL_CONNECT_WINDOW_MS / 1000));
            WiFi.disconnect();              // 現 AP を切断 → 以降 manageWiFiConnect が管理
        }
    }
    // 未知の type はサイレント破棄 (将来拡張のため)
}

// Uplink (HID -> PC, port 51000+id): queue に溜まった CAN フレームを JSON broadcast。
// kind は種別から導出: 11111=telemetry(severity付) / 11110=answer / その他=raw (level F の全フレーム)。
void drainBridgeTelemetry() {
    if (!bridge_tlm_queue) return;
    bridge_tlm_t t;
    char json[256];
    while (xQueueReceive(bridge_tlm_queue, &t, 0) == pdTRUE) {
        uint8_t msgType = (t.id >> 6) & 0x1F;   // メッセージ種別 (Bit10-6)
        uint8_t srcDev  = (t.id >> 3) & 0x07;   // 送信元デバイス種別 (Bit5-3)

        // payload を "01 02 .." 形式の hex 文字列へ
        char payloadStr[40]; size_t pi = 0;
        for (uint8_t i = 0; i < t.dlc && pi + 3 < sizeof(payloadStr); i++) {
            if (i) payloadStr[pi++] = ' ';
            snprintf(&payloadStr[pi], 3, "%02X", t.data[i]);
            pi += 2;
        }
        payloadStr[pi] = '\0';

        int n;
        if (msgType == 0x1F) {
            // テレメトリ (11111): サブID = severity
            uint8_t severity = t.id & 0x07;
            n = snprintf(json, sizeof(json),
                "{\"type\":\"can\",\"kind\":\"telemetry\",\"bus\":1,\"canid\":\"0x%X\","
                "\"src_device_type\":%u,\"dlc\":%u,\"severity\":\"%s\",\"severity_level\":%u,"
                "\"payload\":\"%s\",\"ts_ms\":%lu}\n",
                (unsigned)t.id, srcDev, (unsigned)t.dlc, severityName(severity), severity,
                payloadStr, (unsigned long)millis());
        } else {
            // 11110 = answer / それ以外 = raw (level F)。いずれも severity を持たない。
            const char* kind = (msgType == 0x1E) ? "answer" : "raw";
            n = snprintf(json, sizeof(json),
                "{\"type\":\"can\",\"kind\":\"%s\",\"bus\":1,\"canid\":\"0x%X\","
                "\"src_device_type\":%u,\"dlc\":%u,\"payload\":\"%s\",\"ts_ms\":%lu}\n",
                kind, (unsigned)t.id, srcDev, (unsigned)t.dlc, payloadStr, (unsigned long)millis());
        }
        if (n <= 0) continue;

        // バッチバッファへ追加 (MTU 超過なら内部で flush=UDP 送出)
        bridgeUpAppend(json, (size_t)n);
    }
    // 前回送出から FLUSH_MS 経過していれば、MTU 未満でも滞留させず送出する。
    if (bridgeUpLen > 0 && (unsigned long)(millis() - bridgeUpLastFlush) >= BRIDGE_UP_FLUSH_MS) {
        bridgeUpFlush();
    }
}

void handleUdpPackets() {
    // hid_bridge downlink (PC -> HID, 41000+id): JSON コマンド [robot_comm_spec v2.0.0]
    int bridgeN = bridgeRx.parsePacket();
    if (bridgeN > 0) {
        static char bridgeBuf[600];
        int blen = bridgeRx.read((uint8_t*)bridgeBuf, sizeof(bridgeBuf) - 1);
        if (blen > 0) {
            bridgeBuf[blen] = '\0';
            handleBridgeDownlink(bridgeBuf);
        }
    }
    // hid_bridge uplink (HID -> PC, 51000+id): CAN テレメトリ転送
    drainBridgeTelemetry();

    // EMS packets (shared with OTA on port 40999)
    int emsSize = ems.parsePacket();
    if (emsSize) {
        IPAddress remoteIP = ems.remoteIP();
        uint16_t remotePort = ems.remotePort();
        int len = ems.read(packetBuffer, sizeof(packetBuffer) - 1);
        if (len < 0) len = 0;   // parsePacket>0 でも read が -1 を返し得る → packetBuffer[-1] 書込を防ぐ
        packetBuffer[len] = '\0';

        // Check for OTA command (first byte == 0x30)
        if (len > 1 && (uint8_t)packetBuffer[0] == OTA_CMD_BYTE) {
            Log.println("========================================");
            Log.println("[OTA] OTA command received");
            Log.printf("  From: %d.%d.%d.%d:%d\n", remoteIP[0], remoteIP[1], remoteIP[2], remoteIP[3], remotePort);
            Log.printf("  URL: %s\n", &packetBuffer[1]);
            Log.println("========================================");
            handleOTACommand(&packetBuffer[1]);
        } else {
            // Standard EMS packet
            Log.println("========================================");
            Log.println("[UDP RX] EMS Packet Received");
            Log.printf("  From: %d.%d.%d.%d:%d\n", remoteIP[0], remoteIP[1], remoteIP[2], remoteIP[3], remotePort);
            Log.printf("  Size: %d bytes\n", emsSize);
            Log.printf("  Data: \"%s\"\n", packetBuffer);
            Log.println("========================================");

            if (strstr(packetBuffer, "stop") != NULL) {
                lastEmsPacketTime = millis();
            }
        }
    }

    // Update remote EMO status and notify PowerBoard on change
    bool prevRemoteEMO = isRemoteEMO;
    if (millis() - lastEmsPacketTime > EMS_TIMEOUT_MS) {
        isRemoteEMO = false;
    } else {
        isRemoteEMO = true;
    }

    // Notify PowerBoard if remote EMO changed
    if (isRemoteEMO != prevRemoteEMO) {
        Log.printf("Remote EMO changed: %d -> %d\n", prevRemoteEMO, isRemoteEMO);
        sendUpdateEmo();              // Update WioTerminal display
        sendEMOStatusToPowerBoard();  // Update PowerBoard
    }

    // AI downlink packets: 受信は udp_rx_task が高 priority で処理。
    // ここでは queue から取り出して CU UART に forward するだけ
    // (UART は main 側だけが触る = thread-safety 確保)
    if (rx_packet_queue) {
        rx_packet_t rx;
        while (xQueueReceive(rx_packet_queue, &rx, 0) == pdTRUE) {
            mainCom.write(rx.data, rx.len);
        }
    }

    // Uplink from main board
    while (mainCom.available()) {
        sendPacketBuffer[sendPacketBufferIndex] = mainCom.read();
        sendPacketBuffer[sendPacketBufferIndex + 1] = '\0';

        if (sendPacketBuffer[sendPacketBufferIndex] == '\n' ||
            sendPacketBufferIndex >= sizeof(sendPacketBuffer) - 2) {

            // Send uplink packet
            IPAddress broadcast = WiFi.localIP();
            broadcast[3] = 255;

            // radio_metrics: 上り送信記録 (送信直前の TSF を tx_ul JSON で報告)
            metrics_record_tx(uplinkPort, (uint16_t)(sendPacketBufferIndex + 1));

            uplink.beginPacket(broadcast, uplinkPort);
            uplink.write(sendPacketBuffer, sendPacketBufferIndex + 1);
            uplink.endPacket();

            Log.printf("Uplink sent to port %d\n", uplinkPort);
            sendPacketBufferIndex = 0;
        } else {
            sendPacketBufferIndex++;
        }
    }
}

// ============================================================================
// Setup and Loop
// ============================================================================
void setup() {
    Serial.begin(115200);
    // デバッグ UART (TX=GPIO9 / RX=GPIO8)。VCC 非結線の USB-serial 変換器でログを取得し、
    // USB 給電なしで電源投入直後の挙動を観測できるようにする。
    dbgCom.begin(115200, SERIAL_8N1, DBG_RX, DBG_TX);
    delay(500);

    Log.println("\n========================================");
    Log.printf("ESP32C5Controller - %s\n", BOARD_NAME);
    Log.printf("Version: %s\n", CONTROLLER_VERSION);
    Log.println("========================================");

    // Initialize LED
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, LOW);
    ledState = false;

    // Initialize Serial1 for main board communication
    mainCom.begin(115200, SERIAL_8N1, SERIAL1_RX, SERIAL1_TX);
    Log.println("Serial1 initialized");

    // Initialize I2C Master
    Wire.begin(I2C_SDA, I2C_SCL, I2C_CLOCK_SPEED);
    Log.printf("I2C Master initialized (SDA=%d, SCL=%d)\n", I2C_SDA, I2C_SCL);

    // Initialize CAN
    initCAN();

    // Load robot ID from preferences
    preferences.begin("robot", true);
    robotId = preferences.getUChar("id", 0);
    preferences.end();

    listenPort = BASE_LISTEN_PORT + robotId;
    uplinkPort = BASE_UPLINK_PORT + robotId;
    Log.printf("Robot ID: %d (ports: %d, %d)\n", robotId, listenPort, uplinkPort);

    // Register WiFi event handler
    WiFi.onEvent(WiFiEvent);

    // Start WiFi (non-blocking)
    startWiFi();

    // Send initial display data immediately (don't wait for WiFi)
    Log.println("Waiting for WioTerminal to be ready...");
    delay(2000);  // Wait for WioTerminal to be ready (increased from 500ms)
    Log.println("Sending initial data to WioTerminal...");
    sendUpdateStatus();  // Send basic status first (lightweight)
    delay(500);          // Delay between packets for WioTerminal processing
    sendUpdateNetwork(); // Send SSID and version (lightweight)
    delay(500);
    sendSetRobotId();    // Send robot ID
    delay(500);
    sendUpdateEmo();     // Send EMO status
    Log.println("Initial display data sent to WioTerminal");

    // Send initial state to PowerBoard (Robot ID + EMO status)
    delay(100);
    sendInitialStateToPowerBoard();

    // Notify Main of initial HID status (rev4 §1.3: HID 起動時に必ず1回)
    // 起動時点では hidOpMode = OP_MODE_NORMAL。Wio から CMD_ENTER_MANUAL が
    // 後から届いた場合は checkSlaveEvents() の case CMD_ENTER_MANUAL で再送される。
    delay(100);
    sendHIDStatusToMain(hidOpMode, robotId);

    // UDP rx 専用 task 起動 (loop の他処理から isolation して latency tail を縮める)
    rx_packet_queue = xQueueCreate(32, sizeof(rx_packet_t));
    xTaskCreate(udp_rx_task, "udp_rx", 4096, NULL, 10, &udp_rx_task_handle);
    Log.println("udp_rx_task started (priority=10, queue=32)");

    // hid_bridge: CAN テレメトリ転送 queue (ISR -> main loop) [robot_comm_spec v2.0.0]
    bridge_tlm_queue = xQueueCreate(32, sizeof(bridge_tlm_t));

    // metrics_radio broadcast 専用 task (main loop 律速を回避)
    xTaskCreate(metrics_broadcast_task, "metrics_bc", 4096, NULL, 5, NULL);
    Log.println("metrics_broadcast_task started (priority=5)");

    Log.println("Setup complete!\n");
}

void loop() {
    unsigned long currentTime = millis();

    // Check WiFi connection (non-blocking)
    checkWiFi();

    // Check for slave events
    checkSlaveEvents();

    // Wio READY 後の全データ再送 (非ブロッキング、ステップ送信)
    serviceWioResend();

    // Handle UDP communication (UDP rx は udp_rx_task が処理、ここでは queue dispatch + UL のみ)
    // metrics_task() は metrics_broadcast_task が周期実行する
    if (wifiConnected) {
        handleUdpPackets();
    }

    // Send periodic CAN power commands (10Hz)
    if (currentTime - lastCanSendTime >= CAN_SEND_INTERVAL_MS) {
        lastCanSendTime = currentTime;
        sendCANPowerCommand();
    }

    // Process CAN messages (handled by callback, this handles reconnection logic)
    processCANMessages();

    // 電源基板 OTA 進捗 (CAN 0x318) を受信していたら Wio へ中継 (ISR では I2C 不可)
    if (pbOtaPending) {
        pbOtaPending = false;
        sendOtaProgressSrc(OTA_SRC_POWERBOARD, pbOtaPhase, pbOtaPct);
    }

    // Update LED status
    updateLED();

    // Periodic status update
    if (currentTime - lastStatusUpdate > STATUS_UPDATE_INTERVAL_MS) {
        lastStatusUpdate = currentTime;
        sendUpdateStatus();

        // Send EMO update if changed
        static bool lastRemoteEMO = false;
        if (isRemoteEMO != lastRemoteEMO) {
            Log.printf("EMO state changed: Remote=%d\n", isRemoteEMO);
            sendUpdateEmo();
            lastRemoteEMO = isRemoteEMO;
        }
    }

    // delay(10) was here — removed because it bottlenecked 100 Hz UDP rx.
    // (loop 1 周あたり 10ms 強制スリープで packet が socket queue に積まれていた)
    // 代わりに yield() で WiFi/lwIP background task に CPU を譲る。
    yield();
}
