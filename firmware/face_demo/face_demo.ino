// Marvin 頭像實機示範：face_rt.h 即時渲染 + GC9A01。
// 姿勢＝搖擺律動 + 每 4 秒輪流一個動作標籤（律動保留 30%）→ 臨界阻尼彈簧（0.25s），同銜接樣機定案。
// 螢幕：10MHz 初始化、40MHz 畫圖（40MHz init 會讓顏色錯位）。
#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include <esp_heap_caps.h>
#include "../car_puck/face_rt.h"
#include "base_sphere.h"

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
      c.panel_width = 240; c.panel_height = 240;
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

static uint8_t* g_out = nullptr;
static FaceRtWork* g_work = nullptr;
static uint16_t g_row[240];
static const uint8_t BAYER[16] = {0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5};

static inline uint16_t to565(const uint8_t* p, int x, int y) {
  int k = BAYER[(y & 3) * 4 + (x & 3)];
  int R5 = (p[0] * 496 + k * 255) / 4080, G6 = (p[1] * 1008 + k * 255) / 4080, B5 = (p[2] * 496 + k * 255) / 4080;
  return (uint16_t)((R5 << 11) | (G6 << 5) | B5);
}

static void pushRows(int y0, int y1, int x0, int x1) {
  int w = x1 - x0 + 1;
  tft.setAddrWindow(x0, y0, w, y1 - y0 + 1);
  for (int y = y0; y <= y1; y++) {
    const uint8_t* src = g_out + (size_t)(y * 240 + x0) * 3;
    for (int i = 0; i < w; i++) g_row[i] = to565(src + i * 3, x0 + i, y);
    tft.writePixels(g_row, w, true);
  }
}

// 只推 prev∪cur 每 4 列一帶（同 bench）
static void pushDirty() {
  tft.startWrite();
  for (int by = 0; by < 240; by += 4) {
    int bx0 = 240, bx1 = -1;
    for (int y = by; y < by + 4; y++) {
      if (g_work->prevX1[y] >= g_work->prevX0[y]) { if (g_work->prevX0[y] < bx0) bx0 = g_work->prevX0[y]; if (g_work->prevX1[y] > bx1) bx1 = g_work->prevX1[y]; }
      if (g_work->curX1[y] >= g_work->curX0[y]) { if (g_work->curX0[y] < bx0) bx0 = g_work->curX0[y]; if (g_work->curX1[y] > bx1) bx1 = g_work->curX1[y]; }
    }
    if (bx1 >= bx0) pushRows(by, by + 3, bx0, bx1);
  }
  tft.endWrite();
}

// ---------- 姿勢（移植自銜接樣機） ----------
static inline float clamp01(float x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }
static inline float ss(float x) { x = clamp01(x); return x * x * (3 - 2 * x); }
static inline float tripE(float u, float a) { return u < a ? ss(u / a) : (u > 1 - a ? ss((1 - u) / a) : 1); }
static void sway(float T, float* p) {   // p = yaw, pitch, roll, browUp
  float s = sinf(2 * PI * T / (8 * 60.0f / 71));
  p[0] = .45f * s; p[1] = .06f * s * s; p[2] = .3f * s; p[3] = 0;
}
static void gesture(int id, float u, float* g) {
  g[0] = g[1] = g[2] = g[3] = 0;
  switch (id) {
    case 0: { float e = tripE(u, .3f); g[2] = .32f * e; g[0] = .35f * e; g[1] = -.06f * e; g[3] = .05f * e; break; }  // 歪頭
    case 1: { float s = sinf(2 * PI * u); g[1] = .3f * s * s; break; }                                              // 點頭
    case 2: { g[0] = .5f * sinf(2 * PI * 2 * u) * tripE(u, .2f); break; }                                          // 搖頭
    case 3: { float e = tripE(u, .3f); g[0] = -1.1f * e; g[2] = -.1f * e; break; }                                 // 看旁邊
    case 4: { float e = tripE(u, .35f); g[1] = -.45f * e; g[0] = .4f * e; g[2] = -.16f * e; g[3] = .04f * e; break; } // 抬頭想
  }
}
static const float GDUR[5] = {1.8f, 1.3f, 1.5f, 2.4f, 2.2f};
static const char* GNAME[5] = {"tilt", "yes", "no", "glance", "ponder"};
static float sx[4] = {0}, sv[4] = {0};

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);   // USB 序列埠沒人讀時不要卡住（會造成每幾秒頓一下）
  delay(800);
  bool ok = tft.init();
  tft.setBrightness(200);
  tft.setFreq(40000000);
  g_out = (uint8_t*)heap_caps_malloc(240 * 240 * 3, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  g_work = (FaceRtWork*)heap_caps_malloc(sizeof(FaceRtWork), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  Serial.printf("[Face] init=%d out=%p work=%p\n", ok, g_out, g_work);
  if (!g_out || !g_work) { for (;;) delay(1000); }
  memcpy(g_out, BASE_SPHERE, sizeof(BASE_SPHERE));
  face_rt_init(g_work);
  tft.startWrite(); pushRows(0, 239, 0, 239); tft.endWrite();   // 底圖整張推一次
}

void loop() {
  static const int64_t PERIOD = 41667;
  static int64_t next = esp_timer_get_time(), statT0 = next;
  static uint32_t frames = 0, late = 0, rUs = 0, pUs = 0, gapMax = 0, gap50 = 0;
  static int64_t lastShow = 0;
  static int gid = -1; static float gStart = 0;

  float T = esp_timer_get_time() / 1e6f;
  // 每 4 秒觸發下一個動作
  int slot = (int)(T / 4.0f);
  if (slot % 1 == 0 && (gid < 0 || T - gStart > 4.0f)) { gid = (gid + 1) % 5; gStart = T; Serial.printf("[Face] gesture %s\n", GNAME[gid]); }

  float base[4], g[4] = {0};
  sway(T, base);
  float u = (T - gStart) / GDUR[gid];
  float duck = 0;
  if (u < 1) { gesture(gid, u, g); duck = tripE(u, .25f); }
  float k = 1 - duck * (1 - 0.3f);
  float tgt[4];
  for (int i = 0; i < 4; i++) tgt[i] = base[i] * k + g[i];
  // 臨界阻尼彈簧：約 0.25s 追到 95%
  const float om = 4.7f / 0.25f, h = (PERIOD / 1e6f) / 4;
  for (int s = 0; s < 4; s++) for (int i = 0; i < 4; i++) { float a = om * om * (tgt[i] - sx[i]) - 2 * om * sv[i]; sv[i] += a * h; sx[i] += sv[i] * h; }

  FacePose p = {sx[0], sx[1], sx[2], sx[3], 0.0f};
  int64_t t0 = esp_timer_get_time();
  face_rt_render(p, BASE_SPHERE, g_out);
  int64_t t1 = esp_timer_get_time();
  pushDirty();
  int64_t t2 = esp_timer_get_time();
  if (lastShow) { uint32_t gap = (uint32_t)(t2 - lastShow); if (gap > gapMax) gapMax = gap; if (gap > 50000) gap50++; }
  lastShow = t2;
  rUs += (uint32_t)(t1 - t0); pUs += (uint32_t)(t2 - t1); frames++;

  if (t2 - statT0 > 5000000) {
    Serial.printf("[Face] fps=%.1f render=%.1fms push=%.1fms late=%u gap_max=%.1fms gap>50ms=%u\n", frames / ((t2 - statT0) / 1e6), rUs / 1000.0 / frames, pUs / 1000.0 / frames, late, gapMax / 1000.0, gap50);
    frames = late = rUs = pUs = gapMax = gap50 = 0; statT0 = t2;
  }
  next += PERIOD;
  int64_t now = esp_timer_get_time();
  if (now >= next) { late++; next = now; delay(1); }
  else delay((uint32_t)((next - now + 999) / 1000));
}
