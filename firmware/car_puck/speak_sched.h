// speak_sched.h — 依「播放位置」套用講話旗標/頭部動作的 SPSC 小佇列，純 C++、不 include
// Arduino，host 可測。producer（network task）在收到 ICY 第 10 欄變化時排入
// {目前累計寫入 ring 的 bytes 位置, speaking}（type=0），在收到第 11 欄的新 gesture 時排入
// {同一種位置, gesture, level}（type=1）；consumer（playback task）依自己已讀出的 bytes
// 累計位置把事件依序消化，讓講話旗標/動作跟實際播出的音訊對齊（而非跟網路收到的時間點
// 對齊——ring buffer 蓄了多少秒，網路跟實際播放就差多少秒）。
#ifndef SPEAK_SCHED_H
#define SPEAK_SCHED_H

#include <stdint.h>

struct SpeakEvent { uint32_t pos; uint8_t type; bool speaking; int8_t gesture; uint8_t level; };

struct SpeakSched {
  static const int N = 16;
  SpeakEvent ev[N];
  volatile uint32_t head;   // producer（network task）寫
  volatile uint32_t tail;   // consumer（playback task）寫
  bool lastQueued;          // producer 專用：上次排進去的講話值（只在值改變時排）
  bool haveLastQueued;      // producer 專用
};

inline void speak_sched_init(SpeakSched& s) {
  s.head = 0;
  s.tail = 0;
  s.lastQueued = false;
  s.haveLastQueued = false;
}

// producer（講話事件）：值跟上次排的一樣 → 不排回 false；佇列滿 → 丟掉這筆回 false
// （不覆蓋）；否則排入回 true。
inline bool speak_sched_push(SpeakSched& s, uint32_t pos, bool speaking) {
  if (s.haveLastQueued && s.lastQueued == speaking) return false;
  uint32_t head = s.head, tail = s.tail;
  if (head - tail >= (uint32_t)SpeakSched::N) return false;  // 滿：丟掉這筆，不覆蓋舊的
  s.ev[head % SpeakSched::N] = SpeakEvent{pos, 0, speaking, -1, 0};
  s.head = head + 1;
  s.lastQueued = speaking;
  s.haveLastQueued = true;
  return true;
}

// producer（動作事件）：不受講話的「同值不重排」影響、不改 lastQueued；佇列滿回 false；
// 否則排入回 true。
inline bool speak_sched_push_gesture(SpeakSched& s, uint32_t pos, int gesture, int level) {
  uint32_t head = s.head, tail = s.tail;
  if (head - tail >= (uint32_t)SpeakSched::N) return false;  // 滿：丟掉這筆，不覆蓋舊的
  s.ev[head % SpeakSched::N] = SpeakEvent{pos, 1, false, (int8_t)gesture, (uint8_t)level};
  s.head = head + 1;
  return true;
}

struct SpeakApplyOut { bool speakApplied; bool speaking; int gesture; int level; };

// consumer：把所有 pos 已被播放位置追過的事件依序消化（判斷用有號差，處理 uint32 繞回）。
// 講話事件 → speakApplied=true、speaking=該值；動作事件 → gesture/level=該值（多筆取最後）。
// 進來先把 *out 設成 {false, false, -1, 0}；有消化任何事件回 true。
inline bool speak_sched_apply(SpeakSched& s, uint32_t readTotal, SpeakApplyOut* out) {
  *out = SpeakApplyOut{false, false, -1, 0};
  bool applied = false;
  uint32_t tail = s.tail, head = s.head;
  while (tail != head) {
    const SpeakEvent& e = s.ev[tail % SpeakSched::N];
    if ((int32_t)(readTotal - e.pos) < 0) break;  // 還沒播到這個事件的位置
    if (e.type == 0) {
      out->speakApplied = true;
      out->speaking = e.speaking;
    } else {
      out->gesture = e.gesture;
      out->level = e.level;
    }
    applied = true;
    tail++;
  }
  s.tail = tail;
  return applied;
}

// producer：重連時呼叫，讓下一筆講話事件不論值為何都會排入。
inline void speak_sched_reset_producer(SpeakSched& s) {
  s.haveLastQueued = false;
}

#endif  // SPEAK_SCHED_H
