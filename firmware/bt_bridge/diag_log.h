// diag_log.h — bt_bridge 配對診斷紀錄環形緩衝（純 C++，不依賴 Arduino / ESP-IDF，可在 host 端測試）
//
// 每條一行固定 DIAG_TEXT bytes（含結尾 '\0'）；滿了覆蓋最舊的一條。
// head = 下一個寫入位置；count 最大為 DIAG_MAX。
#ifndef DIAG_LOG_H
#define DIAG_LOG_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>

static const int DIAG_MAX = 16;
static const int DIAG_TEXT = 40;

struct DiagLog {
  char line[DIAG_MAX][DIAG_TEXT];
  uint8_t count = 0;
  uint8_t head = 0;  // 下一個寫入位置
};

// 截斷到 DIAG_TEXT-1 並補 '\0'；滿了覆蓋最舊。
inline void diag_add(DiagLog& d, const char* text) {
  char* dst = d.line[d.head];
  size_t n = strlen(text);
  if (n > DIAG_TEXT - 1) n = DIAG_TEXT - 1;
  memcpy(dst, text, n);
  dst[n] = '\0';
  d.head = (uint8_t)((d.head + 1) % DIAG_MAX);
  if (d.count < DIAG_MAX) d.count++;
}

// i=0 最舊 … count-1 最新。越界回 0，否則複製到 out（至少 DIAG_TEXT bytes）並回 1。
inline int diag_get(const DiagLog& d, int i, char* out) {
  if (i < 0 || i >= d.count) return 0;
  int oldest = (d.head + DIAG_MAX - d.count) % DIAG_MAX;
  memcpy(out, d.line[(oldest + i) % DIAG_MAX], DIAG_TEXT);
  return 1;
}

// 格式：[count][line×count，依最舊→最新，每條固定 DIAG_TEXT bytes]。cap 不夠回 0。
inline size_t diag_serialize(const DiagLog& d, uint8_t* buf, size_t cap) {
  size_t need = 1 + (size_t)DIAG_TEXT * d.count;
  if (cap < need) return 0;
  buf[0] = d.count;
  for (int i = 0; i < d.count; i++) {
    diag_get(d, i, reinterpret_cast<char*>(buf + 1 + (size_t)DIAG_TEXT * i));
  }
  return need;
}

// 解析失敗（count 超界或長度不符）→ d 清空回 false。
inline bool diag_deserialize(DiagLog& d, const uint8_t* buf, size_t len) {
  if (len < 1 || buf[0] > DIAG_MAX || len != 1 + (size_t)DIAG_TEXT * buf[0]) {
    d = DiagLog();
    return false;
  }
  d = DiagLog();
  d.count = buf[0];
  for (int i = 0; i < d.count; i++) {
    memcpy(d.line[i], buf + 1 + (size_t)DIAG_TEXT * i, DIAG_TEXT);
    d.line[i][DIAG_TEXT - 1] = '\0';
  }
  d.head = (uint8_t)(d.count % DIAG_MAX);
  return true;
}

#endif  // DIAG_LOG_H
