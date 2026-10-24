// face_motion.h — 頭像姿勢律動/動作/封面交接/背光的純邏輯，純 C++、不 include Arduino，
// host 可測。公式逐字照抄已定案的銜接樣機 JS（見任務檔）。全部用呼叫端傳入的 float 秒，
// 不讀時鐘、不配 heap。
#ifndef FACE_MOTION_H
#define FACE_MOTION_H

#include <math.h>
#include <stdint.h>

#include "face_rt.h"  // FacePose

namespace face_motion {

constexpr float PI2 = 6.283185307179586f;

// FNV-1a：track_id 字串 → uint32，給小動作排程判斷「換歌」用。
// 放這裡不放 .ino：Arduino 自動原型會插在 .ino 第一個函式定義前，害 setLed(LedState) 跑到 enum 之前。
inline uint32_t fnv1a(const char* s) {
  uint32_t h = 2166136261u;
  for (; *s; s++) { h ^= (uint8_t)*s; h *= 16777619u; }
  return h;
}

inline float clamp01(float x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }
inline float ss(float x) { x = clamp01(x); return x * x * (3 - 2 * x); }
inline float tripE(float u, float a) {
  return u < a ? ss(u / a) : (u > 1 - a ? ss((1 - u) / a) : 1.0f);
}

// bpm → 節拍週期（秒）：從 {1,2,4}×beat 中選 |p-1| 最小者，平手取先出現（較小倍數）。
inline float nodPeriod(float bpm) {
  float beat = 60.0f / bpm;
  float mult[3] = {1.0f, 2.0f, 4.0f};
  float best = mult[0] * beat;
  float bestErr = fabsf(best - 1.0f);
  for (int i = 1; i < 3; i++) {
    float p = mult[i] * beat;
    float err = fabsf(p - 1.0f);
    if (err < bestErr) { bestErr = err; best = p; }
  }
  return best;
}

// ---------- pose 通道（跟 FacePose 欄位順序一致：yaw, pitch, roll, browUp, err） ----------
struct Pose5 { float yaw, pitch, roll, browUp, err; };

inline Pose5 layerStill() { return {0, 0, 0, 0, 0}; }

inline Pose5 layerSway(float T) {
  float per = 8.0f * 60.0f / 71.0f;
  float s = sinf(PI2 * T / per);
  return {0.45f * s, 0.06f * s * s, 0.3f * s, 0, 0};
}

inline Pose5 layerNod(float T, int bpm) {
  float per = nodPeriod((float)bpm);
  float n = floorf(T / per);
  float ph = T / per - n;
  bool odd = (((long)n) % 2) != 0;
  float amp = odd ? 0.6f : 1.0f;
  float d = ph < 0.45f ? ss(ph / 0.45f) : 1.0f - ss((ph - 0.45f) / 0.55f);
  return {0, 0.42f * amp * d, 0.06f * amp * d, 0, 0};
}

inline Pose5 layerListen(float t) {
  float c = fmodf(t, 1.8f);
  if (c < 0) c += 1.8f;
  float b = c < 0.7f ? sinf(3.14159265358979f * c / 0.7f) : 0.0f;
  b = b * b;
  return {-0.35f, -0.06f + 0.1f * b, 0.34f, 0, 0};
}

inline Pose5 layerThink(float t) {
  return {0.45f + 0.06f * sinf(1.3f * t), -0.3f, -0.14f, 0.04f, 0};
}

inline Pose5 layerSpeak(float t) {
  float b = sinf(3.14159265358979f * t / 1.0f);
  b = b * b;
  return {0.2f * b, 0.16f * b, 0, 0, 0};
}

inline Pose5 layerError(float t) {
  float yaw = 0.7f * sinf(PI2 * t / 0.8f) * ss(t / 0.2f);
  return {yaw, 0, 0, 0, 1.0f};
}

enum Layer { LAYER_STILL = 0, LAYER_SWAY, LAYER_NOD, LAYER_LISTEN, LAYER_THINK, LAYER_SPEAK, LAYER_ERROR, LAYER_COUNT };

inline Pose5 layerEval(Layer k, float t, float T, int bpm) {
  switch (k) {
    case LAYER_STILL: return layerStill();
    case LAYER_SWAY: return layerSway(T);
    case LAYER_NOD: return layerNod(T, bpm);
    case LAYER_LISTEN: return layerListen(t);
    case LAYER_THINK: return layerThink(t);
    case LAYER_SPEAK: return layerSpeak(t);
    case LAYER_ERROR: return layerError(t);
    default: return layerStill();
  }
}

// ---------- 動作標籤（gesture） ----------
enum Gesture { GESTURE_TILT = 0, GESTURE_YES, GESTURE_NO, GESTURE_GLANCE, GESTURE_PONDER, GESTURE_COUNT };

inline float gestureDuration(Gesture g) {
  static const float D[GESTURE_COUNT] = {1.8f, 1.3f, 1.5f, 2.4f, 2.2f};
  return D[g];
}

// 回傳 yaw,pitch,roll,browUp（不含 err，動作標籤不影響 err）
inline void gestureEval(Gesture g, float u, float* yaw, float* pitch, float* roll, float* browUp) {
  *yaw = *pitch = *roll = *browUp = 0;
  switch (g) {
    case GESTURE_TILT: {
      float e = tripE(u, 0.3f);
      *roll = 0.32f * e; *yaw = 0.35f * e; *pitch = -0.06f * e; *browUp = 0.05f * e;
      break;
    }
    case GESTURE_YES: {
      float s = sinf(PI2 * u);
      *pitch = 0.3f * s * s;
      break;
    }
    case GESTURE_NO: {
      *yaw = 0.5f * sinf(PI2 * 2.0f * u) * tripE(u, 0.2f);
      break;
    }
    case GESTURE_GLANCE: {
      float e = tripE(u, 0.3f);
      *yaw = -1.1f * e; *roll = -0.1f * e;
      break;
    }
    case GESTURE_PONDER: {
      float e = tripE(u, 0.35f);
      *pitch = -0.45f * e; *yaw = 0.4f * e; *roll = -0.16f * e; *browUp = 0.04f * e;
      break;
    }
    default: break;
  }
}

// ---------- Override / Inputs ----------
enum Override { OVERRIDE_NONE = 0, OVERRIDE_LISTEN, OVERRIDE_THINK, OVERRIDE_ERROR, OVERRIDE_SPEAK };

struct FaceInputs {
  Override override_;
  bool playing;
  int bpm;       // 0 = 未知
  float dur_s;
  float pos_s;
};

struct ActiveGesture {
  Gesture g;
  float start;   // T 起算
  bool active;
  float amp;
};

// 動作強度（ICY 第 11 欄的 level 1-3）→ 振幅倍率。
inline float gesture_level_amp(int level) {
  switch (level) {
    case 1: return 0.6f;
    case 2: return 1.0f;
    case 3: return 1.3f;
    default: return 1.0f;
  }
}

// ---------- 混合器 ----------
struct FaceMixer {
  float lin[LAYER_COUNT];        // 線性權重（往 target 走）
  float layerStart[LAYER_COUNT]; // 進入該層的 T（該層 target 最近一次由 0 變 1 的時間點）
  bool prevTarget[LAYER_COUNT];  // 上一格該層 target 是否為 1
  ActiveGesture gestures[4];
  // 彈簧狀態：yaw, pitch, roll, browUp, err
  float sx[5], sv[5];
};

inline void face_mixer_init(FaceMixer& m) {
  for (int i = 0; i < LAYER_COUNT; i++) {
    m.lin[i] = (i == LAYER_STILL) ? 1.0f : 0.0f;
    m.layerStart[i] = 0;
    m.prevTarget[i] = (i == LAYER_STILL);
  }
  for (int i = 0; i < 4; i++) m.gestures[i].active = false;
  for (int i = 0; i < 5; i++) { m.sx[i] = 0; m.sv[i] = 0; }
}

inline Layer grooveLayer(const FaceInputs& in) {
  if (!in.playing) return LAYER_STILL;
  if (in.bpm >= 120) return LAYER_NOD;   // 10/25 使用者定：120 當點頭分界（原 100）
  return LAYER_SWAY;
}

inline Layer overrideLayer(Override o) {
  switch (o) {
    case OVERRIDE_LISTEN: return LAYER_LISTEN;
    case OVERRIDE_THINK: return LAYER_THINK;
    case OVERRIDE_ERROR: return LAYER_ERROR;
    case OVERRIDE_SPEAK: return LAYER_SPEAK;
    default: return LAYER_STILL;
  }
}

// LED 來的 override + 講話/等回應狀態 → 最終 override。
// 優先序：ERROR > LISTEN > SPEAK > THINK（LED_PLAYING 或等回應中）> NONE。
inline Override resolve_override(Override ledOv, bool speaking, bool awaitingReply) {
  if (ledOv == OVERRIDE_ERROR || ledOv == OVERRIDE_LISTEN) return ledOv;
  if (speaking) return OVERRIDE_SPEAK;
  if (ledOv == OVERRIDE_THINK || awaitingReply) return OVERRIDE_THINK;
  return OVERRIDE_NONE;
}

// 外部請求一個新的動作標籤；最多同時 4 個進行中，多的丟掉最舊。
inline void face_mixer_trigger_gesture(FaceMixer& m, Gesture g, float T, float amp = 1.0f) {
  int slot = -1;
  for (int i = 0; i < 4; i++) {
    if (!m.gestures[i].active) { slot = i; break; }
  }
  if (slot < 0) {
    // 丟掉最舊的（start 最小者）
    int oldest = 0;
    for (int i = 1; i < 4; i++) {
      if (m.gestures[i].start < m.gestures[oldest].start) oldest = i;
    }
    slot = oldest;
  }
  m.gestures[slot].g = g;
  m.gestures[slot].start = T;
  m.gestures[slot].active = true;
  m.gestures[slot].amp = amp;
}

struct FacePoseOut {
  FacePose pose;
};

inline FacePoseOut face_mixer_step(FaceMixer& m, const FaceInputs& in, float T, float dt) {
  Layer target = (in.override_ != OVERRIDE_NONE) ? overrideLayer(in.override_) : grooveLayer(in);

  const float FADE_S = 0.4f;
  float maxStep = dt / FADE_S;
  float w[LAYER_COUNT];
  float sum = 0;
  for (int i = 0; i < LAYER_COUNT; i++) {
    bool tgtOn = (i == target);
    float tgt = tgtOn ? 1.0f : 0.0f;
    if (tgtOn && !m.prevTarget[i]) {
      m.layerStart[i] = T;   // 該層 target 由 0 變 1 的瞬間
    }
    m.prevTarget[i] = tgtOn;
    if (m.lin[i] < tgt) {
      m.lin[i] = m.lin[i] + maxStep < tgt ? m.lin[i] + maxStep : tgt;
    } else if (m.lin[i] > tgt) {
      m.lin[i] = m.lin[i] - maxStep > tgt ? m.lin[i] - maxStep : tgt;
    }
    w[i] = ss(m.lin[i]);
    sum += w[i];
  }
  if (sum > 1.0f) {
    for (int i = 0; i < LAYER_COUNT; i++) w[i] /= sum;
  }

  Pose5 p = {0, 0, 0, 0, 0};
  for (int i = 0; i < LAYER_COUNT; i++) {
    if (w[i] <= 0.0f) continue;
    Pose5 lp = layerEval((Layer)i, T - m.layerStart[i], T, in.bpm);
    float fade = 1.0f;
    if (i == LAYER_SWAY || i == LAYER_NOD) {
      if (in.dur_s > 0.0f) {
        float remain = in.dur_s - in.pos_s;
        fade = ss(remain / 15.0f);
      }
    }
    p.yaw += lp.yaw * w[i] * (i == LAYER_SWAY || i == LAYER_NOD ? fade : 1.0f);
    p.pitch += lp.pitch * w[i] * (i == LAYER_SWAY || i == LAYER_NOD ? fade : 1.0f);
    p.roll += lp.roll * w[i] * (i == LAYER_SWAY || i == LAYER_NOD ? fade : 1.0f);
    p.browUp += lp.browUp * w[i];
    p.err += lp.err * w[i];
  }

  // gesture 疊加
  float duck = 0.0f;
  float gyaw = 0, gpitch = 0, groll = 0, gbrow = 0;
  for (int i = 0; i < 4; i++) {
    if (!m.gestures[i].active) continue;
    float d = gestureDuration(m.gestures[i].g);
    float u = (T - m.gestures[i].start) / d;
    if (u >= 1.0f) { m.gestures[i].active = false; continue; }
    float gy, gp, gr, gb;
    gestureEval(m.gestures[i].g, u, &gy, &gp, &gr, &gb);
    float amp = m.gestures[i].amp;
    gyaw += gy * amp; gpitch += gp * amp; groll += gr * amp; gbrow += gb * amp;
    float e = tripE(u, 0.25f);
    if (e > duck) duck = e;
  }
  float k = 1.0f - duck * (1.0f - 0.3f);
  p.yaw = p.yaw * k + gyaw;
  p.pitch = p.pitch * k + gpitch;
  p.roll = p.roll * k + groll;
  p.browUp = p.browUp * k + gbrow;

  // 臨界阻尼彈簧，5 通道：yaw, pitch, roll, browUp, err
  float target5[5] = {p.yaw, p.pitch, p.roll, p.browUp, p.err};
  const float om = 4.7f / 0.25f;
  const float h = dt / 4.0f;
  for (int s = 0; s < 4; s++) {
    for (int i = 0; i < 5; i++) {
      float a = om * om * (target5[i] - m.sx[i]) - 2.0f * om * m.sv[i];
      m.sv[i] += a * h;
      m.sx[i] += m.sv[i] * h;
    }
  }

  FacePoseOut out;
  out.pose.yaw = m.sx[0];
  out.pose.pitch = m.sx[1];
  out.pose.roll = m.sx[2];
  out.pose.browUp = m.sx[3];
  out.pose.err = m.sx[4];
  return out;
}

// ---------- 小動作排程 ----------
struct FaceIdleGestures {
  uint32_t trackKey;
  bool haveTrack;
  bool firedThisTrack;
  int nextGestureParity;   // 0=GLANCE, 1=PONDER（跨歌交替）
  bool wasPlaying;
  float noPlayNextAt;      // 下次「沒歌」觸發時間（T 座標）；<0 代表未排
  bool haveNoPlaySchedule;
};

inline void face_idle_init(FaceIdleGestures& st) {
  st.trackKey = 0;
  st.haveTrack = false;
  st.firedThisTrack = false;
  st.nextGestureParity = 0;
  st.wasPlaying = false;
  st.noPlayNextAt = 0;
  st.haveNoPlaySchedule = false;
}

// 回傳要觸發的 gestureId（Gesture enum 值）或 -1。
inline int face_idle_step(FaceIdleGestures& st, const FaceInputs& in, uint32_t trackKey, float T, float rand01) {
  int result = -1;

  if (in.playing && in.dur_s > 0.0f) {
    if (!st.haveTrack || st.trackKey != trackKey) {
      st.trackKey = trackKey;
      st.haveTrack = true;
      st.firedThisTrack = false;
    }
    float frac = in.pos_s / in.dur_s;
    if (!st.firedThisTrack && frac >= 0.45f && frac <= 0.65f) {
      st.firedThisTrack = true;
      if (in.override_ == OVERRIDE_NONE) {
        result = (st.nextGestureParity == 0) ? (int)GESTURE_GLANCE : (int)GESTURE_PONDER;
        st.nextGestureParity = 1 - st.nextGestureParity;
      }
    }
    st.haveNoPlaySchedule = false;
  } else if (in.playing) {
    // 有歌但不知道長度：不觸發小動作，也不算沒歌
    st.haveNoPlaySchedule = false;
  } else {
    if (!st.haveNoPlaySchedule) {
      st.noPlayNextAt = T + 20.0f + 20.0f * rand01;
      st.haveNoPlaySchedule = true;
    }
    if (T >= st.noPlayNextAt) {
      if (in.override_ == OVERRIDE_NONE) {
        result = (int)GESTURE_GLANCE;
      }
      st.noPlayNextAt = T + 20.0f + 20.0f * rand01;
    }
  }

  st.wasPlaying = in.playing;
  return result;
}

// ---------- 封面狀態機 ----------
enum CoverPhase { COVER_HEAD = 0, COVER_FADE_OUT, COVER_COVER, COVER_FADE_IN };

struct FaceCoverState {
  CoverPhase phase;
  float phaseStart;
  bool overriddenDuringFade;  // FADE_IN 期間若是被 override 打斷，縮短時長
};

inline void face_cover_init(FaceCoverState& st) {
  st.phase = COVER_HEAD;
  st.phaseStart = 0;
  st.overriddenDuringFade = false;
}

struct FaceCoverOut {
  int phase;
  float brightnessScale;
  bool drawCoverNow;
  bool redrawHeadNow;
};

inline FaceCoverOut face_cover_step(FaceCoverState& st, float now, bool coverArrived,
                                     float trackChangedAt, bool overridden) {
  FaceCoverOut out = {(int)st.phase, 1.0f, false, false};

  // 任何非 HEAD 階段遇到 overridden：立刻要求 redrawHeadNow，FADE_IN 縮成 0.15s 內亮度回
  // 1，這首不補播。用 (phase==FADE_IN && overriddenDuringFade) 當「已經在處理這次打斷」
  // 的旗標，避免每格都重新觸發、永遠跑不完 0.15s。
  bool alreadyHandlingInterrupt = (st.phase == COVER_FADE_IN && st.overriddenDuringFade);
  if (overridden && st.phase != COVER_HEAD && !alreadyHandlingInterrupt) {
    st.phase = COVER_FADE_IN;
    st.phaseStart = now;
    st.overriddenDuringFade = true;
    out.phase = (int)st.phase;
    out.redrawHeadNow = true;
    out.brightnessScale = 0.0f;
    return out;
  }

  switch (st.phase) {
    case COVER_HEAD: {
      if (coverArrived && (now - trackChangedAt) <= 15.0f && !overridden) {
        st.phase = COVER_FADE_OUT;
        st.phaseStart = now;
        st.overriddenDuringFade = false;
      }
      out.brightnessScale = 1.0f;
      break;
    }
    case COVER_FADE_OUT: {
      float t = now - st.phaseStart;
      const float D = 0.3f;
      if (t >= D) {
        out.drawCoverNow = true;
        out.brightnessScale = 0.0f;
        st.phase = COVER_COVER;
        st.phaseStart = now;
      } else {
        out.brightnessScale = 1.0f - t / D;
      }
      break;
    }
    case COVER_COVER: {
      float t = now - st.phaseStart;
      const float D = 6.0f;
      out.brightnessScale = 1.0f;
      if (t >= D) {
        st.phase = COVER_FADE_IN;
        st.phaseStart = now;
        st.overriddenDuringFade = false;
        out.redrawHeadNow = true;
        out.brightnessScale = 0.0f;
      }
      break;
    }
    case COVER_FADE_IN: {
      float D = st.overriddenDuringFade ? 0.15f : 0.3f;
      float t = now - st.phaseStart;
      if (t >= D) {
        st.phase = COVER_HEAD;
        out.brightnessScale = 1.0f;
      } else {
        out.brightnessScale = t / D;
      }
      break;
    }
  }

  out.phase = (int)st.phase;
  return out;
}

// ---------- 背光策略 ----------
inline uint8_t face_backlight(bool timeValid, int localHour, int localMin, float idleSec, bool overridden) {
  uint8_t dayNight;
  if (!timeValid) {
    dayNight = 200;
  } else {
    int minsOfDay = localHour * 60 + localMin;
    bool day = minsOfDay >= (6 * 60 + 30) && minsOfDay < (18 * 60);
    dayNight = day ? 200 : 60;
  }
  if (overridden) return dayNight;
  if (idleSec >= 600.0f) return 0;
  return dayNight;
}

}  // namespace face_motion

#endif  // FACE_MOTION_H
