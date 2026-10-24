// GC9A01 接線測試：純色輪播＋方向標記。腳位為推定的擴展板 8 孔座：
// SCL=21 SDA=47 RES=45 DC=40 CS=41 BLK=42
#define LGFX_USE_V1
#include <LovyanGFX.hpp>

class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_GC9A01 _panel;
  lgfx::Bus_SPI _bus;
  lgfx::Light_PWM _light;
public:
  LGFX() {
    { auto c = _bus.config();
      c.spi_host = SPI2_HOST; c.spi_mode = 0; c.freq_write = 10000000; c.freq_read = 16000000;
      c.pin_sclk = 21; c.pin_mosi = 47; c.pin_miso = -1; c.pin_dc = 40;
      c.dma_channel = SPI_DMA_CH_AUTO;
      _bus.config(c); _panel.setBus(&_bus); }
    { auto c = _panel.config();
      c.pin_cs = 41; c.pin_rst = 45; c.pin_busy = -1;
      c.panel_width = 240; c.panel_height = 240; c.offset_x = 0; c.offset_y = 0;
      c.invert = true; c.rgb_order = false; c.readable = false; c.bus_shared = false;
      _panel.config(c); }
    { auto c = _light.config();
      c.pin_bl = 42; c.invert = false; c.freq = 44100; c.pwm_channel = 7;
      _light.config(c); _panel.setLight(&_light); }
    setPanel(&_panel);
  }
  void setFreq(uint32_t f) { _bus.release(); auto c = _bus.config(); c.freq_write = f; _bus.config(c); _bus.init(); }
};
LGFX tft;

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("[GC9A01] init...");
  bool ok = tft.init();
  Serial.printf("[GC9A01] init=%d w=%d h=%d\n", ok, tft.width(), tft.height());
  tft.setBrightness(200);
}

static void bars(const char* label) {
  tft.fillScreen(TFT_BLACK);
  tft.fillRect(30, 40, 60, 160, TFT_RED);
  tft.fillRect(90, 40, 60, 160, TFT_GREEN);
  tft.fillRect(150, 40, 60, 160, TFT_BLUE);
  tft.setTextColor(TFT_WHITE);
  tft.setTextDatum(middle_center);
  tft.setTextSize(2);
  tft.drawString("R", 60, 25);
  tft.drawString("G", 120, 25);
  tft.drawString("B", 180, 25);
  tft.drawString(label, 120, 215);
}

void loop() {
  // 依序試不同 SPI 速度，畫面下方標速度；最後一張是方向標記
  const struct { uint32_t f; const char* n; } fs[] = {
    {20000000, "20MHz"}, {26666667, "26.7MHz"}, {40000000, "40MHz"}};
  for (auto& k : fs) {
    tft.setFreq(k.f);
    bars(k.n);
    Serial.printf("[GC9A01] RGB bars @%s\n", k.n);
    delay(4000);
  }
  tft.setFreq(10000000);
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_YELLOW);
  tft.setTextDatum(middle_center);
  tft.setTextSize(3);
  tft.fillTriangle(120, 10, 100, 45, 140, 45, TFT_YELLOW);
  tft.drawString("UP", 120, 70);
  tft.drawString("L", 30, 120);
  tft.drawString("R", 210, 120);
  tft.drawCircle(120, 120, 118, TFT_CYAN);
  Serial.println("[GC9A01] ORIENT @10MHz");
  delay(4000);
}
