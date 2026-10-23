// cover_fetch.h — 徽章封面下載（/car_cover）的純邏輯：JPEG 粗驗、HTTP 標頭解析、遙測
// 字串格式化。純 C++、不 include Arduino，host 可測。
//
// 2026-10-20：換歌時把 Mac 預算的封面抓進 PSRAM，先做壓力測試（還沒接螢幕顯示）。
#ifndef COVER_FETCH_H
#define COVER_FETCH_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

// JPEG 粗驗：n >= 4 且開頭 FF D8、結尾 FF D9。
inline bool cover_jpeg_valid(const uint8_t* p, size_t n) {
  if (!p || n < 4) return false;
  if (p[0] != 0xFF || p[1] != 0xD8) return false;
  if (p[n - 2] != 0xFF || p[n - 1] != 0xD9) return false;
  return true;
}

// "HTTP/1.1 200 OK" → 200。格式不對（沒有空白、空白後不是 3 位數字）→ 0。
inline int http_status_code(const char* line) {
  if (!line) return 0;
  const char* sp = strchr(line, ' ');
  if (!sp) return 0;
  const char* d = sp + 1;
  if (!d[0] || !d[1] || !d[2]) return 0;
  if (d[0] < '0' || d[0] > '9' || d[1] < '0' || d[1] > '9' || d[2] < '0' || d[2] > '9') return 0;
  return (d[0] - '0') * 100 + (d[1] - '0') * 10 + (d[2] - '0');
}

// 標頭行若為 Content-Length（名稱大小寫不分，冒號後可有任意空白）→ 回傳數值；
// 不是這個標頭、或值不是純數字（允許結尾 \r）、或空值 → -1。
inline long http_content_length(const char* line) {
  if (!line) return -1;
  static const char kName[] = "content-length";
  size_t i = 0;
  for (; kName[i]; i++) {
    char c = line[i];
    if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
    if (c != kName[i]) return -1;
  }
  if (line[i] != ':') return -1;
  i++;
  while (line[i] == ' ' || line[i] == '\t') i++;
  size_t start = i;
  long v = 0;
  bool any = false;
  for (; line[i]; i++) {
    char c = line[i];
    if (c == '\r' && line[i + 1] == '\0') { i++; break; }
    if (c < '0' || c > '9') return -1;
    v = v * 10 + (c - '0');
    any = true;
  }
  (void)start;
  if (!any) return -1;
  return v;
}

struct CoverStats {
  uint32_t ok, none, fail, busy;   // 成功／404 沒封面／失敗／Funnel 鎖忙
  uint32_t lastBytes, lastMs, lastWaitMs, maxMs;
  char lastVia;                    // 'L'＝區網 'F'＝Funnel '-'＝還沒有
  uint32_t ifree, imin;            // 內部 RAM 目前剩餘／開機以來最低
};

// 格式固定：
// "ok=%u none=%u fail=%u busy=%u last=%c/%uB/%ums wait=%ums max=%ums ifree=%u imin=%u"
// 回傳 snprintf 的回傳值。
inline int cover_sum_format(char* dst, size_t cap, const CoverStats& s) {
  return snprintf(dst, cap, "ok=%u none=%u fail=%u busy=%u last=%c/%uB/%ums wait=%ums max=%ums ifree=%u imin=%u",
                   (unsigned)s.ok, (unsigned)s.none, (unsigned)s.fail, (unsigned)s.busy,
                   s.lastVia, (unsigned)s.lastBytes, (unsigned)s.lastMs, (unsigned)s.lastWaitMs,
                   (unsigned)s.maxMs, (unsigned)s.ifree, (unsigned)s.imin);
}

#endif  // COVER_FETCH_H
