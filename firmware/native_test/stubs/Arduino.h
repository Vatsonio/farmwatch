// Стаби Arduino для нативного тесту парсера прошивки на ПК (g++).
// Дозволяють ганяти справжній main.cpp без заліза.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

extern uint32_t g_millis;
static inline uint32_t millis() { return g_millis; }
static inline void btStop() {}

struct StubSerial {
  const char *buf = nullptr;
  size_t pos = 0, len = 0;
  void begin(long) {}
  void feed(const char *s) { buf = s; pos = 0; len = strlen(s); }
  int available() { return (int)(len - pos); }
  int read() { return pos < len ? (unsigned char)buf[pos++] : -1; }
};
extern StubSerial Serial;

struct StubESP {
  bool restarted = false;
  void restart() { restarted = true; }
};
extern StubESP ESP;
