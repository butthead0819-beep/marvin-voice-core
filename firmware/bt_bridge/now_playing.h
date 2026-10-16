// now_playing.h — 車機顯示曲名第一刀：WROOM 端解析從 S3 UART 收到的 now-playing 一行，
// 存成 NowPlaying（純 C++，不依賴 Arduino / ESP-IDF，可在 host 端測試）。
//
// 一行格式（見 marvin_voice_core/icy_meta.py）：
//   M1\x1ftrack_id\x1fdur_ms\x1fpos_ms\x1fstate\x1ftitle\x1fartist\x1falbum
#ifndef NOW_PLAYING_H
#define NOW_PLAYING_H

#include <stdint.h>
#include <string.h>
#include <stdlib.h>

struct NowPlaying {
  bool     valid = false;   // 收過至少一筆
  uint8_t  track_id[8] = {0};
  uint32_t dur_ms = 0, pos_ms = 0;
  char     state = 'S';
  char     title[121] = {0}, artist[81] = {0}, album[121] = {0};
  uint32_t rx_ms = 0;        // 收到這筆時的 millis()，由呼叫端填
};

namespace np_detail {

// 1 個 hex 字元→0..15；非 hex 回 -1。
inline int hex_val(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// 16 個 hex 字元 → 8 bytes。全部合法才回 true。
inline bool hex16_to_bytes(const char* s, size_t len, uint8_t out[8]) {
  if (len != 16) return false;
  for (int i = 0; i < 8; i++) {
    int hi = hex_val(s[i * 2]);
    int lo = hex_val(s[i * 2 + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = (uint8_t)((hi << 4) | lo);
  }
  return true;
}

// 把一段 [start,end) 複製進固定容量陣列（含 '\0'），超長截斷、不溢位。
inline void copy_field(const char* start, const char* end, char* dst, size_t cap) {
  size_t n = (size_t)(end - start);
  if (n > cap - 1) n = cap - 1;
  memcpy(dst, start, n);
  dst[n] = '\0';
}

}  // namespace np_detail

// 解析一行（不含 '\n'）。欄位數不是 8、開頭不是 "M1"、track_id 非空且不是 16 個
// hex → 回 false 且不動 out。
inline bool np_parse(const char* line, size_t len, NowPlaying& out) {
  // 依 '\x1f' 切成最多 8 段；超過 8 段也視為格式錯誤。
  const char* field_start[8];
  const char* field_end[8];
  int n_fields = 0;
  const char* p = line;
  const char* seg_start = line;
  const char* end = line + len;
  while (true) {
    if (p == end || *p == '\x1f') {
      if (n_fields >= 8) return false;  // 超過 8 段
      field_start[n_fields] = seg_start;
      field_end[n_fields] = p;
      n_fields++;
      if (p == end) break;
      seg_start = p + 1;
    }
    p++;
  }
  if (n_fields != 8) return false;

  if (!(field_end[0] - field_start[0] == 2 && field_start[0][0] == 'M' && field_start[0][1] == '1'))
    return false;

  uint8_t track_id[8];
  size_t track_len = (size_t)(field_end[1] - field_start[1]);
  if (track_len == 0) {
    memset(track_id, 0xFF, 8);
  } else if (!np_detail::hex16_to_bytes(field_start[1], track_len, track_id)) {
    return false;
  }

  char dur_buf[16], pos_buf[16];
  np_detail::copy_field(field_start[2], field_end[2], dur_buf, sizeof(dur_buf));
  np_detail::copy_field(field_start[3], field_end[3], pos_buf, sizeof(pos_buf));
  uint32_t dur_ms = (uint32_t)strtoul(dur_buf, nullptr, 10);
  uint32_t pos_ms = (uint32_t)strtoul(pos_buf, nullptr, 10);

  if (field_end[4] - field_start[4] != 1) return false;
  char state = field_start[4][0];

  // 全部解析成功才動 out（部分失敗不能留下半套狀態）。
  memcpy(out.track_id, track_id, 8);
  out.dur_ms = dur_ms;
  out.pos_ms = pos_ms;
  out.state = state;
  np_detail::copy_field(field_start[5], field_end[5], out.title, sizeof(out.title));
  np_detail::copy_field(field_start[6], field_end[6], out.artist, sizeof(out.artist));
  np_detail::copy_field(field_start[7], field_end[7], out.album, sizeof(out.album));
  out.valid = true;
  return true;
}

// 播放中：pos_ms + (now_ms - rx_ms)，夾到 dur_ms（dur_ms==0 不夾）；非播放中：pos_ms。
// uint32_t 減法天然處理 millis() 溢位。
inline uint32_t np_position_now(const NowPlaying& np, uint32_t now_ms) {
  if (np.state != 'P') return np.pos_ms;
  uint32_t elapsed = now_ms - np.rx_ms;
  uint32_t pos = np.pos_ms + elapsed;
  if (np.dur_ms != 0 && pos > np.dur_ms) pos = np.dur_ms;
  return pos;
}

inline bool np_same_track(const NowPlaying& a, const NowPlaying& b) {
  return memcmp(a.track_id, b.track_id, 8) == 0;
}

// AVRCP 1.3 TRACK_CHANGED identifier（我們的 TG 沒宣告 browsing，Identifier 只有兩種合法
// 值）：有曲目（np.valid 且 title 非空）→ 8 bytes 全 0x00；否則（尚未選曲）→ 全 0xFF。
inline void np_track_uid(const NowPlaying& np, uint8_t out[8]) {
  bool has_title = np.valid && np.title[0];
  memset(out, has_title ? 0x00 : 0xFF, 8);
}

#endif  // NOW_PLAYING_H
