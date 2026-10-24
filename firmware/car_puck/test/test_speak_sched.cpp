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
    bool spk = false;
    assert(speak_sched_apply(s, 1000, &spk) == true);
    assert(spk == true);
    assert(speak_sched_apply(s, 2000, &spk) == false);  // 沒有第二筆可消化
    printf("T1 ok\n");
  }

  // T2: 位置未到不套用、到了才套用
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push(s, 500, true);
    bool spk = false;
    assert(speak_sched_apply(s, 499, &spk) == false);
    assert(speak_sched_apply(s, 500, &spk) == true);
    assert(spk == true);
    printf("T2 ok\n");
  }

  // T3: 多筆一次追過取最後一筆
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push(s, 100, true);
    speak_sched_push(s, 200, false);
    speak_sched_push(s, 300, true);
    bool spk = false;
    assert(speak_sched_apply(s, 1000, &spk) == true);
    assert(spk == true);   // 最後一筆（pos=300, true）
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
    bool spk = false;
    assert(speak_sched_apply(s, 5, &spk) == true);
    assert(spk == true);  // i=0 → v=true
    printf("T4 ok\n");
  }

  // T5: uint32 繞回（pos 接近 0xFFFFFFFF、readTotal 繞過 0）仍正確
  {
    SpeakSched s; speak_sched_init(s);
    uint32_t pos = 0xFFFFFFF0u;
    speak_sched_push(s, pos, true);
    uint32_t readTotal = 0xFFFFFFF0u;
    bool spk = false;
    assert(speak_sched_apply(s, readTotal, &spk) == true);
    assert(spk == true);
    // 再排一筆繞回後的位置（pos 數值上比上一筆小，但時間上在後面）
    speak_sched_push(s, 5u, false);  // 0xFFFFFFF0 + 0x15 繞回成 5
    readTotal = 20u;  // 已經繞過 0，追過 pos=5
    assert(speak_sched_apply(s, readTotal, &spk) == true);
    assert(spk == false);
    printf("T5 ok\n");
  }

  // T7: reset_producer 後同值也能排入
  {
    SpeakSched s; speak_sched_init(s);
    speak_sched_push(s, 100, true);
    assert(speak_sched_push(s, 200, true) == false);  // 同值，不排
    speak_sched_reset_producer(s);
    assert(speak_sched_push(s, 300, true) == true);    // reset 後同值也能排入
    bool spk = false;
    assert(speak_sched_apply(s, 1000, &spk) == true);
    assert(spk == true);
    printf("T7 ok\n");
  }

  printf("all speak_sched tests passed\n");
  return 0;
}
