// verify_face466_quality.cpp — 嚴格比對 466 DDA-Fast 與 3D 精算的畫質差異
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <math.h>
#include <vector>
#include <string>

#include "firmware/face466_bench/face_rt466.h"
#include "/private/tmp/claude-501/-Users-jackhuang-Code-Discord-voice-bot/dc391f83-1741-433b-8416-fbab5109532b/scratchpad/face_prof/face_rt3d.h"

static const int W = 466;
static const int FRAMES = 120;

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

static void savePPM(const char* filename, const uint8_t* rgb, int w, int h) {
  FILE* fp = fopen(filename, "wb");
  if (!fp) return;
  fprintf(fp, "P6\n%d %d\n255\n", w, h);
  fwrite(rgb, 1, (size_t)w * h * 3, fp);
  fclose(fp);
}

static void saveDiffPPM(const char* filename, const uint8_t* a, const uint8_t* b, int w, int h, int scale = 10) {
  std::vector<uint8_t> diff((size_t)w * h * 3);
  for (size_t i = 0; i < (size_t)w * h * 3; i++) {
    int d = abs((int)a[i] - (int)b[i]) * scale;
    diff[i] = (uint8_t)(d > 255 ? 255 : d);
  }
  savePPM(filename, diff.data(), w, h);
}

int main() {
  printf("=== 466 Face Quality Verification ===\n");

  std::vector<uint8_t> base(W * W * 3);
  for (size_t i = 0; i < (size_t)W * W * 3; i++) base[i] = (uint8_t)(i * 7);

  std::vector<uint8_t> out_ref = base;
  std::vector<uint8_t> out_fast = base;

  Face3dWork work3d;
  FaceRtWork466 workRt;

  face3d_init(&work3d, W);
  face_rt466_init(&workRt);

  double total_mae = 0;
  int global_max_diff = 0;
  long total_diff_gt8 = 0;
  long total_diff_gt16 = 0;

  for (int f = 0; f < FRAMES; f++) {
    FacePose p = poseAt(f);
    face3d_render(FACE3D_MARVIN, p, base.data(), out_ref.data(), &work3d);
    face_rt466_render<8, 2>(&workRt, p, base.data(), out_fast.data());

    // 比對整張
    int frame_max_diff = 0;
    long frame_diff_sum = 0;
    int frame_gt8 = 0, frame_gt16 = 0;

    for (size_t i = 0; i < (size_t)W * W * 3; i += 3) {
      int dr = abs((int)out_ref[i] - (int)out_fast[i]);
      int dg = abs((int)out_ref[i + 1] - (int)out_fast[i + 1]);
      int db = abs((int)out_ref[i + 2] - (int)out_fast[i + 2]);
      int m = dr > dg ? dr : dg;
      if (db > m) m = db;

      if (m > frame_max_diff) frame_max_diff = m;
      frame_diff_sum += (dr + dg + db);
      if (m > 8) frame_gt8++;
      if (m > 16) frame_gt16++;
    }

    if (frame_max_diff > global_max_diff) global_max_diff = frame_max_diff;
    double frame_mae = (double)frame_diff_sum / (W * W * 3);
    total_mae += frame_mae;
    total_diff_gt8 += frame_gt8;
    total_diff_gt16 += frame_gt16;

    if (f == 0 || f == 30 || f == 60) {
      char fn_ref[64], fn_fast[64], fn_diff[64];
      snprintf(fn_ref, sizeof(fn_ref), "/tmp/face_f%d_ref.ppm", f);
      snprintf(fn_fast, sizeof(fn_fast), "/tmp/face_f%d_fast.ppm", f);
      snprintf(fn_diff, sizeof(fn_diff), "/tmp/face_f%d_diff.ppm", f);
      savePPM(fn_ref, out_ref.data(), W, W);
      savePPM(fn_fast, out_fast.data(), W, W);
      saveDiffPPM(fn_diff, out_ref.data(), out_fast.data(), W, W, 10);
      printf("Frame %2d: max_diff=%3d, MAE=%.4f, px(diff>8)=%d, px(diff>16)=%d (Saved PPM)\n",
             f, frame_max_diff, frame_mae, frame_gt8, frame_gt16);
    }
  }

  printf("\n--- 120 Frames Summary ---\n");
  printf("Average MAE per channel: %.4f (out of 255)\n", total_mae / FRAMES);
  printf("Global Max Diff: %d / 255\n", global_max_diff);
  printf("Avg px per frame with diff > 8:  %.1f (佔整張 466^2 的 %.3f%%)\n",
         (double)total_diff_gt8 / FRAMES, ((double)total_diff_gt8 / FRAMES / (W * W)) * 100.0);
  printf("Avg px per frame with diff > 16: %.1f (佔整張 466^2 的 %.3f%%)\n",
         (double)total_diff_gt16 / FRAMES, ((double)total_diff_gt16 / FRAMES / (W * W)) * 100.0);

  return 0;
}
