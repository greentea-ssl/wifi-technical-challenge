/**
 * Configuration for WioDisplay
 *
 * WiFi credentials are used by OTA update path (RTL8720DN via rpcWiFi).
 * Normal display operation does not require WiFi.
 */

#ifndef CONFIG_H
#define CONFIG_H

// ============================================================================
// WiFi Configuration (used only by OTA update)
// ============================================================================

// Build-time configuration (set via build flags: -D__CONFIG__)
#ifdef __CONFIG__
    #define WIFI_SSID       __SSID__
    #define WIFI_PASSWORD   __PASSWD__
#else
    // Default configuration (must match ESP32C5Controller for shared OTA tool)
    #define WIFI_SSID       "TEAM_SSID"
    #define WIFI_PASSWORD   ""
#endif

#endif // CONFIG_H
