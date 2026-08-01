#pragma once
enum { WIFI_OFF };
struct StubWiFi { void mode(int) {} };
extern StubWiFi WiFi;
