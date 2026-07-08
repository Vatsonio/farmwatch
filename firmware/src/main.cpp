// farmwatch ESP32-C3 farm status display.
// Матриця 8x8 (MAX7219). Дані від farmwatch по USB serial.
//
// Протокол (рядок):  FW|<count>|<letter><progress>,...\n
//   letter: p=друк, i=простій, d=готово, e=стоп/помилка, a=пауза, o=offline
//   progress: 0..100
//
// Головний екран: стовпчикова діаграма (колонка = принтер, висота = прогрес).
// Стан кодується патерном анімації (яскравість MAX7219 спільна на чип).
// Події: перехід у готово -> хвиля + номер; перехід у стоп -> блимання рамки.
// Нема кадру понад 6 с -> іконка "нема звʼязку".

#include <Arduino.h>
#include <WiFi.h>
#include <MD_MAX72xx.h>

// --- залізо ---
#define HARDWARE_TYPE MD_MAX72XX::FC16_HW  // якщо картинка бита: GENERIC_HW / ICSTATION_HW
#define MAX_DEVICES 1
#define PIN_DATA 4  // DIN
#define PIN_CS   5  // CS
#define PIN_CLK  6  // CLK

// Орієнтація: якщо перевернуто, поміняй ці прапорці
#define FLIP_X 0
#define FLIP_Y 0

MD_MAX72XX mx = MD_MAX72XX(HARDWARE_TYPE, PIN_DATA, PIN_CLK, PIN_CS, MAX_DEVICES);

// --- стан ферми ---
static const uint8_t MAX_PRINTERS = 8;
static const uint32_t LINK_TIMEOUT_MS = 6000;
static const uint16_t FRAME_MS = 33;  // ~30 к/с

struct Printer { char st; uint8_t progress; };
Printer cur[MAX_PRINTERS];
char prevSt[MAX_PRINTERS];
uint8_t printerCount = 0;
bool haveFirst = false;
uint32_t lastFrameMs = 0;
float dispH[MAX_PRINTERS];  // згладжена висота стовпчика

// --- черга оверлеїв подій ---
struct Overlay { uint8_t type; uint8_t idx; };  // type: 1=готово, 2=стоп
Overlay ovq[MAX_PRINTERS];
uint8_t ovHead = 0, ovTail = 0;
bool ovActive = false;
Overlay ovCur;
uint32_t ovStart = 0;

// --- 3x5 цифри (рядки зверху вниз, біти b2 b1 b0) ---
const uint8_t DIGITS[10][5] = {
  {0b111,0b101,0b101,0b101,0b111}, // 0
  {0b010,0b110,0b010,0b010,0b111}, // 1
  {0b111,0b001,0b111,0b100,0b111}, // 2
  {0b111,0b001,0b111,0b001,0b111}, // 3
  {0b101,0b101,0b111,0b001,0b001}, // 4
  {0b111,0b100,0b111,0b001,0b111}, // 5
  {0b111,0b100,0b111,0b101,0b111}, // 6
  {0b111,0b001,0b010,0b010,0b010}, // 7
  {0b111,0b101,0b111,0b101,0b111}, // 8
  {0b111,0b101,0b111,0b001,0b111}, // 9
};

// px: x колонка 0..7 (зліва), y рядок 0..7 (знизу)
inline void px(int x, int y, bool on) {
  if (x < 0 || x > 7 || y < 0 || y > 7) return;
#if FLIP_X
  x = 7 - x;
#endif
#if FLIP_Y
  y = 7 - y;
#endif
  mx.setPoint(7 - y, x, on);  // MD рядок 0 зверху
}

inline bool blink(uint16_t periodMs) {
  return (millis() / periodMs) % 2 == 0;
}

void enqueueOverlay(uint8_t type, uint8_t idx) {
  uint8_t next = (ovTail + 1) % MAX_PRINTERS;
  if (next == ovHead) return;  // черга повна, пропускаємо
  ovq[ovTail] = { type, idx };
  ovTail = next;
}

// --- парсинг кадру ---
char lineBuf[128];
uint8_t lineLen = 0;

void applyFrame() {
  // очікуємо FW|<count>|<entries>
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

  // події: порівняння зі старим станом
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
      lineLen = 0;  // рядок задовгий, скидаємо
    }
  }
}

// --- рендер ---
uint8_t progToHeight(uint8_t progress) {
  // 0..100 -> 0..8
  return (uint8_t)((progress * 8 + 50) / 100);
}

void drawColumn(int i) {
  Printer &pr = cur[i];
  uint8_t target = progToHeight(pr.progress);
  if (pr.st == 'd') target = 8;

  // згладжування висоти
  float &h = dispH[i];
  h += (target - h) * 0.25f;
  int hi = (int)(h + 0.5f);

  switch (pr.st) {
    case 'p': {  // друк: стовпчик + блимаюча верхівка (активність)
      for (int y = 0; y < hi; y++) px(i, y, true);
      if (hi < 8 && blink(500)) px(i, hi, true);
      break;
    }
    case 'd': {  // готово: повний яскравий стовпчик
      for (int y = 0; y < 8; y++) px(i, y, true);
      break;
    }
    case 'e': {  // стоп/помилка: уся колонка блимає швидко
      if (blink(125)) for (int y = 0; y < 8; y++) px(i, y, true);
      break;
    }
    case 'a': {  // пауза: стовпчик прогресу, повільне блимання
      if (blink(1000)) for (int y = 0; y < hi; y++) px(i, y, true);
      break;
    }
    case 'o': {  // offline: піксель унизу, повільне блимання
      if (blink(1000)) px(i, 0, true);
      break;
    }
    case 'i':    // простій: піксель унизу рівно
    default:
      px(i, 0, true);
      break;
  }
}

void drawBarChart() {
  for (int i = 0; i < printerCount; i++) drawColumn(i);
}

void drawNoLink() {
  // повільне блимання кутів = нема даних від ПК
  if (blink(700)) {
    px(0, 0, true); px(7, 0, true); px(0, 7, true); px(7, 7, true);
  }
}

void drawDigit(int d, int x0, int y0) {
  if (d < 0 || d > 9) return;
  for (int r = 0; r < 5; r++) {
    uint8_t row = DIGITS[d][r];
    for (int c = 0; c < 3; c++) {
      if (row & (1 << (2 - c))) px(x0 + c, y0 + (4 - r), true);
    }
  }
}

void drawOverlay() {
  uint32_t t = millis() - ovStart;
  if (ovCur.type == 1) {  // готово: хвиля знизу вгору, далі номер принтера
    if (t < 800) {
      int rows = (int)(t / 100);  // 0..7
      for (int y = 0; y <= rows && y < 8; y++)
        for (int x = 0; x < 8; x++) px(x, y, true);
    } else {
      int num = ovCur.idx + 1;
      if (num <= 9 && blink(300)) drawDigit(num, 3, 2);
      else if (num > 9) { if (blink(300)) for (int x = 0; x < 8; x++) { px(x,0,true); px(x,7,true); } }
    }
    if (t >= 2000) ovActive = false;
  } else {  // стоп: блимання рамки
    if (blink(150)) {
      for (int k = 0; k < 8; k++) { px(k,0,true); px(k,7,true); px(0,k,true); px(7,k,true); }
    }
    if (t >= 1500) ovActive = false;
  }
}

void render() {
  mx.clear();
  bool linkLost = (millis() - lastFrameMs) > LINK_TIMEOUT_MS;

  if (!ovActive && ovHead != ovTail) {  // взяти наступний оверлей з черги
    ovCur = ovq[ovHead];
    ovHead = (ovHead + 1) % MAX_PRINTERS;
    ovActive = true;
    ovStart = millis();
  }

  if (linkLost && !ovActive) {
    drawNoLink();
  } else if (ovActive) {
    drawOverlay();
  } else {
    drawBarChart();
  }
  mx.update();
}

void splash() {
  // короткий sweep на старті
  for (int x = 0; x < 8; x++) {
    mx.clear();
    for (int y = 0; y < 8; y++) px(x, y, true);
    mx.update();
    delay(40);
  }
  mx.clear();
  mx.update();
}

void setup() {
  // wifi/bt off: знімає пікові струми з маленької C3
  WiFi.mode(WIFI_OFF);
  btStop();

  Serial.begin(115200);

  mx.begin();
  mx.control(MD_MAX72XX::INTENSITY, 2);  // помірна яскравість (живлення від USB)
  mx.clear();

  for (int i = 0; i < MAX_PRINTERS; i++) { prevSt[i] = 0; dispH[i] = 0; cur[i].st = 'i'; cur[i].progress = 0; }
  lastFrameMs = millis() - LINK_TIMEOUT_MS;  // старт у стані "нема звʼязку"

  splash();
}

void loop() {
  readSerial();
  render();
  delay(FRAME_MS);
}
