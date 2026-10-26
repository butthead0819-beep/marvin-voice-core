// test_face_rt3d.cpp — face_rt3d.h 的 host 端黑箱測試：比對 tools/puck_face/face_3d.js
// 產生的標準答案（gen_3d_golden.js）。
//
// 編譯執行（四組：240 marvin、466 marvin、240 round、466 round）：
//   node tools/puck_face/gen_3d_golden.js /tmp/g3d_240_marvin.bin 240 marvin   （另外三組：466 marvin、240 round、466 round）
//   c++ -std=c++17 -Wall -Wextra -O1 -DFACE_RT3D_STATS -fsanitize=undefined -fno-sanitize-recover=all -o /tmp/test_face_rt3d firmware/car_puck/test/test_face_rt3d.cpp
//   /tmp/test_face_rt3d /tmp/g3d_240_marvin.bin marvin   （四組各跑一次，W 從 golden 檔頭讀）
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。

#undef NDEBUG
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <chrono>
#include <vector>

#include "../face_rt3d.h"

#ifdef FACE_RT3D_STATS
uint32_t g_face_rt3d_px = 0;
#endif

namespace {

struct GoldenPose {
  FacePose pose;
  FaceRect rect;
  std::vector<uint8_t> frame;  // W*W*3
};

// 輸入資料（不是被測邏輯）：跟 gen_3d_golden.js 的 makeBase 同一條公式。
std::vector<uint8_t> make_base(int W) {
  std::vector<uint8_t> base((size_t)W * W * 3);
  for (int y = 0; y < W; y++) {
    for (int x = 0; x < W; x++) {
      size_t i = (size_t)(y * W + x) * 3;
      base[i] = (uint8_t)((x * 7 + y * 3) & 255);
      base[i + 1] = (uint8_t)((x * y) & 255);
      base[i + 2] = (uint8_t)((x + 2 * y) & 255);
    }
  }
  return base;
}

uint32_t rd_u32(FILE* f) {
  uint8_t b[4];
  size_t n = fread(b, 1, 4, f);
  assert(n == 4);
  return (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

float rd_f32(FILE* f) {
  uint32_t u = rd_u32(f);
  float v;
  memcpy(&v, &u, 4);
  return v;
}

int16_t rd_i16(FILE* f) {
  uint8_t b[2];
  size_t n = fread(b, 1, 2, f);
  assert(n == 2);
  return (int16_t)((uint16_t)b[0] | ((uint16_t)b[1] << 8));
}

std::vector<GoldenPose> load_golden(const char* path, int* W_out) {
  FILE* f = fopen(path, "rb");
  if (!f) {
    fprintf(stderr, "cannot open %s\n", path);
    exit(1);
  }
  uint32_t magic = rd_u32(f);
  assert(magic == 0x46443347u);
  uint32_t W = rd_u32(f);
  assert(W > 0 && W <= (uint32_t)FACE3D_MAX_W);
  uint32_t count = rd_u32(f);
  *W_out = (int)W;
  const size_t frame_bytes = (size_t)W * W * 3;

  std::vector<GoldenPose> out;
  out.reserve(count);
  for (uint32_t i = 0; i < count; i++) {
    GoldenPose gp;
    gp.pose.yaw = rd_f32(f);
    gp.pose.pitch = rd_f32(f);
    gp.pose.roll = rd_f32(f);
    gp.pose.browUp = rd_f32(f);
    gp.pose.err = rd_f32(f);
    gp.rect.x0 = rd_i16(f);
    gp.rect.y0 = rd_i16(f);
    gp.rect.x1 = rd_i16(f);
    gp.rect.y1 = rd_i16(f);
    gp.frame.resize(frame_bytes);
    size_t n = fread(gp.frame.data(), 1, frame_bytes, f);
    assert(n == frame_bytes);
    out.push_back(std::move(gp));
  }
  fclose(f);
  return out;
}

int iabs(int v) { return v < 0 ? -v : v; }

}  // namespace

int main(int argc, char** argv) {
  if (argc < 3) {
    fprintf(stderr, "usage: %s <golden file> <marvin|round>\n", argv[0]);
    return 1;
  }
  const Face3dChar* ch = nullptr;
  if (strcmp(argv[2], "marvin") == 0) ch = &FACE3D_MARVIN;
  else if (strcmp(argv[2], "round") == 0) ch = &FACE3D_ROUND_DEMO;
  else {
    fprintf(stderr, "unknown character %s\n", argv[2]);
    return 1;
  }

  int W = 0;
  std::vector<GoldenPose> poses = load_golden(argv[1], &W);
  const size_t FRAME_BYTES = (size_t)W * W * 3;
  printf("loaded %zu poses, W=%d, char=%s\n", poses.size(), W, argv[2]);

  static Face3dWork work;
  std::vector<uint8_t> base = make_base(W);
  std::vector<uint8_t> out(FRAME_BYTES);

  int worst_max_diff = 0;
  double worst_same_ratio = 101.0;
  uint32_t max_px = 0;
  double sum_px = 0;

  for (size_t pi = 0; pi < poses.size(); pi++) {
    const GoldenPose& gp = poses[pi];

    // 不變式：呼叫前 out 必須整張等於 base；每個姿勢互相獨立，順便重設 cur/prev 範圍。
    memcpy(out.data(), base.data(), FRAME_BYTES);
    face3d_init(&work, W);
#ifdef FACE_RT3D_STATS
    g_face_rt3d_px = 0;
#endif
    FaceRect rect = face3d_render(*ch, gp.pose, base.data(), out.data(), &work);

    // 1. rect 一致（容許差 1）
    assert(iabs(rect.x0 - gp.rect.x0) <= 1);
    assert(iabs(rect.y0 - gp.rect.y0) <= 1);
    assert(iabs(rect.x1 - gp.rect.x1) <= 1);
    assert(iabs(rect.y1 - gp.rect.y1) <= 1);

    // 2. rect 內像素：每通道 |diff| <= 2，完全相同的通道比例 >= 99.5%。實測最壞差 1 級、相同 99.94%；
    //    C++ 用 float、JS 用 double，只在描邊/漸層邊緣差 1 級；門檻收緊才抓得到係數被改小這類突變。
    int max_diff = 0;
    long same = 0, total = 0;
    for (int y = rect.y0; y <= rect.y1; y++) {
      for (int x = rect.x0; x <= rect.x1; x++) {
        size_t i = (size_t)(y * W + x) * 3;
        for (int c = 0; c < 3; c++) {
          int d = iabs((int)out[i + c] - (int)gp.frame[i + c]);
          assert(d <= 2);
          if (d > max_diff) max_diff = d;
          if (d == 0) same++;
          total++;
        }
      }
    }
    double same_ratio = total ? (100.0 * same / total) : 100.0;
    assert(same_ratio >= 99.5);

    // 3. rect 外：golden 必須等於 base（驗 golden 本身），C++ out 也必須等於 base
    for (int y = 0; y < W; y++) {
      for (int x = 0; x < W; x++) {
        if (x >= rect.x0 && x <= rect.x1 && y >= rect.y0 && y <= rect.y1) continue;
        size_t i = (size_t)(y * W + x) * 3;
        assert(gp.frame[i] == base[i]);
        assert(gp.frame[i + 1] == base[i + 1]);
        assert(gp.frame[i + 2] == base[i + 2]);
        assert(out[i] == base[i]);
        assert(out[i + 1] == base[i + 1]);
        assert(out[i + 2] == base[i + 2]);
      }
    }

    // 4. 涵蓋：C++ out 跟 base 不同的每個像素，x 必須落在該列的 curX0..curX1 內
    //    （否則呼叫端只推 dirty span 會漏畫到這個像素）。
    for (int y = 0; y < W; y++) {
      for (int x = 0; x < W; x++) {
        size_t i = (size_t)(y * W + x) * 3;
        bool differs = out[i] != base[i] || out[i + 1] != base[i + 1] || out[i + 2] != base[i + 2];
        if (!differs) continue;
        assert(x >= work.curX0[y] && x <= work.curX1[y]);
      }
    }

#ifdef FACE_RT3D_STATS
    uint32_t px = g_face_rt3d_px;
#else
    uint32_t px = 0;
#endif
    if (px > max_px) max_px = px;
    sum_px += px;
    if (max_diff > worst_max_diff) worst_max_diff = max_diff;
    if (same_ratio < worst_same_ratio) worst_same_ratio = same_ratio;

    printf("pose %3zu: yaw=%+.3f pitch=%+.3f roll=%+.3f browUp=%.3f err=%.2f  "
           "rect=%dx%d@(%d,%d) max_diff=%d same=%.2f%% px=%u\n",
           pi, gp.pose.yaw, gp.pose.pitch, gp.pose.roll, gp.pose.browUp, gp.pose.err,
           rect.x1 - rect.x0 + 1, rect.y1 - rect.y0 + 1, rect.x0, rect.y0, max_diff, same_ratio, px);
  }

  // 連續播放：同一塊 out 依序畫全部姿勢、不清畫面。每畫完一格，整張畫面都必須等於該姿勢的
  // golden（容許同樣的 ≤2 級）——驗證「下一格開頭把上一格畫過的範圍還原成 base」沒有留殘影。
  {
    memcpy(out.data(), base.data(), FRAME_BYTES);
    face3d_init(&work, W);
    for (size_t pi = 0; pi < poses.size(); pi++) {
      face3d_render(*ch, poses[pi].pose, base.data(), out.data(), &work);
      for (size_t i = 0; i < FRAME_BYTES; i++) {
        assert(iabs((int)out[i] - (int)poses[pi].frame[i]) <= 2);
      }
    }
    printf("sequential: %zu poses, whole-frame match OK\n", poses.size());
  }

  printf("\nALL OK\n");
  printf("summary: poses=%zu W=%d worst_max_diff=%d worst_same_ratio=%.3f%% px_max=%u px_avg=%.1f\n",
         poses.size(), W, worst_max_diff, worst_same_ratio, max_px, sum_px / poses.size());

  // 耗時（參考數字，不 assert）
  const int REPEATS = 5;
  auto t0 = std::chrono::steady_clock::now();
  for (int r = 0; r < REPEATS; r++) {
    for (size_t pi = 0; pi < poses.size(); pi++) {
      face3d_render(*ch, poses[pi].pose, base.data(), out.data(), &work);
    }
  }
  auto t1 = std::chrono::steady_clock::now();
  double total_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
  double per_frame_ms = total_ms / (REPEATS * poses.size());
  printf("timing: %d reps x %zu poses, %.3f ms/frame avg (host, -O1)\n", REPEATS, poses.size(), per_frame_ms);

  return 0;
}
