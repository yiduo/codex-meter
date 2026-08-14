#include <Arduino.h>
#include <ArduinoJson.h>
#include <BLEAdvertising.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <M5Unified.h>
#include <Preferences.h>

#include "config.h"

namespace {

constexpr char kServiceUuid[] = "7d6a1000-8f3b-4b6d-9f6a-4d3558438d01";
constexpr char kDataUuid[] = "7d6a1001-8f3b-4b6d-9f6a-4d3558438d01";
constexpr char kStatusUuid[] = "7d6a1002-8f3b-4b6d-9f6a-4d3558438d01";
constexpr size_t kPayloadCapacity = 1024;

constexpr uint16_t kBackground = 0x0841;
constexpr uint16_t kPanel = 0x1082;
constexpr uint16_t kPanelLight = 0x18E3;
constexpr uint16_t kText = 0xF7BE;
constexpr uint16_t kMuted = 0x8410;
constexpr uint16_t kGreen = 0x4FEA;
constexpr uint16_t kYellow = 0xFEA0;
constexpr uint16_t kRed = 0xF9E7;
constexpr uint16_t kBlue = 0x4D7F;

struct UsageData {
  bool valid = false;
  float usedPercent = 0;
  uint64_t sessionTokens = 0;
  uint64_t todayTokens = 0;
  uint64_t weekTokens = 0;
  uint64_t todayInputTokens = 0;
  uint64_t todayCachedTokens = 0;
  uint64_t todayOutputTokens = 0;
  uint64_t todayReasoningTokens = 0;
  uint32_t resetsAt = 0;
  uint32_t updatedAt = 0;
  String windowLabel = "LIMIT";
};

UsageData usage;
BLEServer* bleServer = nullptr;
BLECharacteristic* statusCharacteristic = nullptr;
volatile bool bleConnected = false;
volatile bool restartAdvertising = false;

portMUX_TYPE payloadMux = portMUX_INITIALIZER_UNLOCKED;
char incomingPayload[kPayloadCapacity] = {};
size_t incomingLength = 0;
char pendingPayload[kPayloadCapacity] = {};
volatile size_t pendingLength = 0;
volatile bool pendingReady = false;

uint8_t page = 0;
uint32_t lastSyncAt = 0;
uint32_t lastStatusRedrawAt = 0;
uint32_t lastInteractionAt = 0;
uint32_t lastMotionSampleAt = 0;
uint32_t lastPowerCheckAt = 0;
bool screenAwake = true;
bool externalPower = false;
bool accelSampleValid = false;
float previousAccelX = 0;
float previousAccelY = 0;
float previousAccelZ = 0;
uint8_t displayRotation = 0;
bool orientationCandidateValid = false;
uint8_t orientationCandidateRotation = 0;
uint32_t orientationCandidateSince = 0;
String errorMessage;
float guardedUsedPercent = -1.0f;
uint32_t guardedResetsAt = 0;

float remainingPercent() {
  return constrain(100.0f - usage.usedPercent, 0.0f, 100.0f);
}

bool dataIsStale() {
  return lastSyncAt == 0 || millis() - lastSyncAt > BLE_STALE_AFTER_MS;
}

uint16_t gaugeColor() {
  if (!usage.valid) return kMuted;
  float remaining = remainingPercent();
  if (remaining >= 60.0f) return kGreen;
  if (remaining >= 30.0f) return kYellow;
  return kRed;
}

uint16_t statusColor() {
  if (!usage.valid) return kMuted;
  if (dataIsStale()) return kYellow;
  return gaugeColor();
}

String compactNumber(uint64_t value) {
  if (value >= 1000000000ULL) return String(value / 1000000000.0, 1) + "B";
  if (value >= 1000000ULL) return String(value / 1000000.0, 1) + "M";
  if (value >= 1000ULL) return String(value / 1000.0, 1) + "K";
  return String(static_cast<unsigned long>(value));
}

String timeUntilReset() {
  if (usage.resetsAt == 0 || usage.updatedAt == 0 || usage.resetsAt <= usage.updatedAt) {
    return "RESET --";
  }
  uint32_t seconds = usage.resetsAt - usage.updatedAt;
  uint32_t days = seconds / 86400;
  uint32_t hours = (seconds % 86400) / 3600;
  if (days > 0) return "RESET " + String(days) + "D " + String(hours) + "H";
  uint32_t minutes = (seconds % 3600) / 60;
  return "RESET " + String(hours) + "H " + String(minutes) + "M";
}

void drawHeader(const char* title) {
  auto& d = M5.Display;
  d.fillScreen(kBackground);
  d.fillCircle(10, 12, 3, statusColor());
  d.setFont(&fonts::Font2);
  d.setTextColor(kText, kBackground);
  d.setTextDatum(middle_left);
  d.drawString(title, 18, 12);

  int battery = constrain(M5.Power.getBatteryLevel(), 0, 100);
  d.drawRoundRect(108, 7, 21, 10, 2, kMuted);
  d.fillRect(130, 10, 2, 4, kMuted);
  int fillWidth = map(battery, 0, 100, 0, 17);
  if (fillWidth > 0) d.fillRoundRect(110, 9, fillWidth, 6, 1, battery < 20 ? kRed : kGreen);
}

void drawFooter(const char* left, const char* right) {
  auto& d = M5.Display;
  d.drawFastHLine(8, 220, 119, kPanelLight);
  d.setFont(&fonts::Font0);
  d.setTextColor(kMuted, kBackground);
  d.setTextDatum(middle_left);
  d.drawString(left, 9, 231);
  d.setTextDatum(middle_right);
  d.drawString(right, 126, 231);
}

void drawOverview() {
  drawHeader("CODEX");
  auto& d = M5.Display;
  const int cx = 67;
  const int cy = 76;
  const float remaining = remainingPercent();
  const int progress = usage.valid
                           ? constrain(static_cast<int>(remaining * 3.6f), 0, 360)
                           : 0;

  d.fillCircle(cx, cy, 49, kPanelLight);
  if (progress > 0) d.fillArc(cx, cy, 49, 0, 0, progress, gaugeColor());
  d.setTextDatum(middle_center);
  d.setFont(&fonts::Font4);
  String percent = usage.valid ? String(static_cast<int>(roundf(remaining))) : "--";
  d.setTextColor(kBackground);
  d.drawString(percent + "%", cx + 1, cy);
  d.drawString(percent + "%", cx, cy + 1);
  d.setTextColor(kText);
  d.drawString(percent + "%", cx, cy - 1);
  d.setFont(&fonts::Font0);
  String windowText = usage.windowLabel + " LEFT";
  d.setTextColor(kBackground);
  d.drawString(windowText, cx + 1, 108);
  d.setTextColor(kText);
  d.drawString(windowText, cx, 107);

  d.fillRoundRect(8, 126, 119, 83, 8, kPanel);
  d.setTextDatum(top_left);
  d.setFont(&fonts::Font0);
  d.setTextColor(kMuted, kPanel);
  d.drawString("TODAY TOKENS", 17, 136);
  d.setFont(&fonts::Font4);
  d.setTextColor(kText, kPanel);
  d.drawString(compactNumber(usage.todayTokens), 16, 150);
  d.drawFastHLine(16, 178, 103, kPanelLight);
  d.setFont(&fonts::Font0);
  d.setTextColor(kMuted, kPanel);
  d.setTextDatum(top_left);
  d.drawString("OUTPUT", 16, 182);
  d.drawString("7 DAYS", 73, 182);
  d.setTextColor(kText, kPanel);
  d.setTextDatum(top_right);
  d.drawString(compactNumber(usage.todayOutputTokens), 62, 195);
  d.drawString(compactNumber(usage.weekTokens), 119, 195);
  drawFooter("A  PAGE", "B  PAIR");
}

void drawMetricRow(int y, const char* label, uint64_t value, uint16_t color,
                   uint64_t maxValue) {
  auto& d = M5.Display;
  d.setFont(&fonts::Font0);
  d.setTextDatum(top_left);
  d.setTextColor(kMuted, kBackground);
  d.drawString(label, 10, y);
  d.setTextDatum(top_right);
  d.setTextColor(kText, kBackground);
  d.drawString(compactNumber(value), 125, y);
  d.fillRoundRect(10, y + 15, 115, 6, 3, kPanelLight);
  int width = maxValue == 0 ? 0 : static_cast<int>((value * 115ULL) / maxValue);
  width = constrain(width, 0, 115);
  if (width > 0) d.fillRoundRect(10, y + 15, width, 6, 3, color);
}

void drawDetails() {
  drawHeader("TODAY");
  auto& d = M5.Display;
  uint64_t maxValue = max(max(usage.todayInputTokens, usage.todayCachedTokens),
                          max(usage.todayOutputTokens, usage.todayReasoningTokens));
  drawMetricRow(36, "INPUT", usage.todayInputTokens, kBlue, maxValue);
  drawMetricRow(74, "CACHED", usage.todayCachedTokens, kGreen, maxValue);
  drawMetricRow(112, "OUTPUT", usage.todayOutputTokens, kYellow, maxValue);
  drawMetricRow(150, "REASONING", usage.todayReasoningTokens, 0xB63F, maxValue);
  d.fillRoundRect(10, 190, 115, 21, 5, kPanel);
  d.setFont(&fonts::Font0);
  d.setTextDatum(middle_center);
  d.setTextColor(kMuted, kPanel);
  d.drawString(timeUntilReset(), 67, 200);
  drawFooter("A  PAGE", "B  PAIR");
}

void drawBluetooth() {
  drawHeader("BLUETOOTH");
  auto& d = M5.Display;
  d.setFont(&fonts::Font0);
  d.setTextDatum(top_left);
  d.setTextColor(kMuted, kBackground);
  d.drawString("DEVICE", 10, 39);
  d.drawString("LINK", 10, 82);
  d.drawString("DATA", 10, 125);
  d.drawString("LAST SYNC", 10, 168);

  d.setFont(&fonts::Font2);
  d.setTextColor(kText, kBackground);
  d.drawString(BLE_DEVICE_NAME, 10, 53);
  d.setTextColor(bleConnected ? kBlue : kYellow, kBackground);
  d.drawString(bleConnected ? "CONNECTED" : "ADVERTISING", 10, 96);
  d.setTextColor(usage.valid ? (dataIsStale() ? kYellow : kGreen) : kMuted, kBackground);
  d.drawString(usage.valid ? (dataIsStale() ? "STALE" : "LIVE") : "WAITING", 10, 139);
  d.setTextColor(kText, kBackground);
  String age = lastSyncAt == 0 ? "--" : String((millis() - lastSyncAt) / 1000) + " SEC AGO";
  d.drawString(age, 10, 182);

  if (!errorMessage.isEmpty()) {
    d.setFont(&fonts::Font0);
    d.setTextColor(kRed, kBackground);
    d.drawString(errorMessage.substring(0, 19), 10, 207);
  }
  drawFooter("A  PAGE", "B  ADVERTISE");
}

void drawLandscapeBattery() {
  auto& d = M5.Display;
  int battery = constrain(M5.Power.getBatteryLevel(), 0, 100);
  d.drawRoundRect(212, 5, 21, 10, 2, kMuted);
  d.fillRect(234, 8, 2, 4, kMuted);
  int fillWidth = map(battery, 0, 100, 0, 17);
  if (fillWidth > 0) {
    d.fillRoundRect(214, 7, fillWidth, 6, 1, battery < 20 ? kRed : kGreen);
  }
}

void drawLandscapeHeader(const char* title, const String& subtitle = "") {
  auto& d = M5.Display;
  d.fillScreen(kBackground);
  d.fillCircle(8, 10, 3, statusColor());
  d.setFont(&fonts::Font2);
  d.setTextColor(kText, kBackground);
  d.setTextDatum(middle_left);
  d.drawString(title, 15, 10);
  if (!subtitle.isEmpty()) {
    d.setFont(&fonts::Font0);
    d.setTextColor(kMuted, kBackground);
    d.setTextDatum(middle_right);
    d.drawString(subtitle, 205, 10);
  }
  drawLandscapeBattery();
}

void drawLandscapeFooter(const char* left, const char* right) {
  auto& d = M5.Display;
  d.drawFastHLine(7, 125, 226, kPanelLight);
  d.setFont(&fonts::Font0);
  d.setTextColor(kMuted, kBackground);
  d.setTextDatum(middle_left);
  d.drawString(left, 8, 131);
  d.setTextDatum(middle_right);
  d.drawString(right, 232, 131);
}

void drawLandscapeOverview() {
  drawLandscapeHeader("CODEX");
  auto& d = M5.Display;
  const float remaining = remainingPercent();
  const int progress = usage.valid
                           ? constrain(static_cast<int>(remaining * 3.6f), 0, 360)
                           : 0;

  d.fillCircle(52, 70, 43, kPanelLight);
  if (progress > 0) d.fillArc(52, 70, 43, 0, 0, progress, gaugeColor());
  String percent = usage.valid ? String(static_cast<int>(roundf(remaining))) : "--";
  d.setFont(&fonts::Font4);
  d.setTextDatum(middle_center);
  d.setTextColor(kBackground);
  d.drawString(percent + "%", 53, 66);
  d.drawString(percent + "%", 52, 67);
  d.setTextColor(kText);
  d.drawString(percent + "%", 52, 65);
  d.setFont(&fonts::Font0);
  d.setTextColor(kText);
  d.drawString(usage.windowLabel + " LEFT", 52, 101);

  d.fillRoundRect(103, 26, 130, 94, 8, kPanel);
  d.setTextDatum(top_left);
  d.setFont(&fonts::Font0);
  d.setTextColor(kMuted, kPanel);
  d.drawString("TODAY TOKENS", 112, 34);
  d.setFont(&fonts::Font4);
  d.setTextColor(kText, kPanel);
  d.drawString(compactNumber(usage.todayTokens), 111, 45);
  d.drawFastHLine(111, 76, 114, kPanelLight);
  d.setFont(&fonts::Font0);
  d.setTextColor(kMuted, kPanel);
  d.drawString("OUTPUT", 111, 82);
  d.drawString("7 DAYS", 174, 82);
  d.setFont(&fonts::Font2);
  d.setTextColor(kText, kPanel);
  d.setTextDatum(top_right);
  d.drawString(compactNumber(usage.todayOutputTokens), 165, 96);
  d.drawString(compactNumber(usage.weekTokens), 225, 96);
  drawLandscapeFooter("A  PAGE", "B  PAIR");
}

void drawLandscapeMetricCard(int x, int y, const char* label, uint64_t value,
                             uint16_t color, uint64_t maxValue) {
  auto& d = M5.Display;
  constexpr int width = 110;
  d.fillRoundRect(x, y, width, 42, 6, kPanel);
  d.setFont(&fonts::Font0);
  d.setTextDatum(top_left);
  d.setTextColor(kMuted, kPanel);
  d.drawString(label, x + 7, y + 5);
  d.setFont(&fonts::Font2);
  d.setTextDatum(top_right);
  d.setTextColor(kText, kPanel);
  d.drawString(compactNumber(value), x + width - 7, y + 13);
  d.fillRoundRect(x + 7, y + 32, width - 14, 5, 2, kPanelLight);
  int barWidth = maxValue == 0
                     ? 0
                     : static_cast<int>((value * static_cast<uint64_t>(width - 14)) /
                                        maxValue);
  barWidth = constrain(barWidth, 0, width - 14);
  if (barWidth > 0) d.fillRoundRect(x + 7, y + 32, barWidth, 5, 2, color);
}

void drawLandscapeDetails() {
  drawLandscapeHeader("TODAY", timeUntilReset());
  uint64_t maxValue = max(max(usage.todayInputTokens, usage.todayCachedTokens),
                          max(usage.todayOutputTokens, usage.todayReasoningTokens));
  drawLandscapeMetricCard(7, 28, "INPUT", usage.todayInputTokens, kBlue, maxValue);
  drawLandscapeMetricCard(123, 28, "CACHED", usage.todayCachedTokens, kGreen, maxValue);
  drawLandscapeMetricCard(7, 76, "OUTPUT", usage.todayOutputTokens, kYellow, maxValue);
  drawLandscapeMetricCard(123, 76, "REASONING", usage.todayReasoningTokens, 0xB63F,
                          maxValue);
  drawLandscapeFooter("A  PAGE", "B  PAIR");
}

void drawLandscapeInfoCard(int x, int y, const char* label, const String& value,
                           uint16_t color) {
  auto& d = M5.Display;
  d.fillRoundRect(x, y, 110, 42, 6, kPanel);
  d.setFont(&fonts::Font0);
  d.setTextDatum(top_left);
  d.setTextColor(kMuted, kPanel);
  d.drawString(label, x + 7, y + 5);
  d.setFont(&fonts::Font2);
  d.setTextColor(color, kPanel);
  d.drawString(value, x + 7, y + 19);
}

void drawLandscapeBluetooth() {
  drawLandscapeHeader("BLUETOOTH", errorMessage.substring(0, 16));
  String age = lastSyncAt == 0 ? "--" : String((millis() - lastSyncAt) / 1000) + " SEC AGO";
  drawLandscapeInfoCard(7, 28, "DEVICE", BLE_DEVICE_NAME, kText);
  drawLandscapeInfoCard(123, 28, "LINK",
                        bleConnected ? "CONNECTED" : "ADVERTISING",
                        bleConnected ? kBlue : kYellow);
  drawLandscapeInfoCard(7, 76, "DATA",
                        usage.valid ? (dataIsStale() ? "STALE" : "LIVE") : "WAITING",
                        usage.valid ? (dataIsStale() ? kYellow : kGreen) : kMuted);
  drawLandscapeInfoCard(123, 76, "LAST SYNC", age, kText);
  drawLandscapeFooter("A  PAGE", "B  ADVERTISE");
}

void drawCurrentPage() {
  if (displayRotation & 1) {
    if (page == 0) drawLandscapeOverview();
    else if (page == 1) drawLandscapeDetails();
    else drawLandscapeBluetooth();
  } else {
    if (page == 0) drawOverview();
    else if (page == 1) drawDetails();
    else drawBluetooth();
  }
}

void wakeScreen() {
  if (!screenAwake) {
    M5.Display.setBrightness(SCREEN_BRIGHTNESS);
    screenAwake = true;
  }
  lastInteractionAt = millis();
}

bool isExternalPowerPresent() {
  auto source = M5.Power.M5pm1.getPowerSource();
  bool powerSourceIsExternal =
      source == m5::M5PM1_Class::vin || source == m5::M5PM1_Class::vinout;
  bool vbusPresent = M5.Power.M5pm1.getVBUSVoltage() >= EXTERNAL_POWER_MIN_MV;
  bool charging = M5.Power.isCharging() == m5::Power_Class::is_charging;
#if ARDUINO_USB_CDC_ON_BOOT && ARDUINO_USB_MODE
  bool usbConnected = Serial.isPlugged();
#else
  bool usbConnected = false;
#endif
  return powerSourceIsExternal || vbusPresent || charging || usbConnected;
}

void updatePowerState() {
  uint32_t now = millis();
  if (now - lastPowerCheckAt < POWER_CHECK_INTERVAL_MS) return;
  lastPowerCheckAt = now;

  bool present = isExternalPowerPresent();
  if (present == externalPower) return;
  externalPower = present;
  if (externalPower) {
    wakeScreen();
    drawCurrentPage();
  } else {
    // 拔掉电源后重新开始计算 30 秒，而不是立即降低亮度。
    lastInteractionAt = now;
  }
}

bool updateOrientation(float accelX, float accelY) {
  float uprightAxis = accelX * ORIENTATION_UPRIGHT_X_SIGN;
  float dominant = max(fabsf(uprightAxis), fabsf(accelY));
  if (dominant < ORIENTATION_TRIGGER_G) {
    orientationCandidateValid = false;
    return false;
  }

  uint8_t candidate = 0;
  if (fabsf(uprightAxis) >= fabsf(accelY)) {
    candidate = uprightAxis >= 0 ? 0 : 2;
  } else {
    // M5GFX 的横屏旋转编号与机身 Y 轴方向相反：交换 1/3 才能保持文字朝上。
    candidate = accelY <= 0 ? 3 : 1;
  }

  uint32_t now = millis();
  if (!orientationCandidateValid || candidate != orientationCandidateRotation) {
    orientationCandidateValid = true;
    orientationCandidateRotation = candidate;
    orientationCandidateSince = now;
    return false;
  }
  if (candidate == displayRotation || now - orientationCandidateSince < ORIENTATION_STABLE_MS) {
    return false;
  }

  displayRotation = candidate;
  M5.Display.setRotation(displayRotation);
  return true;
}

void updateMotionAndOrientation() {
  if (!M5.Imu.isEnabled() || millis() - lastMotionSampleAt < IMU_SAMPLE_INTERVAL_MS) return;
  lastMotionSampleAt = millis();
  float x = 0;
  float y = 0;
  float z = 0;
  if (!M5.Imu.getAccelData(&x, &y, &z)) return;

  bool orientationChanged = updateOrientation(x, y);
  bool wokeForMotion = false;
  if (!accelSampleValid) {
    previousAccelX = x;
    previousAccelY = y;
    previousAccelZ = z;
    accelSampleValid = true;
  } else {
    float delta = fabsf(x - previousAccelX) + fabsf(y - previousAccelY) + fabsf(z - previousAccelZ);
    previousAccelX = x;
    previousAccelY = y;
    previousAccelZ = z;
    if (!screenAwake && delta >= MOTION_THRESHOLD_G) {
      wakeScreen();
      wokeForMotion = true;
    }
  }

  if (orientationChanged || wokeForMotion) drawCurrentPage();
}

void drawStarting() {
  auto& d = M5.Display;
  d.fillScreen(kBackground);
  d.fillRoundRect(48, 47, 39, 39, 10, kBlue);
  d.setFont(&fonts::Font4);
  d.setTextDatum(middle_center);
  d.setTextColor(kBackground, kBlue);
  d.drawString("B", 67, 66);
  d.setFont(&fonts::Font2);
  d.setTextColor(kText, kBackground);
  d.drawString("CODEX METER", 67, 108);
  d.setFont(&fonts::Font0);
  d.setTextColor(kMuted, kBackground);
  d.drawString("STARTING BLUETOOTH", 67, 132);
}

void loadLimitGuard() {
  Preferences preferences;
  if (!preferences.begin("codexmeter", true)) return;
  guardedUsedPercent = preferences.getFloat("max_used", -1.0f);
  guardedResetsAt = preferences.getULong("limit_reset", 0);
  preferences.end();
}

void saveLimitGuard(float usedPercent, uint32_t resetsAt) {
  guardedUsedPercent = usedPercent;
  guardedResetsAt = resetsAt;
  Preferences preferences;
  if (!preferences.begin("codexmeter", false)) return;
  preferences.putFloat("max_used", usedPercent);
  preferences.putULong("limit_reset", resetsAt);
  preferences.end();
}

bool applyPayload(const char* payload, size_t length) {
  JsonDocument doc;
  DeserializationError jsonError = deserializeJson(doc, payload, length);
  if (jsonError) {
    errorMessage = "INVALID JSON";
    return false;
  }
  const char* receivedKey = doc["key"] | "";
  if (strlen(BLE_SHARED_KEY) > 0 && strcmp(receivedKey, BLE_SHARED_KEY) != 0) {
    errorMessage = "BAD SHARED KEY";
    return false;
  }

  const char* limitId = doc["limit_id"] | "";
  if (strcmp(limitId, "codex") != 0) {
    errorMessage = "WRONG LIMIT";
    return false;
  }

  bool incomingValid = doc["valid"] | false;
  float incomingUsedPercent = doc["used_percent"] | 0.0f;
  uint32_t incomingResetsAt = doc["resets_at"] | 0;
  if (incomingValid && (!isfinite(incomingUsedPercent) || incomingUsedPercent < 0.0f ||
                        incomingUsedPercent > 100.0f || incomingResetsAt == 0)) {
    errorMessage = "INVALID LIMIT";
    return false;
  }
  if (incomingValid && guardedResetsAt != 0) {
    // 官方重置后 resets_at 会切换到新的周期值；不比较时间戳大小，直接接受。
    // 只有周期值完全相同时，已用百分比才必须保持单调不降。
    if (incomingResetsAt == guardedResetsAt &&
        incomingUsedPercent + LIMIT_PERCENT_DROP_TOLERANCE < guardedUsedPercent) {
      errorMessage = "LIMIT DROP BLOCKED";
      return false;
    }
  }

  usage.valid = incomingValid;
  usage.usedPercent = incomingUsedPercent;
  usage.sessionTokens = doc["session_tokens"].as<uint64_t>();
  usage.todayTokens = doc["today_tokens"].as<uint64_t>();
  usage.weekTokens = doc["week_tokens"].as<uint64_t>();
  usage.todayInputTokens = doc["today_input_tokens"].as<uint64_t>();
  usage.todayCachedTokens = doc["today_cached_input_tokens"].as<uint64_t>();
  usage.todayOutputTokens = doc["today_output_tokens"].as<uint64_t>();
  usage.todayReasoningTokens = doc["today_reasoning_output_tokens"].as<uint64_t>();
  usage.resetsAt = incomingResetsAt;
  usage.updatedAt = doc["updated_at"] | 0;
  usage.windowLabel = doc["window_label"] | "LIMIT";
  errorMessage = usage.valid ? "" : "LIMIT UNAVAILABLE";
  lastSyncAt = millis();
  wakeScreen();

  if (usage.valid &&
      (usage.resetsAt != guardedResetsAt || usage.usedPercent > guardedUsedPercent)) {
    saveLimitGuard(usage.usedPercent, usage.resetsAt);
  }

  if (statusCharacteristic != nullptr) {
    String status = String(usage.updatedAt);
    statusCharacteristic->setValue(status.c_str());
  }
  return true;
}

class ServerCallbacks : public BLEServerCallbacks {
 public:
  void onConnect(BLEServer*) override { bleConnected = true; }
  void onDisconnect(BLEServer*) override {
    bleConnected = false;
    restartAdvertising = true;
  }
};

class DataCallbacks : public BLECharacteristicCallbacks {
 public:
  void onWrite(BLECharacteristic* characteristic) override {
    std::string chunk = characteristic->getValue();
    portENTER_CRITICAL(&payloadMux);
    for (char byte : chunk) {
      if (byte == '\n') {
        if (incomingLength > 0 && incomingLength < kPayloadCapacity && !pendingReady) {
          memcpy(pendingPayload, incomingPayload, incomingLength);
          pendingPayload[incomingLength] = '\0';
          pendingLength = incomingLength;
          pendingReady = true;
        }
        incomingLength = 0;
      } else if (incomingLength < kPayloadCapacity - 1) {
        incomingPayload[incomingLength++] = byte;
      } else {
        incomingLength = 0;
      }
    }
    portEXIT_CRITICAL(&payloadMux);
  }
};

void startBluetooth() {
  BLEDevice::init(BLE_DEVICE_NAME);
  BLEDevice::setMTU(517);
  bleServer = BLEDevice::createServer();
  bleServer->setCallbacks(new ServerCallbacks());

  BLEService* service = bleServer->createService(kServiceUuid);
  BLECharacteristic* dataCharacteristic = service->createCharacteristic(
      kDataUuid, BLECharacteristic::PROPERTY_WRITE);
  dataCharacteristic->setCallbacks(new DataCallbacks());
  statusCharacteristic = service->createCharacteristic(
      kStatusUuid, BLECharacteristic::PROPERTY_READ);
  statusCharacteristic->setValue("0");
  service->start();

  BLEAdvertising* advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(kServiceUuid);
  advertising->setScanResponse(true);
  advertising->setMinPreferred(0x06);
  advertising->setMaxPreferred(0x12);
  // 广播间隔单位为 0.625 ms：500–600 ms，兼顾发现速度与电池续航。
  advertising->setMinInterval(800);
  advertising->setMaxInterval(960);
  BLEDevice::startAdvertising();
}

void processPendingPayload() {
  if (!pendingReady) return;
  char localPayload[kPayloadCapacity];
  size_t localLength = 0;
  portENTER_CRITICAL(&payloadMux);
  if (pendingReady) {
    localLength = pendingLength;
    memcpy(localPayload, pendingPayload, localLength);
    localPayload[localLength] = '\0';
    pendingReady = false;
    pendingLength = 0;
  }
  portEXIT_CRITICAL(&payloadMux);
  if (localLength > 0) {
    applyPayload(localPayload, localLength);
    drawCurrentPage();
  }
}

}  // namespace

void setup() {
  auto config = M5.config();
  config.serial_baudrate = 115200;
  M5.begin(config);
  M5.Display.setRotation(0);
  M5.Display.setBrightness(SCREEN_BRIGHTNESS);
  loadLimitGuard();
  externalPower = isExternalPowerPresent();
  lastInteractionAt = millis();
  drawStarting();
  startBluetooth();
  delay(300);
  drawCurrentPage();
}

void loop() {
  M5.update();
  processPendingPayload();
  updatePowerState();

  if (restartAdvertising) {
    restartAdvertising = false;
    delay(50);
    BLEDevice::startAdvertising();
    if (page == 2) drawCurrentPage();
  }
  if (M5.BtnA.wasPressed()) {
    bool wasSleeping = !screenAwake;
    wakeScreen();
    if (!wasSleeping) page = (page + 1) % 3;
    drawCurrentPage();
  }
  if (M5.BtnB.wasPressed()) {
    bool wasSleeping = !screenAwake;
    wakeScreen();
    if (!wasSleeping) {
      if (!bleConnected) BLEDevice::startAdvertising();
      errorMessage = "";
    }
    drawCurrentPage();
  }
  if (screenAwake && page == 2 && millis() - lastStatusRedrawAt >= 1000) {
    lastStatusRedrawAt = millis();
    drawCurrentPage();
  }
  if (!externalPower && screenAwake && millis() - lastInteractionAt >= SCREEN_TIMEOUT_MS) {
    M5.Display.setBrightness(SCREEN_DIM_BRIGHTNESS);
    screenAwake = false;
  }
  updateMotionAndOrientation();
  delay(20);
}
