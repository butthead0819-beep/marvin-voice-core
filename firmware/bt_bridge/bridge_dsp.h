// bridge_dsp.h — bt_bridge 的 DSP 核心（純 C++，不依賴 Arduino / ESP-IDF，可在 host 端測試）
//
// 資料流：I2S slave RX (48k stereo) → Resampler48to44 → FrameRing → fill_output → A2DP callback
#ifndef BRIDGE_DSP_H
#define BRIDGE_DSP_H

#include <stdint.h>
#include <string.h>
#include <atomic>

static const uint32_t RING_CAP = 8192;      // frames，必須 2 的次方
static const uint32_t PRIME_FRAMES = 4096;  // 起播/斷流後重新起播的門檻 (~93ms)
static const uint32_t HIGH_FRAMES = 6144;   // 超過→每次 callback 多丟 1 格
static const uint32_t LOW_FRAMES  = 2048;   // 低於→每次 callback 補 1 格
static const int32_t  STEP_Q16 = 71332;     // round(48000/44100 * 65536)

struct StereoFrame { int16_t l; int16_t r; };

// 48000 → 44100 線性內插重取樣（串流式，跨塊狀態保留）
class Resampler48to44 {
 public:
  // 輸入 n 格，輸出最多 out_cap 格，回傳寫出數。
  // 最後一格輸入會留到下一塊當 lookahead，所以每次輸出都是完整內插值。
  size_t process(const StereoFrame* in, size_t n, StereoFrame* out, size_t out_cap) {
    if (n == 0) return 0;
    size_t written = 0;
    while (true) {
      int32_t i = pos_q16 >> 16;          // 可為 -1（上一塊最後一格）
      int32_t frac = pos_q16 & 0xFFFF;
      if (i + 1 > (int32_t)n - 1 || written >= out_cap) break;
      StereoFrame a = (i < 0) ? last : in[i];
      StereoFrame b = in[i + 1];
      out[written].l = (int16_t)(a.l + (int32_t)(((int64_t)(b.l - a.l) * frac) >> 16));
      out[written].r = (int16_t)(a.r + (int32_t)(((int64_t)(b.r - a.r) * frac) >> 16));
      written++;
      pos_q16 += STEP_Q16;
    }
    pos_q16 -= (int32_t)n << 16;
    last = in[n - 1];
    return written;
  }

 private:
  int32_t pos_q16 = -65536;  // 下一個輸出點的位置（Q16）；-1 代表上一塊的最後一格
  StereoFrame last = {0, 0};
};

// 單一 writer task、單一 reader callback 的 SPSC lock-free ring
class FrameRing {
 public:
  StereoFrame buf[RING_CAP];
  std::atomic<uint32_t> head{0}, tail{0};  // head=寫入累計數、tail=讀出累計數
  uint32_t overruns = 0;                   // 只有 writer 寫

  uint32_t fill() const {
    return head.load(std::memory_order_acquire) - tail.load(std::memory_order_acquire);
  }

  // 最多推 RING_CAP - fill() 格，超出的丟掉新進的
  uint32_t push(const StereoFrame* f, uint32_t n) {
    uint32_t h = head.load(std::memory_order_relaxed);
    uint32_t t = tail.load(std::memory_order_acquire);
    uint32_t space = RING_CAP - (h - t);
    uint32_t k = n < space ? n : space;
    for (uint32_t j = 0; j < k; j++) {
      buf[(h + j) & (RING_CAP - 1)] = f[j];
    }
    head.store(h + k, std::memory_order_release);
    overruns += n - k;
    return k;
  }

  uint32_t pop(StereoFrame* out, uint32_t n) {
    uint32_t t = tail.load(std::memory_order_relaxed);
    uint32_t h = head.load(std::memory_order_acquire);
    uint32_t avail = h - t;
    uint32_t k = n < avail ? n : avail;
    for (uint32_t j = 0; j < k; j++) {
      out[j] = buf[(t + j) & (RING_CAP - 1)];
    }
    tail.store(t + k, std::memory_order_release);
    return k;
  }
};

struct OutputState { bool priming = true; uint32_t underruns = 0, drops = 0, dups = 0; };

// A2DP callback 呼叫：從 ring 取 n 格到 out，處理起播、漂移補/丟、欠載
inline void fill_output(FrameRing& ring, OutputState& st, StereoFrame* out, uint32_t n) {
  if (st.priming) {
    if (ring.fill() >= PRIME_FRAMES) {
      st.priming = false;
    } else {
      memset(out, 0, n * sizeof(StereoFrame));
      return;
    }
  }

  uint32_t f = ring.fill();
  if (f > HIGH_FRAMES) {
    StereoFrame dropped;
    ring.pop(&dropped, 1);
    st.drops++;
  }

  uint32_t got;
  if (f < LOW_FRAMES && n >= 1) {
    got = ring.pop(out, n - 1);
    if (got == n - 1) {  // 夠 n-1 格才補；不夠就交給下面當 underrun
      out[n - 1] = (got > 0) ? out[got - 1] : StereoFrame{0, 0};
      st.dups++;
      got = n;
    }
  } else {
    got = ring.pop(out, n);
  }

  if (got < n) {
    memset(out + got, 0, (n - got) * sizeof(StereoFrame));
    st.underruns++;
    st.priming = true;
  }
}

#endif  // BRIDGE_DSP_H
