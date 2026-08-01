// Нативний тест прошивки: компілює СПРАВЖНІЙ src/main.cpp зі стабами Arduino
// і перевіряє парсер кадру, події та самоперезавантаження без заліза.
//
// Запуск (з теки firmware):
//   g++ -std=c++17 -I native_test/stubs native_test/parser_test.cpp -o parser_test.exe
//   ./parser_test.exe        (код виходу 0 = усі перевірки пройшли)

#include "stubs/Arduino.h"

uint32_t g_millis = 100000;
StubSerial Serial;
StubESP ESP;
#include "stubs/WiFi.h"
StubWiFi WiFi;

#define setup fw_setup
#define loop fw_loop
#include "../src/main.cpp"
#undef setup
#undef loop

static int failures = 0;

static void check(bool ok, const char *what) {
  printf("%s  %s\n", ok ? "PASS" : "FAIL", what);
  if (!ok) failures++;
}

static void feedFrame(const char *frame) {
  Serial.feed(frame);
  readSerial();
}

static int queued() { return (ovTail - ovHead + MAX_PRINTERS) % MAX_PRINTERS; }

static void resetState() {
  for (int i = 0; i < MAX_PRINTERS; i++) {
    prevSt[i] = 0; prevName[i][0] = '\0';
    cur[i].st = 'i'; cur[i].progress = 0; pname[i][0] = '\0';
  }
  prevCount = 0;
  printerCount = 0;
  haveFirst = false;
  ovHead = ovTail = 0;
  lastFrameMs = g_millis;
}

// живий кадр з ферми (labels=names)
static const char *LIVE =
    "FW|7|i0\x1f" "1. mini,p80\x1f" "1. A1,p75\x1f" "2. A1,i0\x1f" "3. A1,i0\x1f"
    "4. A1,i0\x1f" "5. A1,p46\x1f" "6. A1\n";

int main() {
  resetState();

  // --- парсинг живого кадру ---
  feedFrame(LIVE);
  check(printerCount == 7, "живий кадр: 7 принтерів");
  check(cur[1].st == 'p' && cur[1].progress == 80, "живий кадр: прогрес другого принтера");
  check(strcmp(pname[0], "1. mini") == 0, "живий кадр: назви розібрані");
  buildSummary();
  check(strcmp(curText, "PRINT 3  ERR 0  IDLE 4  DONE 0") == 0, "зведення рахує друк правильно");

  // --- кадр без назв (labels=numbers) ---
  feedFrame("FW|7|i0,i0,p82,p77,i0,i0,p48\n");
  check(printerCount == 7 && cur[2].progress == 82, "кадр без назв розбирається");

  // --- кадр приходить шматками ---
  resetState();
  Serial.feed("FW|2|p10\x1f" "1. A1,p2");
  readSerial();
  Serial.feed("0\x1f" "2. A1\n");
  readSerial();
  check(printerCount == 2 && cur[1].progress == 20, "розрізаний кадр збирається");

  // --- довгі назви (8 принтерів по 16 символів > старий буфер 128) ---
  feedFrame("FW|8|i0\x1f" "AAAAAAAAAAAAAAAA,i0\x1f" "BBBBBBBBBBBBBBBB,p82\x1f"
            "CCCCCCCCCCCCCCCC,p77\x1f" "DDDDDDDDDDDDDDDD,i0\x1f" "EEEEEEEEEEEEEEEE,i0\x1f"
            "FFFFFFFFFFFFFFFF,p48\x1f" "GGGGGGGGGGGGGGGG,i0\x1f" "HHHHHHHHHHHHHHHH\n");
  check(printerCount == 8 && strcmp(pname[7], "HHHHHHHHHHHHHHHH") == 0,
        "довгий кадр з 8 назвами влазить у буфер");

  // --- сміттєвий рядок довший за буфер не ламає наступний кадр ---
  {
    static char junk[400];
    memset(junk, 'x', sizeof(junk) - 2);
    junk[sizeof(junk) - 2] = '\n';
    junk[sizeof(junk) - 1] = '\0';
    feedFrame(junk);
  }
  feedFrame("FW|2|p11\x1f" "1. A1,p22\x1f" "2. A1\n");
  check(printerCount == 2 && cur[0].progress == 11, "після сміття кадр парситься");

  // --- події прив'язані до назви, а не до позиції ---
  resetState();
  feedFrame("FW|3|p50\x1f" "1. A1,p60\x1f" "1. mini,p70\x1f" "2. A1\n");
  ovHead = ovTail = 0;
  // той самий склад, інший порядок, завершився саме "1. mini"
  feedFrame("FW|3|d100\x1f" "1. mini,p55\x1f" "1. A1,p75\x1f" "2. A1\n");
  check(queued() == 1, "переставлений список: рівно одна подія");
  bool right = false;
  if (queued() == 1) {
    Overlay ov = ovq[ovHead];
    right = (ov.type == 1 && strcmp(pname[ov.idx], "1. mini") == 0);
  }
  check(right, "подія DONE вказує на правильний принтер");

  // --- принтер зник зі списку: без фальшивих подій ---
  ovHead = ovTail = 0;
  feedFrame("FW|2|p56\x1f" "1. A1,p76\x1f" "2. A1\n");
  check(queued() == 0, "зниклий принтер не породжує подій");

  // --- нові принтери не породжують подій ---
  ovHead = ovTail = 0;
  feedFrame("FW|3|p57\x1f" "1. A1,p77\x1f" "2. A1,e0\x1f" "9. NEW\n");
  check(queued() == 0, "новий принтер не породжує подій");

  // --- ERR для вже відомого принтера таки надходить ---
  ovHead = ovTail = 0;
  feedFrame("FW|3|e0\x1f" "1. A1,p78\x1f" "2. A1,e0\x1f" "9. NEW\n");
  check(queued() == 1, "перехід у ERR дає подію");

  // --- самоперезавантаження після втрати звʼязку ---
  Serial.feed("");
  ESP.restarted = false;
  g_millis += 4 * 60 * 1000;
  fw_loop();
  check(!ESP.restarted, "4 хвилини тиші: без перезавантаження");
  g_millis += 2 * 60 * 1000;
  fw_loop();
  check(ESP.restarted, "6 хвилин тиші після звʼязку: перезавантаження");

  // --- поки farmwatch просто вимкнений, вічного циклу ребутів нема ---
  ESP.restarted = false;
  haveFirst = false;                       // стан свіжозавантаженої плати
  lastFrameMs = g_millis - LINK_TIMEOUT_MS;
  g_millis += 30 * 60 * 1000;
  fw_loop();
  check(!ESP.restarted, "без жодного кадру з моменту старту: без ребуту");
  check(linkLost(), "у цьому стані дисплей показує NO LINK");

  printf("\n%s (%d перевірок не пройшло)\n", failures ? "ЩОСЬ ЗЛАМАНО" : "ВСЕ ОК", failures);
  return failures ? 1 : 0;
}
