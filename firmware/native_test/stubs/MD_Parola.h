#pragma once
#include "MD_MAX72xx.h"
enum textPosition_t { PA_LEFT, PA_CENTER, PA_RIGHT };
enum textEffect_t { PA_SCROLL_LEFT, PA_SCROLL_UP, PA_SCROLL_DOWN };

struct MD_Parola {
  const char *lastText = nullptr;
  MD_Parola(MD_MAX72XX::moduleType_t, int, int, int, int) {}
  void begin() {}
  void setIntensity(int) {}
  void setInvert(bool) {}
  void displayClear() {}
  void displayText(const char *t, int, int, int, int, int) { lastText = t; }
  bool displayAnimate() { return false; }
};
