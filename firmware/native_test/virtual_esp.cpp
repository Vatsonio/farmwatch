// Віртуальний ESP: програмний двійник плати для тестів без заліза.
//
// Компілює СПРАВЖНІЙ src/main.cpp, приймає байти протоколу зі stdin (як плата
// приймає їх по USB CDC) і друкує у stdout те, що зараз на матриці. Так весь
// ланцюг farmwatch -> SerialDisplay -> прошивка перевіряється end-to-end.
//
// Збірка (з теки firmware):
//   g++ -std=c++17 -I native_test/stubs native_test/virtual_esp.cpp -o virtual_esp.exe
//
// Вивід (по рядку):
//   READY               плата завантажилась
//   SCREEN <текст>      матриця показує цей текст
//   REBOOT              прошивка сама себе перезавантажила
//
// Змінна оточення FW_TIME_SCALE прискорює час прошивки (напр. 60 = хвилина за
// секунду), щоб перевіряти таймаути звʼязку і самоперезавантаження швидко.

#include "stubs/Arduino.h"

#include <chrono>
#include <cstdlib>
#include <mutex>
#include <string>
#include <thread>
#include <fcntl.h>
#include <io.h>

uint32_t g_millis = 0;
StubSerial Serial;
StubESP ESP;
#include "stubs/WiFi.h"
StubWiFi WiFi;

#define setup fw_setup
#define loop fw_loop
#include "../src/main.cpp"
#undef setup
#undef loop

static std::mutex g_lock;                 // Serial ділять потік stdin і головний цикл
static double g_scale = 1.0;
static std::chrono::steady_clock::time_point g_boot;
static std::string g_lastScreen;

static void tick() {
  auto now = std::chrono::steady_clock::now();
  double ms = std::chrono::duration<double, std::milli>(now - g_boot).count();
  g_millis = (uint32_t)(ms * g_scale);
}

static void onText(const char *t) {
  if (!t) return;
  if (g_lastScreen == t) return;          // не спамимо тим самим екраном
  g_lastScreen = t;
  printf("SCREEN %s\n", t);
  fflush(stdout);
}

// Анімація "завершується" раз на ~700 мс часу прошивки, тож екрани змінюються
// приблизно як на живій платі. Лічильник глобальний, бо перезавантаження має
// скидати і його (на залізі рестарт обнуляє геть усе).
static uint32_t g_animNext = 0;

static bool onAnimate() {
  if (g_millis >= g_animNext) { g_animNext = g_millis + 700; return true; }
  return false;
}

static void bootFirmware() {
  for (int i = 0; i < MAX_PRINTERS; i++) {
    prevSt[i] = 0; prevName[i][0] = '\0';
    cur[i].st = 'i'; cur[i].progress = 0; pname[i][0] = '\0';
  }
  prevCount = 0; printerCount = 0; haveFirst = false;
  ovHead = ovTail = 0; evtActive = false; screen = 0;
  lineLen = 0; lineOverflow = false;
  g_lastScreen.clear();
  g_animNext = 0;
  P.textHook = onText;
  P.animateHook = onAnimate;
  fw_setup();
}

// Потік приймача: тягне байти зі stdin у буфер Serial, як USB CDC у залізі.
// Саме _read, а не fread: fread чекав би повний буфер, а справжній UART
// віддає рівно те, що вже прийшло, одразу.
static void stdinReader() {
  char buf[256];
  int fd = _fileno(stdin);
  while (true) {
    int n = _read(fd, buf, (unsigned)sizeof(buf));
    if (n <= 0) break;                    // порт/канал закрито
    std::lock_guard<std::mutex> g(g_lock);
    Serial.push(buf, (size_t)n);
  }
}

int main() {
  _setmode(_fileno(stdin), _O_BINARY);    // байти протоколу без CRLF-перекладу
  if (const char *s = getenv("FW_TIME_SCALE")) {
    double v = atof(s);
    if (v > 0) g_scale = v;
  }
  g_boot = std::chrono::steady_clock::now();
  tick();
  bootFirmware();
  printf("READY\n");
  fflush(stdout);

  std::thread reader(stdinReader);
  reader.detach();

  while (true) {
    tick();
    {
      std::lock_guard<std::mutex> g(g_lock);
      fw_loop();
    }
    if (ESP.restarted) {                  // прошивка попросила перезавантаження
      ESP.restarted = false;
      printf("REBOOT\n");
      fflush(stdout);
      g_boot = std::chrono::steady_clock::now();
      tick();
      std::lock_guard<std::mutex> g(g_lock);
      bootFirmware();
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  }
  return 0;
}
