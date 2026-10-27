// test_face_atlas_rt.cpp — face_atlas_rt.h 的 host 端黑箱測試：比對 tools/puck_face/atlas_export.js
// 用同一份圖集跑 face_atlas.js recallHybrid 產生的標準答案（golden）。
//
// 編譯執行（兩組：純查表、混合式 band=2.5）：
//   node tools/puck_face/atlas_export.js --W 466 --step 0.1 --mode sdf2 --out /tmp/atlas_a.bin --golden /tmp/gatl_a.bin
//   node tools/puck_face/atlas_export.js --W 466 --step 0.2 --mode sdf2 --out /tmp/atlas_b.bin --golden /tmp/gatl_b.bin --band 2.5
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o /tmp/test_face_atlas_rt firmware/car_puck/test/test_face_atlas_rt.cpp
//   /tmp/test_face_atlas_rt /tmp/atlas_a.bin /tmp/gatl_a.bin
//   /tmp/test_face_atlas_rt /tmp/atlas_b.bin /tmp/gatl_b.bin
//   （用法：test_face_atlas_rt <圖集.bin> <golden.bin>；band 與 W 從 golden 檔頭讀。角色固定 MARVIN。）
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。門檻不放寬，跑不過就是實作或圖集有問題。

#undef NDEBUG
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <chrono>
#include <vector>

#include "../face_atlas_rt.h"

namespace {

struct GoldenPose {
  FacePose pose;
  FaceRect rect;
  uint32_t exactPx;
  std::vector<uint8_t> frame;  // W*W*3
};

// 輸入資料（不是被測邏輯）：跟 gen_3d_golden.js / atlas_export.js 的 makeBase 同一條公式。
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

std::vector<uint8_t> load_file(const char* path) {
  FILE* f = fopen(path, "rb");
  if (!f) {
    fprintf(stderr, "cannot open %s\n", path);
    exit(1);
  }
  fseek(f, 0, SEEK_END);
  long n = ftell(f);
  fseek(f, 0, SEEK_SET);
  std::vector<uint8_t> v((size_t)n);
  size_t got = n ? fread(v.data(), 1, (size_t)n, f) : 0;
  assert(got == (size_t)n);
  fclose(f);
  return v;
}

std::vector<GoldenPose> load_golden(const char* path, int* W_out, float* band_out) {
  FILE* f = fopen(path, "rb");
  if (!f) {
    fprintf(stderr, "cannot open %s\n", path);
    exit(1);
  }
  uint32_t magic = rd_u32(f);
  assert(magic == 0x4C544147u);
  uint32_t W = rd_u32(f);
  assert(W > 0 && W <= (uint32_t)FACE3D_MAX_W);
  uint32_t count = rd_u32(f);
  *band_out = rd_f32(f);
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
    gp.exactPx = rd_u32(f);
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
    fprintf(stderr, "usage: %s <atlas.bin> <golden.bin>\n", argv[0]);
    return 1;
  }
  const Face3dChar& ch = FACE3D_MARVIN;

  int W = 0;
  float band = -1;
  std::vector<uint8_t> blob = load_file(argv[1]);
  std::vector<GoldenPose> poses = load_golden(argv[2], &W, &band);
  const size_t FRAME_BYTES = (size_t)W * W * 3;
  printf("loaded atlas %zu bytes, %zu poses, W=%d, band=%g (%s)\n", blob.size(), poses.size(), W, band,
         band >= 0 ? "hybrid" : "lookup only");

  FaceAtlasView at;
  bool opened = face_atlas_open(blob.data(), (uint32_t)blob.size(), &at);
  assert(opened);
  assert(at.W == W);

  // open 的拒絕路徑：magic 壞、被截掉一半的檔、太短的檔、空指標都必須回傳 false
  {
    FaceAtlasView tmp;
    std::vector<uint8_t> bad = blob;
    bad[0] ^= 0xFF;
    assert(!face_atlas_open(bad.data(), (uint32_t)bad.size(), &tmp));
    assert(!face_atlas_open(blob.data(), (uint32_t)blob.size() / 2, &tmp));
    assert(!face_atlas_open(blob.data(), 20, &tmp));
    assert(!face_atlas_open(nullptr, 0, &tmp));
  }

  static Face3dWork work;
  std::vector<uint8_t> base = make_base(W);
  std::vector<uint8_t> out(FRAME_BYTES);

  int worst_max_diff = 0;
  double worst_same_ratio = 101.0;
  double worst_exact_rel = 0;
  uint32_t max_exact = 0;
  double sum_exact = 0;

  for (size_t pi = 0; pi < poses.size(); pi++) {
    const GoldenPose& gp = poses[pi];

    // 不變式：呼叫前 out 必須整張等於 base；每個姿勢互相獨立，順便重設 cur/prev 範圍。
    memcpy(out.data(), base.data(), FRAME_BYTES);
    face3d_init(&work, W);
    uint32_t exactPx = 12345;
    FaceRect rect = face_atlas_render(ch, at, gp.pose, band, base.data(), out.data(), &work, &exactPx);

    // 1. rect 一致（容許差 1）
    assert(iabs(rect.x0 - gp.rect.x0) <= 1);
    assert(iabs(rect.y0 - gp.rect.y0) <= 1);
    assert(iabs(rect.x1 - gp.rect.x1) <= 1);
    assert(iabs(rect.y1 - gp.rect.y1) <= 1);

    // 2. rect 內像素：每通道 |diff| <= 3，完全相同的通道比例 >= 99%。
    //    C++ 用 float、JS 用 double，加上查表內插，只在邊緣差少數級。
    int max_diff = 0;
    long same = 0, total = 0;
    for (int y = rect.y0; y <= rect.y1; y++) {
      for (int x = rect.x0; x <= rect.x1; x++) {
        size_t i = (size_t)(y * W + x) * 3;
        for (int c = 0; c < 3; c++) {
          int d = iabs((int)out[i + c] - (int)gp.frame[i + c]);
          assert(d <= 3);
          if (d > max_diff) max_diff = d;
          if (d == 0) same++;
          total++;
        }
      }
    }
    double same_ratio = total ? (100.0 * same / total) : 100.0;
    assert(same_ratio >= 99.0);

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

    // 5. 混合式：精算像素數跟 golden 差 <= 2%；純查表必須是 0
    if (band >= 0) {
      double diff = (double)iabs((int)exactPx - (int)gp.exactPx);
      assert(diff <= 0.02 * gp.exactPx);
      double rel = gp.exactPx ? diff / gp.exactPx : 0;
      if (rel > worst_exact_rel) worst_exact_rel = rel;
    } else {
      assert(exactPx == 0 && gp.exactPx == 0);
    }
    if (exactPx > max_exact) max_exact = exactPx;
    sum_exact += exactPx;
    if (max_diff > worst_max_diff) worst_max_diff = max_diff;
    if (same_ratio < worst_same_ratio) worst_same_ratio = same_ratio;

    printf("pose %3zu: yaw=%+.3f pitch=%+.3f roll=%+.3f browUp=%.3f err=%.2f  "
           "rect=%dx%d@(%d,%d) max_diff=%d same=%.2f%% exactPx=%u (golden %u)\n",
           pi, gp.pose.yaw, gp.pose.pitch, gp.pose.roll, gp.pose.browUp, gp.pose.err,
           rect.x1 - rect.x0 + 1, rect.y1 - rect.y0 + 1, rect.x0, rect.y0, max_diff, same_ratio, exactPx, gp.exactPx);
  }

  // 連續播放：同一塊 out 依序畫全部姿勢、不清畫面。每畫完一格，整張畫面都必須等於該姿勢的
  // golden（容許同樣的 <=3 級）——驗證「下一格開頭把上一格畫過的範圍還原成 base」沒有留殘影。
  {
    memcpy(out.data(), base.data(), FRAME_BYTES);
    face3d_init(&work, W);
    for (size_t pi = 0; pi < poses.size(); pi++) {
      face_atlas_render(ch, at, poses[pi].pose, band, base.data(), out.data(), &work, nullptr);
      for (size_t i = 0; i < FRAME_BYTES; i++) {
        assert(iabs((int)out[i] - (int)poses[pi].frame[i]) <= 3);
      }
    }
    printf("sequential: %zu poses, whole-frame match OK\n", poses.size());
  }

  printf("\nALL OK\n");
  printf("summary: poses=%zu W=%d band=%g worst_max_diff=%d worst_same_ratio=%.3f%% exactPx_max=%u exactPx_avg=%.1f worst_exactPx_diff=%.3f%%\n",
         poses.size(), W, band, worst_max_diff, worst_same_ratio, max_exact, sum_exact / poses.size(), 100.0 * worst_exact_rel);

  // 耗時（參考數字，不 assert）
  const int REPEATS = 5;
  auto t0 = std::chrono::steady_clock::now();
  for (int r = 0; r < REPEATS; r++) {
    for (size_t pi = 0; pi < poses.size(); pi++) {
      face_atlas_render(ch, at, poses[pi].pose, band, base.data(), out.data(), &work, nullptr);
    }
  }
  auto t1 = std::chrono::steady_clock::now();
  double total_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
  double per_frame_ms = total_ms / (REPEATS * poses.size());
  printf("timing: %d reps x %zu poses, %.3f ms/frame avg (host, -O1)\n", REPEATS, poses.size(), per_frame_ms);

  return 0;
}
