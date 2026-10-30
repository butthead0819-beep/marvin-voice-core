// face466_bench.ino — 466×466 face_rt 實機 bench 測試程式
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include "../car_puck/face_rt.h"
#include "face_rt466.h"

static const int FRAMES = 120;
static const int W466 = 466;
static const int W240 = 240;

static uint8_t *base466 = nullptr, *out466 = nullptr;
static uint8_t *base240 = nullptr, *out240 = nullptr;
static FaceRtWork* work240 = nullptr;
static FaceRtWork466* work466 = nullptr;
static uint32_t us[FRAMES];

static void* psramAlloc(size_t n) {
  void* p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!p) {
    Serial.println("[Bench466] alloc failed");
    while (true) delay(1000);
  }
  return p;
}

static FacePose poseAt(int f) {
  const float t = f / 24.0f, tau = 6.2831853f;
  FacePose p;
  p.yaw = 1.1f * sinf(tau * t / 5.0f);
  p.pitch = 0.4f * sinf(tau * t / 3.3f);
  p.roll = 0.3f * sinf(tau * t / 4.1f);
  p.browUp = 0.03f * (1 + sinf(tau * t / 2.7f));
  p.err = 0;
  return p;
}

static int cmpU32(const void* a, const void* b) {
  uint32_t x = *(const uint32_t*)a, y = *(const uint32_t*)b;
  return x < y ? -1 : (x > y ? 1 : 0);
}

static void report(const char* name, int W) {
  double sum = 0;
  for (int i = 0; i < FRAMES; i++) sum += us[i];
  qsort(us, FRAMES, sizeof(uint32_t), cmpU32);
  float avg = (float)(sum / FRAMES / 1000.0);
  float p50 = us[FRAMES / 2] / 1000.0f;
  float p95 = us[(FRAMES * 95) / 100] / 1000.0f;
  float mx = us[FRAMES - 1] / 1000.0f;
  float fps_cap = (p95 > 0.0f) ? (1000.0f / p95) : 0.0f;

  Serial.printf("[Bench466] cfg=%-22s W=%d avg=%6.2f p50=%6.2f p95=%6.2f max=%6.2f fps_cap=%5.1f\n",
                name, W, avg, p50, p95, mx, fps_cap);
}

// 測試 240 基準
static void bench240() {
  memcpy(out240, base240, (size_t)W240 * W240 * 3);
  face_rt_init(work240);

  for (int f = 0; f < 30; f++) {
    FacePose p = poseAt(f);
    face_rt_render(p, base240, out240);
  }

  for (int f = 0; f < FRAMES; f++) {
    FacePose p = poseAt(f);
    uint32_t t0 = (uint32_t)esp_timer_get_time();
    face_rt_render(p, base240, out240);
    us[f] = (uint32_t)esp_timer_get_time() - t0;
    delay(1);
  }
  report("face_rt_240_t4_asin", W240);
}

// 測試 466 各種配置
template <int TILE_SZ, int MODE>
static void bench466(const char* name) {
  memcpy(out466, base466, (size_t)W466 * W466 * 3);
  face_rt466_init(work466);

  for (int f = 0; f < 30; f++) {
    FacePose p = poseAt(f);
    face_rt466_render<TILE_SZ, MODE>(work466, p, base466, out466);
  }

  for (int f = 0; f < FRAMES; f++) {
    FacePose p = poseAt(f);
    uint32_t t0 = (uint32_t)esp_timer_get_time();
    face_rt466_render<TILE_SZ, MODE>(work466, p, base466, out466);
    us[f] = (uint32_t)esp_timer_get_time() - t0;
    delay(1);
  }
  report(name, W466);
}

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);
  delay(3000);

  Serial.println();
  Serial.printf("[Bench466] S3 Bring-up: cpu=%uMHz psram=%u bytes\n",
                (unsigned)getCpuFrequencyMhz(), (unsigned)ESP.getPsramSize());

  base240 = (uint8_t*)psramAlloc((size_t)W240 * W240 * 3);
  out240 = (uint8_t*)psramAlloc((size_t)W240 * W240 * 3);
  work240 = (FaceRtWork*)psramAlloc(sizeof(FaceRtWork));

  base466 = (uint8_t*)psramAlloc((size_t)W466 * W466 * 3);
  out466 = (uint8_t*)psramAlloc((size_t)W466 * W466 * 3);
  work466 = (FaceRtWork466*)psramAlloc(sizeof(FaceRtWork466));

  for (size_t i = 0; i < (size_t)W240 * W240 * 3; i++) base240[i] = (uint8_t)(i * 7);
  for (size_t i = 0; i < (size_t)W466 * W466 * 3; i++) base466[i] = (uint8_t)(i * 7);

  // 1. 現行 240 基準
  bench240();

  // 2. 466 8x8 + 逐像素 asin
  bench466<8, 0>("face_rt_466_t8_asin");

  // 3. 466 8x8 + DDA 角度 + 逐像素雙線性權重
  bench466<8, 1>("face_rt_466_t8_dda_bl");

  // 4. 466 8x8 + DDA 角度 + 均值凍結 + 座標 DDA (最速版)
  bench466<8, 2>("face_rt_466_t8_dda_fast");

  Serial.println("[Bench466] all benches finished.");
}

void loop() {
  delay(1000);
}
