// farmwatch ESP32-C3 farm status display.
// 4 модулі 8x8 (MAX7219) у ряд = 32x8. Дані від farmwatch по USB serial.
//
// Показує читабельний текст і цифри (бібліотека MD_Parola):
//   екран зведення:  PRINT 3  ERR 1  IDLE 2  DONE 0
//   екран деталей:   1 72%  2 IDLE  3 ERR  4 DONE  5 45%
//   подія:           спливає DONE 3 / ERR 2
//   нема даних:      біжить NO LINK
//
// Протокол (рядок):  FW|<count>|<letter><progress>,...\n
//   letter: p=друк, i=простій, d=готово, e=стоп/помилка, a=пауза, o=offline
//   progress: 0..100

#include <Arduino.h>
#include <WiFi.h>
#include <MD_Parola.h>
#include <MD_MAX72xx.h>
#include <SPI.h>

// --- залізо ---
#define HARDWARE_TYPE MD_MAX72XX::FC16_HW  // якщо текст битий/дзеркальний: GENERIC_HW / ICSTATION_HW / PAROLA_HW
#define MAX_DEVICES 4
#define PIN_DATA 4  // DIN
#define PIN_CS   5  // CS
#define PIN_CLK  6  // CLK

MD_Parola P = MD_Parola(HARDWARE_TYPE, PIN_DATA, PIN_CLK, PIN_CS, MAX_DEVICES);

// --- стан ферми ---
static const uint8_t MAX_PRINTERS = 8;
static const uint32_t LINK_TIMEOUT_MS = 6000;

struct Printer { char st; uint8_t progress; };
Printer cur[MAX_PRINTERS];
char prevSt[MAX_PRINTERS];
uint8_t printerCount = 0;
bool haveFirst = false;
uint32_t lastFrameMs = 0;

// --- екрани і події ---
uint8_t screen = 0;                 // 0 = зведення, 1 = деталі
char curText[256];                  // MD_Parola тримає вказівник, тож буфер статичний
char evtText[24];
bool evtActive = false;

struct Overlay { uint8_t type; uint8_t idx; };  // 1=готово, 2=стоп
Overlay ovq[MAX_PRINTERS];
uint8_t ovHead = 0, ovTail = 0;

void enqueueOverlay(uint8_t type, uint8_t idx) {
  uint8_t next = (ovTail + 1) % MAX_PRINTERS;
  if (next == ovHead) return;
  ovq[ovTail] = { type, idx };
  ovTail = next;
}

// --- парсинг кадру ---
char lineBuf[128];
uint8_t lineLen = 0;

void applyFrame() {
  if (strncmp(lineBuf, "FW|", 3) != 0) return;
  char *p = lineBuf + 3;
  char *bar = strchr(p, '|');
  if (!bar) return;
  int count = atoi(p);
  if (count < 0) count = 0;
  if (count > MAX_PRINTERS) count = MAX_PRINTERS;
  p = bar + 1;

  Printer parsed[MAX_PRINTERS];
  int n = 0;
  while (*p && n < count) {
    char letter = *p++;
    int prog = atoi(p);
    if (prog < 0) prog = 0;
    if (prog > 100) prog = 100;
    parsed[n].st = letter;
    parsed[n].progress = (uint8_t)prog;
    n++;
    char *comma = strchr(p, ',');
    if (!comma) break;
    p = comma + 1;
  }

  // події: перехід у готово / стоп
  if (haveFirst) {
    for (int i = 0; i < n; i++) {
      if (prevSt[i] != parsed[i].st) {
        if (parsed[i].st == 'd' && prevSt[i] == 'p') enqueueOverlay(1, i);
        else if (parsed[i].st == 'e' && prevSt[i] != 'e') enqueueOverlay(2, i);
      }
    }
  }

  printerCount = n;
  for (int i = 0; i < n; i++) {
    cur[i] = parsed[i];
    prevSt[i] = parsed[i].st;
  }
  haveFirst = true;
  lastFrameMs = millis();
}

void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      applyFrame();
      lineLen = 0;
    } else if (c != '\r' && lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else if (lineLen >= sizeof(lineBuf) - 1) {
      lineLen = 0;
    }
  }
}

// --- побудова тексту ---
bool linkLost() { return (millis() - lastFrameMs) > LINK_TIMEOUT_MS; }

void stateWord(char st, uint8_t prog, char *out) {
  switch (st) {
    case 'p': sprintf(out, "%d%%", prog); break;
    case 'i': strcpy(out, "IDLE"); break;
    case 'd': strcpy(out, "DONE"); break;
    case 'e': strcpy(out, "ERR"); break;
    case 'a': strcpy(out, "PAUSE"); break;
    case 'o': strcpy(out, "OFF"); break;
    default:  strcpy(out, "?"); break;
  }
}

void buildSummary() {
  int cp = 0, ce = 0, ci = 0, cd = 0, ca = 0, co = 0;
  for (int i = 0; i < printerCount; i++) {
    switch (cur[i].st) {
      case 'p': cp++; break;
      case 'e': ce++; break;
      case 'i': ci++; break;
      case 'd': cd++; break;
      case 'a': ca++; break;
      case 'o': co++; break;
    }
  }
  char *w = curText;
  w += sprintf(w, "PRINT %d  ERR %d  IDLE %d  DONE %d", cp, ce, ci, cd);
  if (ca) w += sprintf(w, "  PAUSE %d", ca);
  if (co) w += sprintf(w, "  OFF %d", co);
}

void buildDetail() {
  if (printerCount == 0) { strcpy(curText, "NO PRINTERS"); return; }
  char *w = curText;
  for (int i = 0; i < printerCount; i++) {
    char v[8];
    stateWord(cur[i].st, cur[i].progress, v);
    w += sprintf(w, "%d %s   ", i + 1, v);
  }
}

void loadScreen() {
  if (linkLost()) {
    strcpy(curText, "NO LINK");
    P.displayText(curText, PA_CENTER, 45, 600, PA_SCROLL_LEFT, PA_SCROLL_LEFT);
    return;
  }
  if (screen == 0) buildSummary();
  else buildDetail();
  P.displayText(curText, PA_LEFT, 35, 500, PA_SCROLL_LEFT, PA_SCROLL_LEFT);
}

void startEvent() {
  Overlay ov = ovq[ovHead];
  ovHead = (ovHead + 1) % MAX_PRINTERS;
  if (ov.type == 1) sprintf(evtText, "DONE %d", ov.idx + 1);
  else sprintf(evtText, "ERR %d", ov.idx + 1);
  evtActive = true;
  // яскравий сплив: заїзд знизу, виїзд згори, з паузою
  P.displayText(evtText, PA_CENTER, 30, 1300, PA_SCROLL_UP, PA_SCROLL_DOWN);
}

void setup() {
  WiFi.mode(WIFI_OFF);
  btStop();

  Serial.begin(115200);

  P.begin();
  P.setIntensity(2);       // помірна яскравість (живлення від USB)
  P.setInvert(false);
  P.displayClear();

  for (int i = 0; i < MAX_PRINTERS; i++) { prevSt[i] = 0; cur[i].st = 'i'; cur[i].progress = 0; }
  lastFrameMs = millis() - LINK_TIMEOUT_MS;  // старт у стані "нема звʼязку"

  loadScreen();  // почне з NO LINK, поки нема даних
}

void loop() {
  readSerial();

  // подія перебиває поточний екран одразу
  if (!evtActive && ovHead != ovTail) {
    startEvent();
  }

  if (P.displayAnimate()) {  // поточна анімація завершилась
    if (evtActive) {
      evtActive = false;
      loadScreen();          // повертаємось до ротації
    } else {
      screen ^= 1;           // наступний екран
      loadScreen();
    }
  }
}
