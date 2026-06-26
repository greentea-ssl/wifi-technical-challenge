// R4 calibration test — ESP32-C5 STA
//
// Joins TEAM_SSID_OPEN (open auth, hidden SSID OK) and prints
// (esp_timer, TSF) pairs at ~100ms over Serial @ 921600 baud.
//
// CSV columns: seq, esp_timer_us, tsf_us, read_dur_us, tsf_delta_us, rssi
//
// Header lines start with '#'. Data rows start with "R4,".
//
// Verifies R4 (architecture.md §8): is the esp_timer ↔ TSF mapping
// well-approximated by a line over a 100ms-sample window, with residuals
// in the μs range?
//
// FQBN:
//   XIAO C5:    esp32:esp32:XIAO_ESP32C5
//   devkit C5:  esp32:esp32:esp32c5_devkitc  (pick the ESP32-C5 DevKit board, NOT XIAO)

#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_timer.h>

static const char* WIFI_SSID = "TEAM_SSID_OPEN";
static const char* WIFI_PASS = "";  // open auth

static const uint32_t SAMPLE_PERIOD_MS = 100;

static uint32_t g_seq = 0;
static int64_t  g_last_tsf = 0;

static void print_mac(const char* prefix, const uint8_t mac[6]) {
  Serial.printf("# %s=%02X:%02X:%02X:%02X:%02X:%02X\n",
                prefix, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

void setup() {
  Serial.begin(921600);
  // Native USB CDC needs a moment; UART bridge doesn't but the delay is harmless.
  delay(200);

  Serial.println();
  Serial.println("# R4_CALIB_TEST v1");

  WiFi.mode(WIFI_STA);

  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  print_mac("MAC", mac);

  Serial.printf("# Connecting to SSID=%s (open, hidden)\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < 30000) {
    delay(200);
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("# ERROR: WiFi connect timeout (30s)");
    // Keep going — still print samples; TSF may be 0 until associated.
  } else {
    uint8_t* bssid = WiFi.BSSID();
    Serial.printf("# CONNECTED ch=%d rssi=%d ip=%s\n",
                  WiFi.channel(), (int)WiFi.RSSI(), WiFi.localIP().toString().c_str());
    if (bssid) print_mac("BSSID", bssid);
  }

  Serial.println("# CSV: seq,esp_timer_us,tsf_us,read_dur_us,tsf_delta_us,rssi");
}

void loop() {
  int64_t t_a = esp_timer_get_time();
  int64_t tsf = esp_wifi_get_tsf_time(WIFI_IF_STA);
  int64_t t_b = esp_timer_get_time();

  int rssi = (int)WiFi.RSSI();
  int64_t tsf_delta = (g_last_tsf == 0) ? 0 : (tsf - g_last_tsf);
  g_last_tsf = tsf;

  Serial.printf("R4,%u,%lld,%lld,%lld,%lld,%d\n",
                (unsigned)g_seq++,
                (long long)t_a,
                (long long)tsf,
                (long long)(t_b - t_a),
                (long long)tsf_delta,
                rssi);

  delay(SAMPLE_PERIOD_MS);
}
