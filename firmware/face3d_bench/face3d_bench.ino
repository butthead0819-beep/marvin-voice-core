// face3d_bench.ino — S3 上量「純 render 耗時」：現行 face_rt.h（240，基準）對 face_rt3d.h（3D 內積版，
// Marvin／圓眼示範 × 240／466）。不接螢幕、不開 WiFi，只計 face_rt_render()/face3d_render() 呼叫本身。
//
// FQBN（同 car_puck.ino）：esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,CDCOnBoot=cdc,
//                         USBMode=hwcdc,PartitionScheme=app3M_fat9M_16MB
// 燒錄流程同 car_puck.ino 檔頭（進下載模式、燒完手動按 RESET）。
//
// 怎麼看結果：開序列埠 115200，開機 2 秒後會印 CPU 頻率、PSRAM 大小，接著五組設定各印一行
//   [Bench3D] cfg=<名稱> W=<W> avg=<ms> p50=<ms> p95=<ms> max=<ms> fps_cap=<1000/p95>
// 全部跑完印 [Bench3D] done。fps_cap 用 p95 估「幾乎每格都算得完」的最高幀率。
// 每組：out=base、init、先跑 240 格暖身不記錄，再跑 240 格（=24fps 10 秒的連續動作）逐格計時。
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include "../car_puck/face_rt.h"
#include "../car_puck/face_rt3d.h"

static const int FRAMES = 240;
static uint8_t *base240, *out240, *base466, *out466;
static FaceRtWork* rtWork;
static Face3dWork* work3d;
static uint32_t us[FRAMES];

static void fillBase(uint8_t* base, int W) {   // 耗時跟內容無關，只要不是全 0（同 host 測試的公式）
  for (int y = 0; y < W; y++) {
    for (int x = 0; x < W; x++) {
      size_t i = (size_t)(y * W + x) * 3;
      base[i] = (uint8_t)((x * 7 + y * 3) & 255);
      base[i + 1] = (uint8_t)((x * y) & 255);
      base[i + 2] = (uint8_t)((x + 2 * y) & 255);
    }
  }
}

static void* psramAlloc(size_t n, const char* what) {
  void* p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!p) {
    Serial.printf("[Bench3D] PSRAM alloc failed: %s (%u bytes)\n", what, (unsigned)n);
    while (true) delay(1000);
  }
  return p;
}

// 第 f 格（t=f/24）的連續動作姿勢
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

// ch==nullptr → 跑 face_rt（基準，W 必為 240）；否則跑 face3d_render
static void runCfg(const char* name, const Face3dChar* ch, int W, uint8_t* base, uint8_t* out) {
  memcpy(out, base, (size_t)W * W * 3);
  if (ch) face3d_init(work3d, W); else face_rt_init(rtWork);
  for (int f = 0; f < FRAMES * 2; f++) {
    FacePose p = poseAt(f % FRAMES);
    uint32_t t0 = (uint32_t)esp_timer_get_time();
    if (ch) face3d_render(*ch, p, base, out, work3d); else face_rt_render(p, base, out);
    uint32_t dt = (uint32_t)esp_timer_get_time() - t0;
    if (f >= FRAMES) us[f - FRAMES] = dt;
    delay(1);   // 不計時；讓出 CPU 避免看門狗
  }
  double sum = 0;
  uint32_t mx = 0;
  for (int i = 0; i < FRAMES; i++) { sum += us[i]; if (us[i] > mx) mx = us[i]; }
  qsort(us, FRAMES, sizeof(uint32_t), cmpU32);
  const float p50 = us[FRAMES / 2] / 1000.0f, p95 = us[(FRAMES * 95) / 100] / 1000.0f;
  Serial.printf("[Bench3D] cfg=%s W=%d avg=%.2f p50=%.2f p95=%.2f max=%.2f fps_cap=%.1f\n",
                name, W, (float)(sum / FRAMES / 1000.0), p50, p95, mx / 1000.0f, 1000.0f / p95);
}

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);   // USB 序列埠沒人讀時不要卡住（face_demo 踩過）
  delay(2000);
  Serial.printf("[Bench3D] cpu=%uMHz psram=%u bytes\n", (unsigned)getCpuFrequencyMhz(), (unsigned)ESP.getPsramSize());

  base240 = (uint8_t*)psramAlloc(240 * 240 * 3, "base240");
  out240 = (uint8_t*)psramAlloc(240 * 240 * 3, "out240");
  base466 = (uint8_t*)psramAlloc(466 * 466 * 3, "base466");
  out466 = (uint8_t*)psramAlloc(466 * 466 * 3, "out466");
  rtWork = (FaceRtWork*)psramAlloc(sizeof(FaceRtWork), "FaceRtWork");
  work3d = (Face3dWork*)psramAlloc(sizeof(Face3dWork), "Face3dWork");
  fillBase(base240, 240);
  fillBase(base466, 466);

  runCfg("face_rt", nullptr, 240, base240, out240);
  runCfg("face3d_marvin", &FACE3D_MARVIN, 240, base240, out240);
  runCfg("face3d_round", &FACE3D_ROUND_DEMO, 240, base240, out240);
  runCfg("face3d_marvin", &FACE3D_MARVIN, 466, base466, out466);
  runCfg("face3d_round", &FACE3D_ROUND_DEMO, 466, base466, out466);
  Serial.println("[Bench3D] done");
}

void loop() {
  delay(1000);
}
