// test_face_motion.cpp — face_motion.h 的 host 端黑箱測試。
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o /tmp/test_face_motion firmware/car_puck/test/test_face_motion.cpp && /tmp/test_face_motion
//
// 原則：只透過公開函式驗性質，不在測試裡重寫被測公式去比對。

#undef NDEBUG
#include <assert.h>
#include <math.h>
#include <stdio.h>

#include "../face_motion.h"

using namespace face_motion;

static const float DT = 1.0f / 60.0f;

// 連續跑 mixer durationSec 秒（dt 固定），回傳最終 pose。
static FacePoseOut runMixer(FaceMixer& m, const FaceInputs& in, float startT, float durationSec, float dt = DT) {
  FacePoseOut out{};
  int steps = (int)(durationSec / dt);
  float T = startT;
  for (int i = 0; i < steps; i++) {
    T += dt;
    out = face_mixer_step(m, in, T, dt);
  }
  return out;
}

int main() {
  // T1: override 優先：playing+bpm=150 給 LISTEN，1s 後逼近 LISTEN 定值
  {
    FaceMixer m; face_mixer_init(m);
    FaceInputs in{OVERRIDE_LISTEN, true, 150, 180.0f, 10.0f};
    FacePoseOut p = runMixer(m, in, 0.0f, 1.0f);
    assert(fabsf(p.pose.roll - 0.34f) < 0.02f);
    assert(fabsf(p.pose.yaw - (-0.35f)) < 0.02f);
    printf("T1 ok (roll=%.4f yaw=%.4f)\n", p.pose.roll, p.pose.yaw);

    // 撤掉 override 後 1s 內離開（roll 應明顯偏離 0.34）
    FaceInputs in2{OVERRIDE_NONE, true, 150, 180.0f, 10.0f};
    FacePoseOut p2 = runMixer(m, in2, 1.0f, 1.0f);
    assert(fabsf(p2.pose.roll - 0.34f) > 0.1f);
    printf("T1b ok (roll after leaving=%.4f)\n", p2.pose.roll);
  }

  // T2: 淡換連續——groove↔override 10 秒內來回切換，每格單一通道變化量 < 0.15
  {
    FaceMixer m; face_mixer_init(m);
    float T = 0.0f;
    float prev[5] = {0, 0, 0, 0, 0};
    bool first = true;
    bool toggled = false;
    for (int i = 0; i < (int)(10.0f / DT); i++) {
      T += DT;
      int sec = (int)T;
      toggled = (sec % 2) == 0;
      FaceInputs in{toggled ? OVERRIDE_THINK : OVERRIDE_NONE, true, 150, 180.0f, 10.0f};
      FacePoseOut p = face_mixer_step(m, in, T, DT);
      float cur[5] = {p.pose.yaw, p.pose.pitch, p.pose.roll, p.pose.browUp, p.pose.err};
      if (!first) {
        for (int c = 0; c < 5; c++) {
          float d = fabsf(cur[c] - prev[c]);
          assert(d < 0.15f);
        }
      }
      for (int c = 0; c < 5; c++) prev[c] = cur[c];
      first = false;
    }
    printf("T2 ok\n");
  }

  // T3: nodPeriod
  {
    assert(fabsf(nodPeriod(60) - 1.0f) < 1e-4f);
    assert(fabsf(nodPeriod(120) - 1.0f) < 1e-4f);
    assert(fabsf(nodPeriod(158) - 0.759493671f) < 1e-4f);
    assert(fabsf(nodPeriod(200) - 1.2f) < 1e-4f);
    assert(fabsf(nodPeriod(45) - 1.333333f) < 1e-4f);
    printf("T3 ok\n");
  }

  // T4: groove 選擇
  {
    // bpm=99 -> SWAY：yaw 會擺動（取兩個不同時間點比較）
    {
      FaceMixer m; face_mixer_init(m);
      FaceInputs in{OVERRIDE_NONE, true, 99, 180.0f, 10.0f};
      FacePoseOut a = runMixer(m, in, 0.0f, 2.0f);
      FacePoseOut b = runMixer(m, in, 2.0f, 2.0f);
      assert(fabsf(a.pose.yaw - b.pose.yaw) > 0.05f);
      printf("T4a ok (sway yaw osc a=%.4f b=%.4f)\n", a.pose.yaw, b.pose.yaw);
    }
    // bpm=100 -> NOD：pitch 擺動、yaw≈0
    {
      FaceMixer m; face_mixer_init(m);
      FaceInputs in{OVERRIDE_NONE, true, 100, 180.0f, 10.0f};
      FacePoseOut a = runMixer(m, in, 0.0f, 2.0f);
      FacePoseOut b = runMixer(m, in, 2.0f, 2.0f);
      assert(fabsf(a.pose.pitch - b.pose.pitch) > 0.05f);
      assert(fabsf(a.pose.yaw) < 0.05f);
      assert(fabsf(b.pose.yaw) < 0.05f);
      printf("T4b ok (nod pitch osc a=%.4f b=%.4f yaw a=%.4f b=%.4f)\n",
             a.pose.pitch, b.pose.pitch, a.pose.yaw, b.pose.yaw);
    }
    // !playing -> STILL：收斂到 0
    {
      FaceMixer m; face_mixer_init(m);
      FaceInputs in{OVERRIDE_NONE, false, 0, 0.0f, 0.0f};
      FacePoseOut p = runMixer(m, in, 0.0f, 2.0f);
      assert(fabsf(p.pose.yaw) < 0.01f);
      assert(fabsf(p.pose.pitch) < 0.01f);
      assert(fabsf(p.pose.roll) < 0.01f);
      printf("T4c ok (still yaw=%.4f pitch=%.4f roll=%.4f)\n", p.pose.yaw, p.pose.pitch, p.pose.roll);
    }
  }

  // T5: 歌尾漸弱
  {
    // remain=0（dur=pos）-> SWAY 輸出收斂到 0
    {
      FaceMixer m; face_mixer_init(m);
      FaceInputs in{OVERRIDE_NONE, true, 80, 100.0f, 100.0f};  // remain = 0
      FacePoseOut p = runMixer(m, in, 0.0f, 3.0f);
      assert(fabsf(p.pose.yaw) < 0.01f);
      assert(fabsf(p.pose.roll) < 0.01f);
      printf("T5a ok (fade-to-zero yaw=%.4f roll=%.4f)\n", p.pose.yaw, p.pose.roll);
    }
    // remain=60 -> 振幅正常（跟 !playing 比較，應明顯非零）
    {
      FaceMixer m; face_mixer_init(m);
      FaceInputs in{OVERRIDE_NONE, true, 80, 180.0f, 120.0f};  // remain = 60
      FacePoseOut a = runMixer(m, in, 0.0f, 2.0f);
      FacePoseOut b = runMixer(m, in, 2.0f, 2.0f);
      assert(fabsf(a.pose.yaw - b.pose.yaw) > 0.05f);
      printf("T5b ok (full amplitude yaw a=%.4f b=%.4f)\n", a.pose.yaw, b.pose.yaw);
    }
  }

  // T6: 小動作排程
  {
    // 有歌：每首只觸發一次且在 45-65% 內
    {
      FaceIdleGestures st; face_idle_init(st);
      FaceInputs in{OVERRIDE_NONE, true, 0, 100.0f, 0.0f};
      int fired = -1;
      int fireCount = 0;
      float firePos = 0;
      for (int i = 0; i <= 100; i++) {
        in.pos_s = 100.0f * i / 100.0f;
        int g = face_idle_step(st, in, 42, (float)i, 0.0f);
        if (g >= 0) { fireCount++; fired = g; firePos = in.pos_s; }
      }
      assert(fireCount == 1);
      assert(firePos >= 45.0f && firePos <= 65.0f);
      printf("T6a ok (fired=%d at pos=%.1f)\n", fired, firePos);
    }
    // 連兩首依序 GLANCE、PONDER
    {
      FaceIdleGestures st; face_idle_init(st);
      FaceInputs in{OVERRIDE_NONE, true, 0, 100.0f, 50.0f};
      int g1 = -1;
      for (int i = 0; i <= 100; i++) {
        in.pos_s = 100.0f * i / 100.0f;
        int g = face_idle_step(st, in, 1, (float)i, 0.0f);
        if (g >= 0) g1 = g;
      }
      assert(g1 == (int)GESTURE_GLANCE);
      int g2 = -1;
      for (int i = 0; i <= 100; i++) {
        in.pos_s = 100.0f * i / 100.0f;
        int g = face_idle_step(st, in, 2, (float)(200 + i), 0.0f);
        if (g >= 0) g2 = g;
      }
      assert(g2 == (int)GESTURE_PONDER);
      printf("T6b ok (g1=%d g2=%d)\n", g1, g2);
    }
    // override 期間經過 45-65%，這首不補
    {
      FaceIdleGestures st; face_idle_init(st);
      FaceInputs in{OVERRIDE_LISTEN, true, 0, 100.0f, 0.0f};
      bool fired = false;
      for (int i = 0; i <= 100; i++) {
        in.pos_s = 100.0f * i / 100.0f;
        int g = face_idle_step(st, in, 7, (float)i, 0.0f);
        if (g >= 0) fired = true;
      }
      assert(!fired);
      printf("T6c ok (no补)\n");
    }
    // 沒歌：rand01=0 -> 20s；rand01=1 -> 40s
    {
      FaceIdleGestures st; face_idle_init(st);
      FaceInputs in{OVERRIDE_NONE, false, 0, 0.0f, 0.0f};
      int fireAt = -1;
      for (int i = 0; i <= 50; i++) {
        int g = face_idle_step(st, in, 0, (float)i, 0.0f);
        if (g >= 0 && fireAt < 0) fireAt = i;
      }
      assert(fireAt == 20);
      printf("T6d ok (rand01=0 fireAt=%d)\n", fireAt);
    }
    {
      FaceIdleGestures st; face_idle_init(st);
      FaceInputs in{OVERRIDE_NONE, false, 0, 0.0f, 0.0f};
      int fireAt = -1;
      for (int i = 0; i <= 50; i++) {
        int g = face_idle_step(st, in, 0, (float)i, 1.0f);
        if (g >= 0 && fireAt < 0) fireAt = i;
      }
      assert(fireAt == 40);
      printf("T6e ok (rand01=1 fireAt=%d)\n", fireAt);
    }
  }

  // T7: 封面狀態機
  {
    // 正常流程各 phase 時間點（0.3/6/0.3）、drawCoverNow/redrawHeadNow 各只出現一格
    {
      FaceCoverState st; face_cover_init(st);
      int drawCount = 0, redrawCount = 0;
      int phase = -1;
      float dt = 0.01f;
      float now = 0.0f;
      bool coverArrivedOnce = false;
      for (int i = 0; i < (int)(7.0f / dt); i++) {
        now += dt;
        bool arrived = !coverArrivedOnce && now >= 0.05f;
        if (arrived) coverArrivedOnce = true;
        FaceCoverOut out = face_cover_step(st, now, arrived, 0.0f, false);
        if (out.drawCoverNow) { drawCount++; assert(out.phase == (int)COVER_COVER); }
        if (out.redrawHeadNow) { redrawCount++; }
        phase = out.phase;
      }
      assert(drawCount == 1);
      assert(redrawCount == 1);
      assert(phase == (int)COVER_HEAD);
      printf("T7a ok (draw=%d redraw=%d finalPhase=%d)\n", drawCount, redrawCount, phase);
    }
    // 換歌 16s 後到不切（超過 15s 門檻）
    {
      FaceCoverState st; face_cover_init(st);
      FaceCoverOut out = face_cover_step(st, 16.0f, true, 0.0f, false);
      assert(out.phase == (int)COVER_HEAD);
      printf("T7b ok\n");
    }
    // COVER 中 override -> 0.15s 內亮度回 1 且回 HEAD；期間第二張忽略
    {
      FaceCoverState st; face_cover_init(st);
      float now = 0.0f, dt = 0.01f;
      // 先走到 COVER phase
      bool fired = false;
      while (st.phase != COVER_COVER) {
        now += dt;
        bool arrived = !fired && now >= 0.05f;
        if (arrived) fired = true;
        face_cover_step(st, now, arrived, 0.0f, false);
      }
      // COVER 中來一張忽略
      FaceCoverOut ignoreOut = face_cover_step(st, now + dt, true, 0.0f, false);
      now += dt;
      assert(ignoreOut.phase == (int)COVER_COVER);
      // override 打斷
      FaceCoverOut o1 = face_cover_step(st, now + dt, false, 0.0f, true);
      now += dt;
      assert(o1.phase == (int)COVER_FADE_IN);
      float t0 = now;
      float finalBright = 0;
      int finalPhase = -1;
      while (finalPhase != (int)COVER_HEAD && now - t0 < 1.0f) {
        now += dt;
        FaceCoverOut o = face_cover_step(st, now, true /* 忽略 */, 0.0f, true);
        finalBright = o.brightnessScale;
        finalPhase = o.phase;
      }
      assert(now - t0 <= 0.16f + dt);  // 0.15s 內完成（容忍一格數值誤差）
      assert(fabsf(finalBright - 1.0f) < 0.05f);
      assert(finalPhase == (int)COVER_HEAD);
      printf("T7c ok (settleT=%.3f finalBright=%.3f)\n", now - t0, finalBright);
    }
  }

  // T8: 背光
  {
    assert(face_backlight(true, 6, 29, 0, false) == 60);
    assert(face_backlight(true, 6, 30, 0, false) == 200);
    assert(face_backlight(true, 17, 59, 0, false) == 200);
    assert(face_backlight(true, 18, 0, 0, false) == 60);
    assert(face_backlight(false, 12, 0, 0, false) == 200);
    assert(face_backlight(true, 12, 0, 600, false) == 0);
    assert(face_backlight(true, 12, 0, 600, true) == 200);   // overridden：照日夜亮度，無視 idle
    printf("T8 ok\n");
  }

  printf("all face_motion tests passed\n");
  return 0;
}
