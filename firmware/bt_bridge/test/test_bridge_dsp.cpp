// test_bridge_dsp.cpp — bridge_dsp.h 的 host 端黑箱測試
//
// 編譯執行（帶 UBSan，T9 靠它抓乘法溢位）：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o test_dsp firmware/bt_bridge/test/test_bridge_dsp.cpp && ./test_dsp
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。

#undef NDEBUG
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <algorithm>
#include <vector>
#include "../bridge_dsp.h"

static const double PI_VAL = 3.14159265358979323846;

// 把整段輸入以 chunk 格一塊塊餵進 Resampler48to44，收集所有輸出
static std::vector<StereoFrame> resample_all(const std::vector<StereoFrame>& in, size_t chunk) {
  Resampler48to44 rs;
  std::vector<StereoFrame> out;
  size_t pos = 0;
  while (pos < in.size()) {
    size_t n = std::min(chunk, in.size() - pos);
    std::vector<StereoFrame> tmp(n + 2);  // 44.1k 輸出不會超過 n+1
    size_t m = rs.process(&in[pos], n, tmp.data(), tmp.size());
    out.insert(out.end(), tmp.begin(), tmp.begin() + m);
    pos += n;
  }
  return out;
}

static bool same_frame(StereoFrame a, StereoFrame b) { return a.l == b.l && a.r == b.r; }

// T1 DC：常數輸入經內插後仍是常數（第一個輸出是 lookahead 前的初始零）
static void test_dc() {
  std::vector<StereoFrame> in(48000, StereoFrame{1000, -1000});
  std::vector<StereoFrame> out = resample_all(in, 256);
  assert(out.size() >= 44098 && out.size() <= 44102);
  for (size_t i = 1; i < out.size(); i++) {
    assert(same_frame(out[i], StereoFrame{1000, -1000}));
  }
  printf("T1 DC ok (out=%zu)\n", out.size());
}

// T2 頻率：1kHz 正弦重取樣後，l 聲道 1 秒應有約 2000 次正負號變化
static void test_frequency() {
  std::vector<StereoFrame> in(48000);
  for (size_t i = 0; i < in.size(); i++) {
    double s = 10000.0 * sin(2.0 * PI_VAL * 1000.0 * (double)i / 48000.0);
    in[i] = StereoFrame{(int16_t)lround(s), 0};
  }
  std::vector<StereoFrame> out = resample_all(in, 256);
  int changes = 0, last_sign = 0;
  for (const StereoFrame& f : out) {
    int s = f.l > 0 ? 1 : (f.l < 0 ? -1 : 0);
    if (s == 0) continue;
    if (last_sign != 0 && s != last_sign) changes++;
    last_sign = s;
  }
  assert(abs(changes - 2000) <= 6);
  printf("T2 frequency ok (sign changes=%d)\n", changes);
}

// T3 分塊不變：一次餵與每次 37 格餵，輸出序列必須完全相同
static void test_chunk_invariance() {
  std::vector<StereoFrame> in(4800);
  for (size_t i = 0; i < in.size(); i++) {
    in[i] = StereoFrame{(int16_t)((i * 7919) % 20000 - 10000), (int16_t)((i * 104729) % 30000 - 15000)};
  }
  std::vector<StereoFrame> one = resample_all(in, in.size());
  std::vector<StereoFrame> chunked = resample_all(in, 37);
  assert(one.size() == chunked.size());
  for (size_t i = 0; i < one.size(); i++) {
    assert(same_frame(one[i], chunked[i]));
  }
  printf("T3 chunk invariance ok (out=%zu)\n", one.size());
}

// T4 ring：超量推入回傳 RING_CAP、overruns 正確；pop 為 FIFO（含跨繞回）
static void test_ring() {
  static FrameRing ring;
  std::vector<StereoFrame> data(RING_CAP + 100);
  for (size_t i = 0; i < data.size(); i++) {
    data[i] = StereoFrame{(int16_t)i, (int16_t)(-(int)i)};
  }
  uint32_t pushed = ring.push(data.data(), RING_CAP + 100);
  assert(pushed == RING_CAP);
  assert(ring.overruns == 100);
  assert(ring.fill() == RING_CAP);

  std::vector<StereoFrame> got(RING_CAP);
  uint32_t popped = ring.pop(got.data(), RING_CAP);
  assert(popped == RING_CAP);
  assert(ring.fill() == 0);
  for (uint32_t i = 0; i < RING_CAP; i++) {
    assert(same_frame(got[i], data[i]));
  }

  // 繞回：先推拉一段讓 head/tail 離開 0，再推滿跨過 buffer 尾端
  std::vector<StereoFrame> junk(5000, StereoFrame{0, 0});
  assert(ring.push(junk.data(), 5000) == 5000);
  assert(ring.pop(got.data(), 5000) == 5000);
  assert(ring.push(data.data(), RING_CAP) == RING_CAP);
  assert(ring.pop(got.data(), RING_CAP) == RING_CAP);
  for (uint32_t i = 0; i < RING_CAP; i++) {
    assert(same_frame(got[i], data[i]));
  }
  printf("T4 ring ok\n");
}

// T5 priming：fill 不足時輸出全 0 且不消耗 ring；補到門檻後才輸出 ring 內資料
static void test_priming() {
  static FrameRing ring;
  OutputState st;
  std::vector<StereoFrame> data(PRIME_FRAMES);
  for (size_t i = 0; i < data.size(); i++) {
    data[i] = StereoFrame{(int16_t)(i % 30000), (int16_t)(-(int)(i % 30000))};
  }
  StereoFrame out[512];
  for (auto& f : out) f = StereoFrame{77, 77};

  ring.push(data.data(), 100);
  fill_output(ring, st, out, 512);
  for (auto& f : out) assert(same_frame(f, StereoFrame{0, 0}));
  assert(ring.fill() == 100);
  assert(st.priming);

  ring.push(data.data() + 100, PRIME_FRAMES - 100);  // fill 補到 PRIME
  fill_output(ring, st, out, 512);
  assert(!st.priming);
  for (uint32_t i = 0; i < 512; i++) assert(same_frame(out[i], data[i]));
  assert(ring.fill() == PRIME_FRAMES - 512);
  assert(st.underruns == 0);
  printf("T5 priming ok\n");
}

// T6 underrun：ring 只剩 10 格（< LOW）要 512 格。
// 第 4 步走補格分支：pop(out, 511) 只拿到 10 格 ≠ 511，所以不補格（dups 仍為 0），
// 改由第 5 步判定 underrun：前 10 格是資料、其餘填 0、underruns=1、回到 priming。
static void test_underrun() {
  static FrameRing ring;
  OutputState st;
  st.priming = false;
  std::vector<StereoFrame> data(10);
  for (size_t i = 0; i < data.size(); i++) data[i] = StereoFrame{(int16_t)(1000 + i), (int16_t)(-(int)(1000 + i))};
  ring.push(data.data(), 10);

  StereoFrame out[512];
  fill_output(ring, st, out, 512);
  for (uint32_t i = 0; i < 10; i++) assert(same_frame(out[i], data[i]));
  for (uint32_t i = 10; i < 512; i++) assert(same_frame(out[i], StereoFrame{0, 0}));
  assert(st.underruns == 1);
  assert(st.priming == true);
  assert(st.dups == 0);
  assert(ring.fill() == 0);
  printf("T6 underrun ok\n");
}

// T7 drop：fill = HIGH+1000 → 這次 callback 多丟 1 格
static void test_drop() {
  static FrameRing ring;
  OutputState st;
  st.priming = false;
  uint32_t total = HIGH_FRAMES + 1000;
  std::vector<StereoFrame> data(total);
  for (uint32_t i = 0; i < total; i++) data[i] = StereoFrame{(int16_t)i, (int16_t)i};
  assert(ring.push(data.data(), total) == total);

  StereoFrame out[512];
  fill_output(ring, st, out, 512);
  assert(st.drops == 1);
  assert(ring.fill() == total - 513);
  assert(st.underruns == 0);
  assert(out[0].l == 1);  // 第 0 格被丟掉，輸出從第 1 格開始
  printf("T7 drop ok\n");
}

// T8 dup：fill = LOW-100 → 這次 callback 補 1 格（out[511]=out[510]）
static void test_dup() {
  static FrameRing ring;
  OutputState st;
  st.priming = false;
  uint32_t total = LOW_FRAMES - 100;
  std::vector<StereoFrame> data(total);
  for (uint32_t i = 0; i < total; i++) data[i] = StereoFrame{(int16_t)i, (int16_t)i};
  assert(ring.push(data.data(), total) == total);

  StereoFrame out[512];
  fill_output(ring, st, out, 512);
  assert(st.dups == 1);
  assert(ring.fill() == total - 511);
  assert(out[511].l == out[510].l && out[511].r == out[510].r);
  assert(st.underruns == 0);
  printf("T8 dup ok\n");
}

// T9 滿幅不溢位：相鄰兩格 -32768→32767（差 65535）且內插點落在後半段（frac>0.5）時，
// 中間值必須介於兩者之間、靠近中點；乘法用 32-bit 會溢位繞回成錯的值（爆一聲 click）。
// 第 6 個輸出點位置 = -1 + 6*48000/44100 ≈ 5.53 → 落在 x[5]、x[6] 之間、偏 x[6]。
static void test_full_scale_no_wrap() {
  Resampler48to44 rs;
  StereoFrame in[8];
  for (auto& f : in) f = StereoFrame{0, 0};
  in[5] = StereoFrame{-32768, -32768};
  in[6] = StereoFrame{32767, 32767};
  StereoFrame out[16];
  size_t m = rs.process(in, 8, out, 16);
  assert(m >= 7);
  // 真值 ≈ -32768 + 65535*0.53 ≈ +2000；容許寬鬆範圍，只抓繞回
  assert(out[6].l > 0 && out[6].l < 6000);
  assert(out[6].r > 0 && out[6].r < 6000);
  printf("T9 full scale no wrap ok (out6=%d)\n", out[6].l);
}

int main() {
  test_dc();
  test_frequency();
  test_chunk_invariance();
  test_ring();
  test_priming();
  test_underrun();
  test_drop();
  test_dup();
  test_full_scale_no_wrap();
  printf("ALL PASS\n");
  return 0;
}
