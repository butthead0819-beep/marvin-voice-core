// test_face_rt.cpp — face_rt.h 的 host 端黑箱測試：比對 tools/puck_face/face_pixel.js
// 產生的標準答案（gen_pixel_golden.js）。
//
// 編譯執行：
//   node tools/puck_face/gen_pixel_golden.js /tmp/face_golden.bin
//   c++ -std=c++17 -Wall -Wextra -O1 -DFACE_RT_STATS -fsanitize=undefined -fno-sanitize-recover=all -o /tmp/test_face_rt firmware/car_puck/test/test_face_rt.cpp && /tmp/test_face_rt /tmp/face_golden.bin
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

#include "../face_rt.h"

#ifdef FACE_RT_STATS
uint32_t g_face_rt_full_px = 0;
#endif

namespace {

constexpr int W = 240;
constexpr size_t FRAME_BYTES = (size_t)W * W * 3;

struct GoldenPose {
  FacePose pose;
  FaceRect rect;
  std::vector<uint8_t> frame;  // FRAME_BYTES
};

std::vector<uint8_t> make_base() {
  std::vector<uint8_t> base(FRAME_BYTES);
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
  uint8_t b[4];
  size_t n = fread(b, 1, 4, f);
  assert(n == 4);
  uint32_t u = (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
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

std::vector<GoldenPose> load_golden(const char* path) {
  FILE* f = fopen(path, "rb");
  if (!f) {
    fprintf(stderr, "cannot open %s\n", path);
    exit(1);
  }
  uint32_t magic = rd_u32(f);
  assert(magic == 0x46505847u);
  uint32_t count = rd_u32(f);

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
    gp.frame.resize(FRAME_BYTES);
    size_t n = fread(gp.frame.data(), 1, FRAME_BYTES, f);
    assert(n == FRAME_BYTES);
    out.push_back(std::move(gp));
  }
  fclose(f);
  return out;
}

int iabs(int v) { return v < 0 ? -v : v; }

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    fprintf(stderr, "usage: %s <golden file>\n", argv[0]);
    return 1;
  }

  face_rt_init();

  std::vector<uint8_t> base = make_base();
  std::vector<GoldenPose> poses = load_golden(argv[1]);
  printf("loaded %zu poses\n", poses.size());

  std::vector<uint8_t> out(FRAME_BYTES);

  int worst_max_diff = 0;
  double worst_same_ratio = 101.0;
  uint32_t max_full_px = 0;
  double sum_full_px = 0;

  for (size_t pi = 0; pi < poses.size(); pi++) {
    const GoldenPose& gp = poses[pi];

    memset(out.data(), 0xAA, FRAME_BYTES);
#ifdef FACE_RT_STATS
    g_face_rt_full_px = 0;
#endif
    FaceRect rect = face_rt_render(gp.pose, base.data(), out.data());

    // 1. rect 一致（容許差 1）
    assert(iabs(rect.x0 - gp.rect.x0) <= 1);
    assert(iabs(rect.y0 - gp.rect.y0) <= 1);
    assert(iabs(rect.x1 - gp.rect.x1) <= 1);
    assert(iabs(rect.y1 - gp.rect.y1) <= 1);

    // 2. rect 內像素：|diff| <= 3，相同比例 >= 99%
    int max_diff = 0;
    long same = 0, total = 0;
    for (int y = rect.y0; y <= rect.y1; y++) {
      for (int x = rect.x0; x <= rect.x1; x++) {
        size_t i = (size_t)(y * W + x) * 3;
        for (int ch = 0; ch < 3; ch++) {
          int d = iabs((int)out[i + ch] - (int)gp.frame[i + ch]);
          assert(d <= 3);
          if (d > max_diff) max_diff = d;
          if (d == 0) same++;
          total++;
        }
      }
    }
    double same_ratio = total ? (100.0 * same / total) : 100.0;
    assert(same_ratio >= 99.0);

    // 3. rect 外：golden 必須等於 base
    for (int y = 0; y < W; y++) {
      for (int x = 0; x < W; x++) {
        if (x >= rect.x0 && x <= rect.x1 && y >= rect.y0 && y <= rect.y1) continue;
        size_t i = (size_t)(y * W + x) * 3;
        assert(gp.frame[i] == base[i]);
        assert(gp.frame[i + 1] == base[i + 1]);
        assert(gp.frame[i + 2] == base[i + 2]);
      }
    }

    int rw = rect.x1 - rect.x0 + 1, rh = rect.y1 - rect.y0 + 1;
#ifdef FACE_RT_STATS
    uint32_t full_px = g_face_rt_full_px;
#else
    uint32_t full_px = 0;
#endif
    if (full_px > max_full_px) max_full_px = full_px;
    sum_full_px += full_px;
    if (max_diff > worst_max_diff) worst_max_diff = max_diff;
    if (same_ratio < worst_same_ratio) worst_same_ratio = same_ratio;

    printf("pose %3zu: yaw=%+.3f pitch=%+.3f roll=%+.3f browUp=%.3f err=%.2f  "
           "rect=%dx%d@(%d,%d) max_diff=%d same=%.2f%% full_px=%u\n",
           pi, gp.pose.yaw, gp.pose.pitch, gp.pose.roll, gp.pose.browUp, gp.pose.err,
           rw, rh, rect.x0, rect.y0, max_diff, same_ratio, full_px);
  }

  printf("\nALL OK\n");
  printf("summary: poses=%zu worst_max_diff=%d worst_same_ratio=%.3f%% full_px_max=%u full_px_avg=%.1f\n",
         poses.size(), worst_max_diff, worst_same_ratio, max_full_px, sum_full_px / poses.size());

  // 耗時（參考數字，不 assert）
  const int REPEATS = 20;
  auto t0 = std::chrono::steady_clock::now();
  for (int r = 0; r < REPEATS; r++) {
    for (size_t pi = 0; pi < poses.size(); pi++) {
      face_rt_render(poses[pi].pose, base.data(), out.data());
    }
  }
  auto t1 = std::chrono::steady_clock::now();
  double total_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
  double per_frame_ms = total_ms / (REPEATS * poses.size());
  printf("timing: %d reps x %zu poses, %.3f ms/frame avg (host, -O1)\n", REPEATS, poses.size(), per_frame_ms);

  return 0;
}
