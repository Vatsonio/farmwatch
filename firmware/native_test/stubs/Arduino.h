// Стаби Arduino для нативного тесту парсера прошивки на ПК (g++).
// Дозволяють ганяти справжній main.cpp без заліза.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <deque>

extern uint32_t g_millis;
static inline uint32_t millis() { return g_millis; }
static inline void btStop() {}

// Буфер приймача: дані докладаються порціями, як у справжньому UART/CDC.
// Віртуальний ESP наповнює його з окремого потоку (під власним мьютексом),
// тест парсера користується ним однопотоково.
struct StubSerial {
  std::deque<char> q;
  void begin(long) {}
  void feed(const char *s) { push(s, strlen(s)); }
  void push(const char *s, size_t n) { for (size_t i = 0; i < n; i++) q.push_back(s[i]); }
  int available() { return (int)q.size(); }
  int read() {
    if (q.empty()) return -1;
    char c = q.front();
    q.pop_front();
    return (unsigned char)c;
  }
};
extern StubSerial Serial;

struct StubESP {
  bool restarted = false;
  void restart() { restarted = true; }
};
extern StubESP ESP;
