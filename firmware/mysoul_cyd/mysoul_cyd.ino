#include <Arduino.h>
#include <SPI.h>
#include <Preferences.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <WiFiUdp.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <XPT2046_Touchscreen.h>
#include "secrets.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

namespace Pins {
constexpr int tftSclk = 14, tftMosi = 13, tftMiso = 12, tftDc = 2, tftCs = 15, backlight = 21;
constexpr int touchSclk = 25, touchMosi = 32, touchMiso = 39, touchCs = 33, speaker = 26;
constexpr int sdCs = 5;
}

constexpr uint16_t NAVY = 0x0042;
constexpr uint16_t BG = NAVY;
constexpr uint16_t PAPER = 0xF75B;
constexpr uint16_t MUTED = 0x8BCD;
constexpr uint16_t GOLD = 0xE58B;
constexpr int kCalX[5] = {36, 284, 284, 36, 160};
constexpr int kCalY[5] = {36, 36, 204, 204, 120};
constexpr int kOrbX = 160;
constexpr int kOrbY = 96;

SPIClass tftSpi(VSPI);
SPIClass touchSpi(HSPI);
Adafruit_ILI9341 lcd(&tftSpi, Pins::tftDc, Pins::tftCs, -1);
XPT2046_Touchscreen touch(Pins::touchCs);
Preferences prefs;
WebServer web(80);
WiFiUDP udp;

bool webOn = false;
bool gwOk = false;
bool calibrating = false;
bool haveCal = false;
bool waitRelease = false;
bool touchHeld = false;
int calStep = 0;
int calN = 0;
int calXs[20], calYs[20];
int calX[4], calY[4];
int touchL = 300, touchR = 3900, touchT = 300, touchB = 3900, touchSwap = 0;
int heldX = 0, heldY = 0;
IPAddress gwIp;
int gwPort = 8791;
uint32_t lastWifi = 0;
uint32_t lastSeek = 0;
uint32_t lastPoll = 0;
uint32_t lastFrame = 0;
uint32_t lastTap = 0;
int pollMiss = 0;
String uiState = "offline";
String uiLine = "Sem rede";
String uiScreen;
String uiIp = "...";
bool serialPc = false;
String serialLine;
bool chromeDirty = true;
bool orbDirty = false;
uint32_t listenSince = 0;
int page = 0;
bool pageDirty = true;
String pgOcupacao = "Sem PC";
String pgAgir = "Sem leitura";
String pgTrabalho = "-";
String pgCansaco = "-";
String pgEstresse = "-";
String pgEsgota = "-";
String pgEmp[5] = {"-", "-", "-", "-", "-"};
String pgReply = "Escolha como falar";
String pgOpt[3] = {"Dia", "Saude", "Firmas"};
int guideStep = 0;
int guidePath = 0;
const char* kEmpName[5] = {"Vale", "Hatkg", "CPQD", "AlliedIT", "CH8ai"};
uint32_t lastPages = 0;
uint32_t lastHello = 0;

const uint16_t kGold[] = {0x3942, 0x6A42, 0xABA4, 0xD509, 0xF64F, 0xFF98};
const uint16_t kDim[] = {0x20A1, 0x3982, 0x5A84, 0x7B46, 0x9C28, 0xB4AA};

int medianN(int* values, int count) {
  for (int i = 1; i < count; i++) {
    int key = values[i];
    int j = i;
    while (j > 0 && values[j - 1] > key) {
      values[j] = values[j - 1];
      j--;
    }
    values[j] = key;
  }
  return values[count / 2];
}

void beep(int hz = 1200, int ms = 28) { tone(Pins::speaker, hz, ms); }

void center(const char* text, int x, int y, uint8_t size, uint16_t color) {
  lcd.setFont();
  lcd.setTextSize(size);
  lcd.setTextColor(color);
  int16_t x1, y1;
  uint16_t w, h;
  lcd.getTextBounds(text, 0, 0, &x1, &y1, &w, &h);
  lcd.setCursor(x - (int)w / 2, y);
  lcd.print(text);
}

void center(const String& text, int x, int y, uint8_t size, uint16_t color) {
  center(text.c_str(), x, y, size, color);
}

bool pressedNow(TS_Point& point) {
  point = touch.getPoint();
  return touch.touched() || point.z > 120;
}

void mapRaw(int rawX, int rawY, int& screenX, int& screenY) {
  int axisX = touchSwap ? rawY : rawX;
  int axisY = touchSwap ? rawX : rawY;
  screenX = constrain(map(axisX, touchL, touchR, kCalX[0], kCalX[1]), 0, 319);
  screenY = constrain(map(axisY, touchT, touchB, kCalY[0], kCalY[2]), 0, 239);
}

void loadCal() {
  prefs.begin("mysoul", true);
  if (prefs.getInt("calv", 0) >= 2 && prefs.isKey("tl")) {
    touchL = prefs.getInt("tl", touchL);
    touchR = prefs.getInt("tr", touchR);
    touchT = prefs.getInt("tt", touchT);
    touchB = prefs.getInt("tb", touchB);
    touchSwap = prefs.getInt("swap", 0);
    haveCal = abs(touchR - touchL) > 500 && abs(touchB - touchT) > 400;
  }
  prefs.end();
}

void saveCal() {
  prefs.begin("mysoul", false);
  prefs.putInt("tl", touchL);
  prefs.putInt("tr", touchR);
  prefs.putInt("tt", touchT);
  prefs.putInt("tb", touchB);
  prefs.putInt("swap", touchSwap);
  prefs.putInt("calv", 3);
  prefs.end();
  haveCal = true;
}

bool computeCal() {
  int dx = abs(calX[1] - calX[0]) + abs(calX[2] - calX[3]);
  int dy = abs(calY[3] - calY[0]) + abs(calY[2] - calY[1]);
  int crossX = abs(calY[1] - calY[0]) + abs(calY[2] - calY[3]);
  int crossY = abs(calX[3] - calX[0]) + abs(calX[2] - calX[1]);
  touchSwap = (crossX + crossY) > (dx + dy) ? 1 : 0;
  int left, right, top, bottom;
  if (!touchSwap) {
    left = (calX[0] + calX[3]) / 2;
    right = (calX[1] + calX[2]) / 2;
    top = (calY[0] + calY[1]) / 2;
    bottom = (calY[2] + calY[3]) / 2;
  } else {
    left = (calY[0] + calY[3]) / 2;
    right = (calY[1] + calY[2]) / 2;
    top = (calX[0] + calX[1]) / 2;
    bottom = (calX[2] + calX[3]) / 2;
  }
  if (abs(right - left) < 500 || abs(bottom - top) < 400) return false;
  touchL = left;
  touchR = right;
  touchT = top;
  touchB = bottom;
  return true;
}

void drawCalTarget() {
  lcd.fillScreen(BG);
  int x = kCalX[calStep];
  int y = kCalY[calStep];
  lcd.drawLine(x - 12, y, x + 12, y, GOLD);
  lcd.drawLine(x, y - 12, x, y + 12, GOLD);
  lcd.drawCircle(x, y, 8, GOLD);
  if (calStep < 4) center("Toque a cruz", 160, 8, 1, MUTED);
  else center("Toque o centro", 160, 8, 1, GOLD);
}

String gatewayUrl(const String& path) {
  String url = "http://" + gwIp.toString() + ":" + String(gwPort) + path;
  bool first = true;
  for (int i = 0; i < web.args(); i++) {
    if (web.argName(i) == "plain") continue;
    url += first ? "?" : "&";
    first = false;
    url += web.argName(i);
    url += "=";
    url += web.arg(i);
  }
  return url;
}

void proxyPage() {
  if (!gwOk || WiFi.status() != WL_CONNECTED) {
    web.send(503, "text/html",
             "<!DOCTYPE html><meta charset=utf-8><title>MySoul</title>"
             "<body style=\"margin:0;background:#140e0a;color:#f3eadc;font:20px Georgia,serif;padding:32px\">"
             "<h1>MySoul</h1><p>A placa ainda nao achou o computador. "
             "Deixe o gateway ligado na mesma rede e toque o orb.</p></body>");
    return;
  }
  HTTPClient http;
  http.setConnectTimeout(1500);
  http.setTimeout(8000);
  if (!http.begin(gatewayUrl(web.uri()))) {
    web.send(502, "application/json", "{\"error\":\"gateway\"}");
    return;
  }
  int code;
  if (web.method() == HTTP_POST) {
    http.addHeader("Content-Type", "application/json");
    code = http.POST(web.arg("plain"));
  } else {
    code = http.GET();
  }
  String type = http.header("Content-Type");
  if (!type.length()) type = "application/json";
  String body = code > 0 ? http.getString() : String("{\"error\":\"sem resposta\"}");
  http.end();
  web.send(code > 0 ? code : 502, type, body);
}

void tryKnownPc() {
  if (gwOk || WiFi.status() != WL_CONNECTED || !PC_HOST[0]) return;
  IPAddress known;
  if (!known.fromString(PC_HOST)) return;
  HTTPClient http;
  http.setConnectTimeout(200);
  http.setTimeout(300);
  if (!http.begin(String("http://") + PC_HOST + ":8791/v1/state")) return;
  int code = http.GET();
  http.end();
  if (code != 200) return;
  gwIp = known;
  gwPort = 8791;
  gwOk = true;
  pollMiss = 0;
  uiState = "idle";
  uiLine = "Toque o orb";
  chromeDirty = true;
}

void seekGateway() {
  if (WiFi.status() != WL_CONNECTED) return;
  IPAddress subnet = WiFi.localIP();
  subnet[3] = 255;
  const char* probe = "MYSOUL?";
  udp.beginPacket(subnet, 8792);
  udp.write((const uint8_t*)probe, 7);
  udp.endPacket();
  udp.beginPacket(IPAddress(255, 255, 255, 255), 8792);
  udp.write((const uint8_t*)probe, 7);
  udp.endPacket();
}

void readDiscovery() {
  int size = udp.parsePacket();
  if (size <= 0) return;
  char buffer[32];
  int count = udp.read(buffer, sizeof(buffer) - 1);
  if (count < 0) count = 0;
  buffer[count] = 0;
  if (strncmp(buffer, "MYSOUL", 6) != 0) return;
  gwIp = udp.remoteIP();
  int port = atoi(buffer + 6);
  if (port <= 0) port = 8791;
  gwPort = port;
  gwOk = true;
  pollMiss = 0;
  if (uiLine == "Sem PC" || uiLine == "Sem rede" || uiState == "offline") {
    uiState = "idle";
    uiLine = "Toque o orb";
    chromeDirty = true;
  }
  Serial.printf("gateway %s:%d\n", gwIp.toString().c_str(), gwPort);
}

bool pollGateway() {
  if (!gwOk) return false;
  HTTPClient http;
  http.setConnectTimeout(400);
  http.setTimeout(700);
  String url = "http://" + gwIp.toString() + ":" + String(gwPort) + "/v1/state";
  if (!http.begin(url)) return false;
  int code = http.GET();
  String body = code == 200 ? http.getString() : "";
  http.end();
  if (code != 200) return false;
  String nextState = uiState;
  String nextLine = uiLine;
  String nextScreen = uiScreen;
  int from = 0;
  while (from < (int)body.length()) {
    int end = body.indexOf('\n', from);
    if (end < 0) end = body.length();
    String line = body.substring(from, end);
    line.trim();
    int cut = line.indexOf('=');
    if (cut > 0) {
      String key = line.substring(0, cut);
      String value = line.substring(cut + 1);
      value.trim();
      if (key == "state") nextState = value;
      else if (key == "line") nextLine = value;
      else if (key == "screen") nextScreen = value;
    }
    from = end + 1;
  }
  if (nextState == "idle" && uiState == "listening" && millis() - lastTap < 1200) {
    nextState = "listening";
    nextLine = "Ouvindo";
  }
  if (nextState != uiState || nextLine != uiLine || nextScreen != uiScreen) {
    uiState = nextState;
    uiLine = nextLine;
    uiScreen = nextScreen;
    chromeDirty = true;
  }
  return true;
}

void takeField(const String& body, const char* key, String& dest) {
  String token = String(key) + "=";
  int at = body.indexOf(token);
  if (at < 0) return;
  int end = body.indexOf('\n', at);
  if (end < 0) end = body.length();
  dest = body.substring(at + token.length(), end);
  dest.trim();
}

void pollPages() {
  if (!gwOk) return;
  HTTPClient http;
  http.setConnectTimeout(250);
  http.setTimeout(350);
  String url = "http://" + gwIp.toString() + ":" + String(gwPort) + "/v1/pages";
  if (!http.begin(url)) return;
  int code = http.GET();
  String body = code == 200 ? http.getString() : "";
  http.end();
  if (code != 200 || !body.length()) return;
  String nextOcupacao = pgOcupacao;
  String nextAgir = pgAgir;
  takeField(body, "ocupacao", nextOcupacao);
  takeField(body, "agir", nextAgir);
  takeField(body, "trabalho", pgTrabalho);
  takeField(body, "cansaco", pgCansaco);
  takeField(body, "estresse", pgEstresse);
  takeField(body, "esgota", pgEsgota);
  takeField(body, "vale", pgEmp[0]);
  takeField(body, "hatkg", pgEmp[1]);
  takeField(body, "cpqd", pgEmp[2]);
  takeField(body, "allied", pgEmp[3]);
  takeField(body, "ch8", pgEmp[4]);
  bool pagesChanged = nextOcupacao != pgOcupacao || nextAgir != pgAgir;
  pgOcupacao = nextOcupacao;
  pgAgir = nextAgir;
  if (pagesChanged && page == 1) pageDirty = true;
}

void startCal() {
  calibrating = true;
  calStep = 0;
  calN = 0;
  waitRelease = true;
  drawCalTarget();
}

void readSerialPc() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      serialLine.trim();
      if (serialLine == "ok" || serialLine == "PC") {
        serialPc = true;
        if (uiState == "offline" || uiLine == "Sem PC" || uiLine == "Sem rede" || uiLine == "PC no cabo") {
          uiState = "idle";
          uiLine = "Toque para ouvir";
          chromeDirty = true;
        }
      } else if (serialLine.startsWith("line=")) {
        serialPc = true;
        String next = serialLine.substring(5);
        next.trim();
        bool keepListening = uiState == "listening" && next != "Pensando" && next != "Falando";
        if (!keepListening && next.length() && next != uiLine) {
          String before = uiState;
          uiLine = next;
          if (next == "Toque p/ parar" || next == "Ouvindo") uiState = "listening";
          else if (next == "Pensando") uiState = "thinking";
          else if (next == "Falando") uiState = "speaking";
          else uiState = "idle";
          if (uiState != before) orbDirty = true;
          chromeDirty = true;
        }
      } else if (serialLine.startsWith("screen=")) {
        String next = serialLine.substring(7);
        next.trim();
        if (next != uiScreen) {
          uiScreen = next;
          chromeDirty = true;
        }
      } else if (serialLine.startsWith("ocupacao=")) {
        String before = pgOcupacao;
        takeField(serialLine + "\n", "ocupacao", pgOcupacao);
        if (pgOcupacao != before && page == 1) pageDirty = true;
      } else if (serialLine.startsWith("reply=")) {
        String before = pgReply;
        takeField(serialLine + "\n", "reply", pgReply);
        if (pgReply != before && page == 7) pageDirty = true;
      }
      serialLine = "";
    } else if (c != '\r' && serialLine.length() < 180) {
      serialLine += c;
    }
  }
}

void drawPage();

void sendBoard(const char* msg) {
  pgReply = "Enviando";
  page = 7;
  drawPage();
  bool sent = false;
  if (serialPc) {
    Serial.print("ASK ");
    Serial.println(msg);
    sent = true;
  } else if (gwOk) {
    HTTPClient http;
    http.setConnectTimeout(300);
    http.setTimeout(2500);
    String url = "http://" + gwIp.toString() + ":" + String(gwPort) + "/v1/bard";
    if (http.begin(url)) {
      http.addHeader("Content-Type", "text/plain");
      int code = http.POST((uint8_t*)msg, strlen(msg));
      if (code == 200) {
        String body = http.getString();
        takeField(body, "reply", pgReply);
        takeField(body, "opt1", pgOpt[0]);
        takeField(body, "opt2", pgOpt[1]);
        takeField(body, "opt3", pgOpt[2]);
        sent = true;
      }
      http.end();
    }
  }
  if (!sent) pgReply = "Sem PC";
  else if (pgReply == "Enviando") pgReply = msg;
  page = 7;
  drawPage();
}

void postTap() {
  if ((!gwOk && !serialPc) || millis() - lastTap < 450) return;
  if (uiState == "thinking" || uiState == "speaking") return;
  lastTap = millis();
  beep();
  if (uiState == "listening") {
    uiState = "thinking";
    uiLine = "Pensando";
    listenSince = 0;
  } else {
    uiState = "listening";
    uiLine = "Toque p/ parar";
    uiScreen = "";
    listenSince = millis();
  }
  chromeDirty = true;
  orbDirty = true;
  if (serialPc) {
    Serial.println("TAP");
    return;
  }
  HTTPClient http;
  http.setConnectTimeout(300);
  http.setTimeout(500);
  String url = "http://" + gwIp.toString() + ":" + String(gwPort) + "/v1/tap";
  if (!http.begin(url)) return;
  http.POST("");
  http.end();
}

void drawOrb() {
  const uint16_t* palette = (uiState == "thinking" || uiState == "offline") ? kDim : kGold;
  int radius = uiState == "listening" ? 50 : 44;
  lcd.fillRect(90, 34, 140, 130, BG);
  for (int ring = 0; ring < 6; ring++) {
    int size = radius - ring * 6;
    if (size > 2) lcd.fillCircle(kOrbX, kOrbY, size, palette[ring]);
  }
  lcd.fillCircle(kOrbX - 12, kOrbY - 14, 7, 0xFFDE);
  if (uiState == "listening") lcd.drawCircle(kOrbX, kOrbY, radius + 8, GOLD);
}

void iconButton(int cx, int cy, int radius, uint16_t color, const char* label, int kind) {
  lcd.fillCircle(cx, cy, radius + 3, 0x0008);
  lcd.fillCircle(cx, cy, radius, color);
  lcd.fillCircle(cx - 6, cy - 7, radius / 5, 0xFFFF);
  if (kind == 0) {
    lcd.fillCircle(cx, cy, 8, 0xFFFF);
  } else if (kind == 1) {
    lcd.fillRect(cx - 8, cy - 6, 16, 14, 0xFFFF);
    lcd.fillRect(cx - 8, cy - 6, 16, 4, color);
  } else if (kind == 2) {
    lcd.fillRect(cx - 2, cy - 8, 4, 16, 0xFFFF);
    lcd.fillRect(cx - 8, cy - 2, 16, 4, 0xFFFF);
  } else if (kind == 3) {
    lcd.fillRect(cx - 8, cy - 8, 6, 6, 0xFFFF);
    lcd.fillRect(cx + 2, cy - 8, 6, 6, 0xFFFF);
    lcd.fillRect(cx - 8, cy + 2, 6, 6, 0xFFFF);
    lcd.fillRect(cx + 2, cy + 2, 6, 6, 0xFFFF);
  } else {
    lcd.drawCircle(cx, cy, 8, 0xFFFF);
    lcd.drawCircle(cx, cy, 3, 0xFFFF);
  }
  center(label, cx, cy + radius + 6, 1, 0xFFFF);
}

void backButton() {
  lcd.fillCircle(28, 28, 20, 0x2C9F);
  lcd.fillTriangle(18, 28, 32, 18, 32, 38, 0xFFFF);
}

bool hitCircle(int x, int y, int cx, int cy, int radius) {
  int dx = x - cx;
  int dy = y - cy;
  return dx * dx + dy * dy <= radius * radius;
}

void drawChrome() {
  lcd.fillRect(0, 168, 320, 72, BG);
  center(uiLine.length() ? uiLine : "Toque para ouvir", 160, 174, 2, GOLD);
  String first = uiScreen.substring(0, 26);
  String second = uiScreen.length() > 26 ? uiScreen.substring(26, 52) : "";
  if (first.length()) center(first, 160, 198, 1, PAPER);
  if (second.length()) center(second, 160, 214, 1, PAPER);
  chromeDirty = false;
}

void lineAt(const String& text, int y, uint8_t size, uint16_t color) {
  center(text, 160, y, size, color);
}

void tile(int x, int y, int w, int h, uint16_t color, const char* label, int kind) {
  lcd.fillRoundRect(x + 2, y + 3, w, h, 16, 0x0000);
  lcd.fillRoundRect(x, y, w, h, 16, color);
  int cx = x + w / 2;
  int cy = y + h / 2 - 8;
  if (kind == 0) lcd.fillCircle(cx, cy, 8, 0xFFFF);
  else if (kind == 1) {
    lcd.fillRect(cx - 9, cy - 7, 18, 14, 0xFFFF);
    lcd.fillRect(cx - 9, cy - 7, 18, 4, color);
  } else if (kind == 2) {
    lcd.fillRect(cx - 2, cy - 9, 4, 18, 0xFFFF);
    lcd.fillRect(cx - 9, cy - 2, 18, 4, 0xFFFF);
  } else if (kind == 3) {
    lcd.fillRect(cx - 8, cy - 8, 7, 7, 0xFFFF);
    lcd.fillRect(cx + 1, cy - 8, 7, 7, 0xFFFF);
    lcd.fillRect(cx - 8, cy + 1, 7, 7, 0xFFFF);
    lcd.fillRect(cx + 1, cy + 1, 7, 7, 0xFFFF);
  } else {
    lcd.drawCircle(cx, cy, 9, 0xFFFF);
    lcd.drawCircle(cx, cy, 4, 0xFFFF);
  }
  center(label, cx, y + h - 18, 1, 0xFFFF);
}

bool hitTile(int px, int py, int x, int y, int w, int h) {
  return px >= x && px < x + w && py >= y && py < y + h;
}

void drawLauncher() {
  lcd.fillScreen(BG);
  center("MySoul", 160, 2, 2, 0xFFFF);
  center(uiIp, 160, 20, 1, MUTED);
  tile(8, 36, 74, 96, 0xFE60, "Orb", 0);
  tile(88, 36, 74, 96, 0xF9A6, "Agenda", 1);
  tile(168, 36, 74, 96, 0x05D6, "Saude", 2);
  tile(248, 36, 74, 96, 0xE71C, "Board", 2);
  tile(8, 140, 74, 96, 0x2C9F, "Firmas", 3);
  tile(88, 140, 74, 96, 0xFE60, "Guia", 4);
  tile(168, 140, 74, 96, 0x07E0, "Texto", 1);
  tile(248, 140, 74, 96, 0x05FF, "Cal", 4);
}

void scoreBubble(int cx, int cy, uint16_t color, const char* label, const char* value) {
  lcd.fillCircle(cx, cy, 30, color);
  lcd.fillCircle(cx - 7, cy - 8, 5, 0xFFFF);
  center(value, cx, cy - 6, 2, 0xFFFF);
  center(label, cx, cy + 38, 1, 0xFFFF);
}

void drawPage() {
  if (page == 0) {
    drawLauncher();
    pageDirty = false;
    return;
  }
  lcd.fillScreen(BG);
  backButton();
  if (page == 4) {
    center("Orb", 160, 8, 2, 0xFFFF);
    orbDirty = false;
    chromeDirty = true;
    drawOrb();
    drawChrome();
  } else if (page == 1) {
    center("Agenda", 180, 10, 2, 0xFFFF);
    lcd.fillCircle(160, 108, 46, 0xF9A6);
    center(pgOcupacao.c_str(), 160, 96, 2, 0xFFFF);
    char agir[28];
    pgAgir.toCharArray(agir, sizeof(agir));
    center(agir, 160, 176, 1, 0xFFFF);
  } else if (page == 5 || page == 6 || page == 7 || page == 8) {
    backButton();
    center("Board", 180, 8, 2, 0xFFFF);
    if (page == 5) {
      tile(16, 44, 140, 88, 0xFE60, "Falar", 0);
      tile(164, 44, 140, 88, 0x2C9F, "Texto", 1);
      tile(16, 142, 140, 88, 0xF9A6, "Opcoes", 3);
      tile(164, 142, 140, 88, 0x05D6, "Guia", 4);
    } else if (page == 6) {
      tile(16, 44, 140, 88, 0x2C9F, "Dia", 1);
      tile(164, 44, 140, 88, 0x05D6, "Saude", 2);
      tile(16, 142, 140, 88, 0xFE60, "Firmas", 3);
      tile(164, 142, 140, 88, 0xF9A6, "Agora", 4);
    } else if (page == 7) {
      String rest = pgReply;
      int y = 34;
      for (int row = 0; row < 3 && rest.length(); row++) {
        String line = rest.substring(0, 24);
        rest = rest.substring(line.length());
        center(line, 160, y, 1, 0xFFFF);
        y += 16;
      }
      char o1[16], o2[16], o3[16];
      pgOpt[0].toCharArray(o1, sizeof(o1));
      pgOpt[1].toCharArray(o2, sizeof(o2));
      pgOpt[2].toCharArray(o3, sizeof(o3));
      tile(16, 88, 90, 132, 0x2C9F, o1, 3);
      tile(116, 88, 90, 132, 0x05D6, o2, 3);
      tile(214, 88, 90, 132, 0xFE60, o3, 3);
    } else {
      center(guideStep == 0 ? "Por onde?" : "Como esta?", 160, 36, 2, 0xFFFF);
      tile(16, 80, 90, 140, 0xF9A6, guideStep == 0 ? "Dia" : "Cheio", 1);
      tile(116, 80, 90, 140, 0x05D6, guideStep == 0 ? "Saude" : "Livre", 2);
      tile(214, 80, 90, 140, 0x2C9F, guideStep == 0 ? "Firma" : "Meio", 3);
    }
  } else if (page == 2) {
    center("Saude", 180, 8, 2, 0xFFFF);
    scoreBubble(80, 78, 0x05D6, "Trabalho", pgTrabalho.c_str());
    scoreBubble(240, 78, 0x05D6, "Cansaco", pgCansaco.c_str());
    scoreBubble(80, 168, 0xFE60, "Estresse", pgEstresse.c_str());
    scoreBubble(240, 168, 0xF9A6, "Esgota", pgEsgota.c_str());
  } else {
    center("Empresas", 190, 8, 2, 0xFFFF);
    const uint16_t colors[5] = {0x2C9F, 0x05D6, 0xFE60, 0xF9A6, 0x05FF};
    const int xs[5] = {58, 160, 262, 110, 214};
    const int ys[5] = {72, 72, 72, 168, 168};
    for (int i = 0; i < 5; i++) {
      scoreBubble(xs[i], ys[i], colors[i], kEmpName[i], pgEmp[i].c_str());
    }
  }
  pageDirty = false;
}

void drawHome() {
  page = 0;
  drawPage();
}

void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!webOn) {
      web.begin();
      MDNS.begin("mysoul");
      udp.begin(8794);
      webOn = true;
      uiIp = WiFi.localIP().toString();
      chromeDirty = true;
      Serial.print("placa http://");
      Serial.print(uiIp);
      Serial.println("/");
    }
    return;
  }
  webOn = false;
  gwOk = false;
  if (millis() - lastWifi < 20000) return;
  lastWifi = millis();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void acceptCalSample(int rawX, int rawY) {
  if (calStep < 4) {
    calX[calStep] = rawX;
    calY[calStep] = rawY;
    beep(1400, 30);
    if (calStep < 3) {
      calStep++;
      drawCalTarget();
      return;
    }
    if (!computeCal()) {
      calStep = 0;
      center("falhou", 160, 120, 2, GOLD);
      delay(700);
      drawCalTarget();
      return;
    }
    calStep = 4;
    drawCalTarget();
    return;
  }
  int screenX, screenY;
  mapRaw(rawX, rawY, screenX, screenY);
  int dx = screenX - kCalX[4];
  int dy = screenY - kCalY[4];
  if (dx * dx + dy * dy > 70 * 70) {
    calStep = 0;
    center("falhou", 160, 120, 2, GOLD);
    delay(700);
    drawCalTarget();
    return;
  }
  saveCal();
  calibrating = false;
  beep(1800, 40);
  drawHome();
}

void handleCalibration(bool pressed, const TS_Point& point) {
  if (waitRelease) {
    if (!pressed) waitRelease = false;
    return;
  }
  if (!pressed) {
    calN = 0;
    return;
  }
  if (calN < 20) {
    calXs[calN] = point.x;
    calYs[calN] = point.y;
    calN++;
  }
  int x = kCalX[calStep];
  int y = kCalY[calStep];
  lcd.drawCircle(x, y, 8 + min(calN, 14), GOLD);
  if (calN < 8) return;
  waitRelease = true;
  acceptCalSample(medianN(calXs, calN), medianN(calYs, calN));
  calN = 0;
}

void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);
  Serial.begin(115200);
  pinMode(Pins::backlight, OUTPUT);
  digitalWrite(Pins::backlight, HIGH);
  pinMode(Pins::speaker, OUTPUT);
  pinMode(Pins::sdCs, OUTPUT);
  digitalWrite(Pins::sdCs, HIGH);
  loadCal();
  calibrating = !haveCal;
  tftSpi.begin(Pins::tftSclk, Pins::tftMiso, Pins::tftMosi, Pins::tftCs);
  touchSpi.begin(Pins::touchSclk, Pins::touchMiso, Pins::touchMosi, Pins::touchCs);
  lcd.begin(27000000);
  lcd.setRotation(1);
  lcd.setTextWrap(false);
  touch.begin(touchSpi);
  touch.setRotation(1);
  touchSpi.begin(Pins::touchSclk, Pins::touchMiso, Pins::touchMosi, Pins::touchCs);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_11dBm);
  WiFi.setHostname("mysoul");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  lastWifi = millis();
  web.onNotFound(proxyPage);
  if (calibrating) drawCalTarget();
  else drawHome();
}

void loop() {
  readSerialPc();
  if (millis() - lastHello > 2000) {
    lastHello = millis();
    Serial.println("MYSOUL?");
  }
  ensureWifi();
  if (webOn) {
    web.handleClient();
    readDiscovery();
  }

  if (calibrating) {
    TS_Point point;
    bool pressed = pressedNow(point);
    handleCalibration(pressed, point);
    delay(12);
    return;
  }

  if (!serialPc && WiFi.status() != WL_CONNECTED) {
    if (uiLine != "Sem rede") {
      uiState = "offline";
      uiLine = "Sem rede";
      chromeDirty = true;
    }
  } else if (!gwOk && !serialPc) {
    if (millis() - lastSeek > 4000) {
      lastSeek = millis();
      seekGateway();
      tryKnownPc();
    }
    if (uiLine != "Sem PC") {
      uiState = "offline";
      uiLine = "Sem PC";
      chromeDirty = true;
    }
  } else if (!serialPc && millis() - lastPoll > 500) {
    lastPoll = millis();
    if (!pollGateway()) {
      pollMiss++;
      if (pollMiss > 4) {
        gwOk = false;
        if (!serialPc && uiLine != "Sem PC") {
          uiLine = "Sem PC";
          uiState = "offline";
          chromeDirty = true;
        }
      }
    } else {
      pollMiss = 0;
      if (millis() - lastPages > 3000) {
        lastPages = millis();
        pollPages();
      }
    }
  }

  TS_Point point;
  bool pressed = pressedNow(point);
  if (pressed && !touchHeld) {
    int xs[12], ys[12], count = 0;
    for (int i = 0; i < 14 && count < 12; i++) {
      TS_Point sample = touch.getPoint();
      if (sample.z <= 0 && !touch.touched()) break;
      int screenX, screenY;
      mapRaw(sample.x, sample.y, screenX, screenY);
      if (i >= 2) {
        xs[count] = screenX;
        ys[count] = screenY;
        count++;
      }
      delay(3);
    }
    if (count >= 4) {
      heldX = medianN(xs, count);
      heldY = medianN(ys, count);
      touchHeld = true;
    }
  } else if (!pressed && touchHeld) {
    touchHeld = false;
    if (page == 0) {
      int next = page;
      if (hitTile(heldX, heldY, 8, 36, 74, 96)) next = 4;
      else if (hitTile(heldX, heldY, 88, 36, 74, 96)) next = 1;
      else if (hitTile(heldX, heldY, 168, 36, 74, 96)) next = 2;
      else if (hitTile(heldX, heldY, 248, 36, 74, 96)) next = 5;
      else if (hitTile(heldX, heldY, 8, 140, 74, 96)) next = 3;
      else if (hitTile(heldX, heldY, 88, 140, 74, 96)) next = 8;
      else if (hitTile(heldX, heldY, 168, 140, 74, 96)) next = 6;
      else if (hitTile(heldX, heldY, 248, 140, 74, 96)) startCal();
      if (next != page) {
        page = next;
        beep(900, 20);
        drawPage();
      }
    } else if (hitCircle(heldX, heldY, 28, 28, 30)) {
      page = (page == 6 || page == 7 || page == 8) ? 5 : 0;
      beep(700, 16);
      drawPage();
    } else if (page == 5) {
      int next = page;
      if (hitTile(heldX, heldY, 16, 44, 140, 88)) {
        page = 4;
        drawPage();
        postTap();
        return;
      }
      else if (hitTile(heldX, heldY, 164, 44, 140, 88)) next = 6;
      else if (hitTile(heldX, heldY, 16, 142, 140, 88)) next = 7;
      else if (hitTile(heldX, heldY, 164, 142, 140, 88)) next = 8;
      if (next != page) {
        page = next;
        beep(900, 20);
        drawPage();
      }
    } else if (page == 6) {
      if (hitTile(heldX, heldY, 16, 44, 140, 88)) sendBoard("Como esta o dia?");
      else if (hitTile(heldX, heldY, 164, 44, 140, 88)) sendBoard("Como esta a saude?");
      else if (hitTile(heldX, heldY, 16, 142, 140, 88)) sendBoard("Notas das empresas");
      else if (hitTile(heldX, heldY, 164, 142, 140, 88)) sendBoard("O que fazer agora?");
    } else if (page == 7) {
      if (hitTile(heldX, heldY, 16, 88, 90, 132)) sendBoard(pgOpt[0].c_str());
      else if (hitTile(heldX, heldY, 116, 88, 90, 132)) sendBoard(pgOpt[1].c_str());
      else if (hitTile(heldX, heldY, 214, 88, 90, 132)) sendBoard(pgOpt[2].c_str());
    } else if (page == 8) {
      int picked = -1;
      if (hitTile(heldX, heldY, 16, 80, 90, 140)) picked = 0;
      else if (hitTile(heldX, heldY, 116, 80, 90, 140)) picked = 1;
      else if (hitTile(heldX, heldY, 214, 80, 90, 140)) picked = 2;
      if (picked >= 0 && guideStep == 0) {
        guidePath = picked;
        guideStep = 1;
        beep(900, 20);
        drawPage();
      } else if (picked >= 0) {
        const char* msg = "Guiado";
        if (guidePath == 0) msg = picked == 0 ? "Dia cheio" : picked == 1 ? "Dia livre" : "Dia no meio";
        else if (guidePath == 1) msg = picked == 0 ? "Saude cansada" : picked == 1 ? "Saude boa" : "Saude no meio";
        else msg = picked == 0 ? "Empresa cheia" : picked == 1 ? "Empresa livre" : "Empresa no meio";
        guideStep = 0;
        sendBoard(msg);
      }
    } else if (page == 4) {
      postTap();
    }
  }

  if (uiState == "listening" && listenSince && millis() - listenSince > 32000) {
    uiState = "thinking";
    uiLine = "Pensando";
    listenSince = 0;
    chromeDirty = true;
    orbDirty = true;
  }

  if (pageDirty && !calibrating) {
    if (page == 4) pageDirty = false;
    else drawPage();
  }

  if (page == 4 && orbDirty) {
    drawOrb();
    orbDirty = false;
  }
  if (page == 4 && chromeDirty) {
    drawChrome();
    backButton();
  }
}
