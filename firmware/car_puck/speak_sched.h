// speak_sched.h — 依「播放位置」套用講話旗標的 SPSC 小佇列，純 C++、不 include Arduino，
// host 可測。producer（network task）在收到 ICY 第 10 欄變化時排入
// {目前累計寫入 ring 的 bytes 位置, speaking}；consumer（playback task）依自己已讀出的
// bytes 累計位置把事件依序消化，讓講話旗標跟實際播出的音訊對齊（而非跟網路收到的時間點
// 對齊——ring buffer 蓄了多少秒，網路跟實際播放就差多少秒）。
#ifndef SPEAK_SCHED_H
#define SPEAK_SCHED_H

#include <stdint.h>

struct SpeakEvent { uint32_t pos; bool speaking; };

struct SpeakSched {
  static const int N = 16;
  SpeakEvent ev[N];
  volatile uint32_t head;   // producer（network task）寫
  volatile uint32_t tail;   // consumer（playback task）寫
  bool lastQueued;          // producer 專用：上次排進去的值（只在值改變時排）
  bool haveLastQueued;      // producer 專用
};

inline void speak_sched_init(SpeakSched& s) {
  s.head = 0;
  s.tail = 0;
  s.lastQueued = false;
  s.haveLastQueued = false;
}

// producer：值跟上次排的一樣 → 不排回 false；佇列滿 → 丟掉這筆回 false（不覆蓋）；否則排入回 true。
inline bool speak_sched_push(SpeakSched& s, uint32_t pos, bool speaking) {
  if (s.haveLastQueued && s.lastQueued == speaking) return false;
  uint32_t head = s.head, tail = s.tail;
  if (head - tail >= (uint32_t)SpeakSched::N) return false;  // 滿：丟掉這筆，不覆蓋舊的
  s.ev[head % SpeakSched::N] = SpeakEvent{pos, speaking};
  s.head = head + 1;
  s.lastQueued = speaking;
  s.haveLastQueued = true;
  return true;
}

// consumer：把所有 pos 已被播放位置追過的事件依序消化（判斷用有號差，處理 uint32 繞回），
// *speaking 更新成最後一筆被消化事件的值；有消化任何事件回 true。
inline bool speak_sched_apply(SpeakSched& s, uint32_t readTotal, bool* speaking) {
  bool applied = false;
  uint32_t tail = s.tail, head = s.head;
  while (tail != head) {
    const SpeakEvent& e = s.ev[tail % SpeakSched::N];
    if ((int32_t)(readTotal - e.pos) < 0) break;  // 還沒播到這個事件的位置
    *speaking = e.speaking;
    applied = true;
    tail++;
  }
  s.tail = tail;
  return applied;
}

// producer：重連時呼叫，讓下一筆不論值為何都會排入。
inline void speak_sched_reset_producer(SpeakSched& s) {
  s.haveLastQueued = false;
}

#endif  // SPEAK_SCHED_H
