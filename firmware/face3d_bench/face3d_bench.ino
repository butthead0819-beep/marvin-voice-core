// face3d_bench.ino — S3 上量「純 render 耗時」：現行 face_rt.h（240，基準）對 face_rt3d.h（3D 內積版，
// Marvin／圓眼示範 × 240／466），再加 face_atlas_rt.h 的查表與混合式（466、Marvin）。
// 不接螢幕、不開 WiFi，只計 face_rt_render()/face3d_render()/face_atlas_render() 呼叫本身。
//
// 編譯前先產生圖集資料 firmware/face3d_bench/atlas_data.h（FACE_ATLAS_A / FACE_ATLAS_B，不進 git），在 repo 根目錄跑：
//   node tools/puck_face/atlas_export.js --W 466 --step 0.1 --mode sdf2 --out /tmp/atlas_a.bin --header /tmp/atlas_a.h --name FACE_ATLAS_A
//   node tools/puck_face/atlas_export.js --W 466 --step 0.2 --mode sdf2 --out /tmp/atlas_b.bin --header /tmp/atlas_b.h --name FACE_ATLAS_B
//   cat /tmp/atlas_a.h /tmp/atlas_b.h > firmware/face3d_bench/atlas_data.h
//
// FQBN（同 car_puck.ino）：esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,CDCOnBoot=cdc,
//                         USBMode=hwcdc,PartitionScheme=app3M_fat9M_16MB
// 燒錄流程同 car_puck.ino 檔頭（進下載模式、燒完手動按 RESET）。
//
// 怎麼看結果：開序列埠 115200，開機 2 秒後會印 CPU 頻率、PSRAM 大小，接著八組設定各印一行
//   [Bench3D] cfg=<名稱> W=<W> avg=<ms> p50=<ms> p95=<ms> max=<ms> fps_cap=<1000/p95>
// 後三組是圖集：atlas_flash（圖集 A 直接讀 flash，純查表）、atlas_psram（圖集 A 先複製到 PSRAM，純查表）、
// hybrid_psram（圖集 B 複製到 PSRAM，混合式 band=2.5；那行多印 exact_px=<每格平均精算像素數>）。
// 全部跑完印 [Bench3D] done。fps_cap 用 p95 估「幾乎每格都算得完」的最高幀率。
// 每組：out=base、init、先跑 240 格暖身不記錄，再跑 240 格（=24fps 10 秒的連續動作）逐格計時。
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include "../car_puck/face_rt.h"
#include "../car_puck/face_rt3d.h"
#include "../car_puck/face_atlas_rt.h"
#include "atlas_data.h"

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

// at!=nullptr → 跑 face_atlas_render（Marvin，band<0 純查表、>=0 混合式，W 必須等於圖集的 W）；
// 否則 ch==nullptr → 跑 face_rt（基準，W 必為 240）；否則跑 face3d_render
static void runCfg(const char* name, const Face3dChar* ch, int W, uint8_t* base, uint8_t* out,
                   const FaceAtlasView* at = nullptr, float band = -1) {
  memcpy(out, base, (size_t)W * W * 3);
  if (ch) face3d_init(work3d, W); else face_rt_init(rtWork);
  uint64_t exactSum = 0;
  for (int f = 0; f < FRAMES * 2; f++) {
    FacePose p = poseAt(f % FRAMES);
    uint32_t exactPx = 0;
    uint32_t t0 = (uint32_t)esp_timer_get_time();
    if (at) face_atlas_render(*ch, *at, p, band, base, out, work3d, &exactPx);
    else if (ch) face3d_render(*ch, p, base, out, work3d);
    else face_rt_render(p, base, out);
    uint32_t dt = (uint32_t)esp_timer_get_time() - t0;
    if (f >= FRAMES) { us[f - FRAMES] = dt; exactSum += exactPx; }
    delay(1);   // 不計時；讓出 CPU 避免看門狗
  }
  double sum = 0;
  uint32_t mx = 0;
  for (int i = 0; i < FRAMES; i++) { sum += us[i]; if (us[i] > mx) mx = us[i]; }
  qsort(us, FRAMES, sizeof(uint32_t), cmpU32);
  const float p50 = us[FRAMES / 2] / 1000.0f, p95 = us[(FRAMES * 95) / 100] / 1000.0f;
  Serial.printf("[Bench3D] cfg=%s W=%d avg=%.2f p50=%.2f p95=%.2f max=%.2f fps_cap=%.1f",
                name, W, (float)(sum / FRAMES / 1000.0), p50, p95, mx / 1000.0f, 1000.0f / p95);
  if (at && band >= 0) Serial.printf(" exact_px=%.1f", (double)exactSum / FRAMES);
  Serial.println();
}

// 解析圖集；失敗印錯並停。toPsram：先把整份圖集 memcpy 到 PSRAM 再 open（flash 版直接用 blob）
static FaceAtlasView openAtlas(const char* what, const uint8_t* blob, uint32_t len, bool toPsram) {
  if (toPsram) {
    uint8_t* copy = (uint8_t*)psramAlloc(len, what);
    memcpy(copy, blob, len);
    blob = copy;
  }
  FaceAtlasView v;
  if (!face_atlas_open(blob, len, &v)) {
    Serial.printf("[Bench3D] face_atlas_open failed: %s (%u bytes)\n", what, (unsigned)len);
    while (true) delay(1000);
  }
  return v;
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

  FaceAtlasView atlasFlash = openAtlas("FACE_ATLAS_A(flash)", FACE_ATLAS_A, FACE_ATLAS_A_LEN, false);
  runCfg("atlas_flash", &FACE3D_MARVIN, 466, base466, out466, &atlasFlash, -1);
  FaceAtlasView atlasPsram = openAtlas("FACE_ATLAS_A(psram)", FACE_ATLAS_A, FACE_ATLAS_A_LEN, true);
  runCfg("atlas_psram", &FACE3D_MARVIN, 466, base466, out466, &atlasPsram, -1);
  FaceAtlasView atlasHybrid = openAtlas("FACE_ATLAS_B(psram)", FACE_ATLAS_B, FACE_ATLAS_B_LEN, true);
  runCfg("hybrid_psram", &FACE3D_MARVIN, 466, base466, out466, &atlasHybrid, 2.5f);
  Serial.println("[Bench3D] done");
}

void loop() {
  delay(1000);
}
