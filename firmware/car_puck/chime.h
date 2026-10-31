// chime.h — 喚醒/PTT ack 提示音產生，純 C++、不 include Arduino，host 可測。
//
// 兩個音 880Hz→1320Hz，各 90ms，中間 20ms 靜音；每個音 10ms 線性 attack + 30ms 線性
// release，峰值振幅 = amp。16kHz mono int16。總長 (90+20+90)ms = 3200 樣本：
//   [0, 1440)     tone1 880Hz
//   [1440, 1760)  靜音
//   [1760, 3200)  tone2 1320Hz
#ifndef CHIME_H
#define CHIME_H

#include <math.h>
#include <stddef.h>
#include <stdint.h>

namespace chime_detail {
constexpr int kSampleRate = 16000;
constexpr int kToneSamples = kSampleRate / 1000 * 90;       // 1440
constexpr int kSilenceSamples = kSampleRate / 1000 * 20;     // 320
constexpr int kAttackSamples = kSampleRate / 1000 * 10;      // 160
constexpr int kReleaseSamples = kSampleRate / 1000 * 30;     // 480
constexpr float kPi = 3.14159265358979323846f;

inline float tone_envelope(int i) {
  if (i < kAttackSamples) return (float)i / (float)kAttackSamples;
  int releaseStart = kToneSamples - kReleaseSamples;
  if (i >= releaseStart) return (float)(kToneSamples - i) / (float)kReleaseSamples;
  return 1.0f;
}

inline int16_t tone_sample(int i, int freqHz, int16_t amp) {
  float env = tone_envelope(i);
  float s = sinf(2.0f * kPi * (float)freqHz * (float)i / (float)kSampleRate);
  return (int16_t)lroundf(env * (float)amp * s);
}
}  // namespace chime_detail

inline size_t chime_render(int16_t* out, size_t cap, int16_t amp) {
  using namespace chime_detail;
  const size_t total = (size_t)(kToneSamples * 2 + kSilenceSamples);
  size_t n = cap < total ? cap : total;
  for (size_t i = 0; i < n; i++) {
    int idx = (int)i;
    if (idx < kToneSamples) {
      out[i] = tone_sample(idx, 880, amp);
    } else if (idx < kToneSamples + kSilenceSamples) {
      out[i] = 0;
    } else {
      out[i] = tone_sample(idx - kToneSamples - kSilenceSamples, 1320, amp);
    }
  }
  return n;
}

#endif  // CHIME_H
