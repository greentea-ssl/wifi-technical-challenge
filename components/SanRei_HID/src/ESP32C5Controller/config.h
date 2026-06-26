/**
 * Configuration for ESP32C5Controller
 *
 * WiFi credentials and other settings for XIAO ESP32C5
 */

#ifndef CONFIG_H
#define CONFIG_H

// ============================================================================
// WiFi Configuration
// ============================================================================

// Build-time configuration (set via build flags: -D__CONFIG__)
#ifdef __CONFIG__
    #define WIFI_SSID       __SSID__
    #define WIFI_PASSWORD   __PASSWD__
#else
    // Default configuration for XIAO ESP32C5
    #define BOARD_NAME "XIAO ESP32C5"
    #define WIFI_SSID       "TEAM_SSID"
    #define WIFI_PASSWORD   ""
#endif

// WiFi 接続はデフォルト SSID (WIFI_SSID) へ直接 begin する。別 AP は set_ssid downlink
// (SSID 明示の直接接続) で繋ぐ。hidden SSID も直接 begin で接続可。
// set_ssid で指定 AP に切替えた際、この時間内に一度も接続できなければデフォルト SSID へ復帰
#define WIFI_MANUAL_CONNECT_WINDOW_MS 15000

// ============================================================================
// Network Configuration
// ============================================================================

// Subnet for broadcast (third octet)
#ifdef __CONFIG__
    #define SUBNET_THIRD    __SUBNET__
#else
    #define SUBNET_THIRD    4
#endif

#endif // CONFIG_H
