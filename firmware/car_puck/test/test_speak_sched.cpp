// test_speak_sched.cpp — speak_sched.h 的 host 端黑箱測試。
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o /tmp/test_speak_sched firmware/car_puck/test/test_speak_sched.cpp && /tmp/test_speak_sched
//
// 原則：只透過公開函式驗性質，不在測試裡重寫被測邏輯去比對。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>

#include "../speak_sched.h"

int main() {
  // T1: 同值不重排——push 同一個值兩次，第二次回 false、佇列裡只有一筆
  {
    SpeakSched s; speak_sched_init(s);
    assert(speak_sched_push(s, 100, true) == true);
    assert(speak_sched_push(s, 200, true) == false);  // 跟上次排的值一樣
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 1000, &out) == true);
    assert(out.speakApplied == true);
    assert(out.speaking == true);
    assert(speak_sched_apply(s, 2000, &out) == false);  // 沒有第二筆可消化
    assert(out.speakApplied == false);
    printf("T1 ok\n");
  }

  // T2: 位置未到不套用、到了才套用
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push(s, 500, true);
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 499, &out) == false);
    assert(speak_sched_apply(s, 500, &out) == true);
    assert(out.speakApplied == true);
    assert(out.speaking == true);
    printf("T2 ok\n");
  }

  // T3: 多筆一次追過取最後一筆
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push(s, 100, true);
    speak_sched_push(s, 200, false);
    speak_sched_push(s, 300, true);
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 1000, &out) == true);
    assert(out.speaking == true);   // 最後一筆（pos=300, true）
    printf("T3 ok\n");
  }

  // T4: 佇列滿丟新的不覆蓋舊的
  {
    SpeakSched s; speak_sched_init(s);
    // 交替值才能每筆都真的排入（同值不重排），灌滿到 N 筆
    for (int i = 0; i < SpeakSched::N; i++) {
      bool v = (i % 2) == 0;
      assert(speak_sched_push(s, (uint32_t)(i * 10), v) == true);
    }
    // 佇列已滿，下一筆（跟最後一筆排進去的值不同，確保不是被「同值不重排」擋掉）應該被丟棄
    bool lastQueuedVal = ((SpeakSched::N - 1) % 2) == 0;
    assert(speak_sched_push(s, 99999, !lastQueuedVal) == false);
    // 消化全部，驗證第一筆仍是原始第一筆（沒被覆蓋掉）
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 5, &out) == true);
    assert(out.speaking == true);  // i=0 → v=true
    printf("T4 ok\n");
  }

  // T5: uint32 繞回（pos 接近 0xFFFFFFFF、readTotal 繞過 0）仍正確
  {
    SpeakSched s; speak_sched_init(s);
    uint32_t pos = 0xFFFFFFF0u;
    speak_sched_push(s, pos, true);
    uint32_t readTotal = 0xFFFFFFF0u;
    SpeakApplyOut out;
    assert(speak_sched_apply(s, readTotal, &out) == true);
    assert(out.speaking == true);
    // 再排一筆繞回後的位置（pos 數值上比上一筆小，但時間上在後面）
    speak_sched_push(s, 5u, false);  // 0xFFFFFFF0 + 0x15 繞回成 5
    readTotal = 20u;  // 已經繞過 0，追過 pos=5
    assert(speak_sched_apply(s, readTotal, &out) == true);
    assert(out.speaking == false);
    printf("T5 ok\n");
  }

  // T5b: 事件在繞回點前、播放位置已繞過 0（數值上 readTotal < pos）仍要套用
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push(s, 0xFFFFFFF0u, true);
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 0x10u, &out) == true);
    assert(out.speaking == true);
    printf("T5b ok\n");
  }

  // T7: reset_producer 後同值也能排入
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push(s, 100, true);
    assert(speak_sched_push(s, 200, true) == false);  // 同值，不排
    speak_sched_reset_producer(s);
    assert(speak_sched_push(s, 300, true) == true);    // reset 後同值也能排入
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 1000, &out) == true);
    assert(out.speaking == true);
    printf("T7 ok\n");
  }

  // T8: 動作事件不受講話事件的「同值不重排」擋掉
  {
    SpeakSched s; speak_sched_init(s);
    assert(speak_sched_push(s, 100, true) == true);
    assert(speak_sched_push_gesture(s, 150, 1, 2) == true);
    assert(speak_sched_push(s, 200, true) == false);  // 講話同值仍被擋
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 1000, &out) == true);
    assert(out.speakApplied == true);
    assert(out.speaking == true);
    assert(out.gesture == 1);
    assert(out.level == 2);
    printf("T8 ok\n");
  }

  // T9: 講話與動作交錯時依位置各自套用
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push(s, 100, true);
    speak_sched_push_gesture(s, 200, 3, 1);
    speak_sched_push(s, 300, false);
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 150, &out) == true);
    assert(out.speakApplied == true);
    assert(out.speaking == true);
    assert(out.gesture == -1);  // 還沒追到動作事件的位置
    assert(speak_sched_apply(s, 250, &out) == true);
    assert(out.speakApplied == false);  // 這一段只追到動作事件
    assert(out.gesture == 3);
    assert(out.level == 1);
    assert(speak_sched_apply(s, 1000, &out) == true);
    assert(out.speakApplied == true);
    assert(out.speaking == false);
    printf("T9 ok\n");
  }

  // T10: 位置未到的動作事件不套用
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push_gesture(s, 500, 2, 3);
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 499, &out) == false);
    assert(out.gesture == -1);
    assert(speak_sched_apply(s, 500, &out) == true);
    assert(out.gesture == 2);
    assert(out.level == 3);
    printf("T10 ok\n");
  }

  // T11: 多個動作事件一次追過取最後一個
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push_gesture(s, 100, 0, 1);
    speak_sched_push_gesture(s, 200, 4, 3);
    SpeakApplyOut out;
    assert(speak_sched_apply(s, 1000, &out) == true);
    assert(out.gesture == 4);
    assert(out.level == 3);
    printf("T11 ok\n");
  }

  // T12: push 動作事件不改 lastQueued（push 動作後再 push 同值講話仍被擋）
  {
    SpeakSched s; speak_sched_init(s);
    assert(speak_sched_push(s, 100, true) == true);
    assert(speak_sched_push_gesture(s, 150, 1, 2) == true);
    assert(speak_sched_push(s, 200, true) == false);  // 講話值沒變，仍被同值擋
    printf("T12 ok\n");
  }

  printf("all speak_sched tests passed\n");
  return 0;
}
