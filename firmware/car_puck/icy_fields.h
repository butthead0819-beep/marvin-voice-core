// icy_fields.h — ICY payload（見 marvin_voice_core/icy_meta.py）欄位處理，純 C++、不
// include Arduino，host 可測。
//
// WROOM 的 ICY 解析器（firmware/bt_bridge/now_playing.h）規定剛好 8 欄，S3 這邊在
// MARVIN_ICY_BPM 開啟後會收到第 9 欄（bpm），必須在轉送給 WROOM 前砍掉，否則整行被丟棄、
// 車機曲名消失。
#ifndef ICY_FIELDS_H
#define ICY_FIELDS_H

#include <stddef.h>
#include <stdint.h>

// 回傳「前 n_fields 欄」的 byte 長度（不含第 n_fields 個分隔字元 '\x1f' 本身）。
// 欄位數 <= n_fields 時（分隔字元不足 n_fields 個）回傳 len（整段原樣）。
inline size_t icy_prefix_len(const uint8_t* p, size_t len, int n_fields) {
  int seen = 0;
  for (size_t i = 0; i < len; i++) {
    if (p[i] == 0x1f) {
      seen++;
      if (seen == n_fields) return i;
    }
  }
  return len;
}

// 「換歌了沒」比對長度：只看前 8 欄（WROOM 那份），最多 cap bytes。第 9 欄以後（bpm、
// 講話旗標）會頻繁變動，沒歌時 payload 短於 cap，不截掉就會被誤判成換歌、白抓封面。
inline size_t icy_track_cmp_len(const uint8_t* p, size_t len, size_t cap) {
  size_t n = icy_prefix_len(p, len, 8);
  return n < cap ? n : cap;
}

// 取第 9 欄（index 8）解析成十進位整數。缺欄位、空字串、含非數字、值 <= 0 或 > 300 → 回 0。
// 第 9 欄後面如果還有更多欄位，只看第 9 欄本身。
inline int icy_bpm_field(const uint8_t* p, size_t len) {
  size_t field_start = 0, start = (size_t)-1, end = (size_t)-1;
  int field_idx = 0;
  for (size_t i = 0; i <= len; i++) {
    if (i == len || p[i] == 0x1f) {
      if (field_idx == 8) { start = field_start; end = i; break; }
      field_idx++;
      field_start = i + 1;
    }
  }
  if (start == (size_t)-1 || end <= start) return 0;
  int v = 0;
  for (size_t j = start; j < end; j++) {
    if (p[j] < '0' || p[j] > '9') return 0;
    v = v * 10 + (p[j] - '0');
    if (v > 300) return 0;
  }
  return (v > 0 && v <= 300) ? v : 0;
}

// 取第 10 欄（index 9）：剛好是 "1" 回 1、剛好是 "0" 回 0、缺欄位或其他內容回 -1。
inline int icy_speak_field(const uint8_t* p, size_t len) {
  size_t field_start = 0, start = (size_t)-1, end = (size_t)-1;
  int field_idx = 0;
  for (size_t i = 0; i <= len; i++) {
    if (i == len || p[i] == 0x1f) {
      if (field_idx == 9) { start = field_start; end = i; break; }
      field_idx++;
      field_start = i + 1;
    }
  }
  if (start == (size_t)-1 || end <= start) return -1;
  if (end - start != 1) return -1;
  if (p[start] == '1') return 1;
  if (p[start] == '0') return 0;
  return -1;
}

// 第 1–5 欄（M1/track_id/dur_ms/pos_ms/state）解析結果。
struct IcyNow {
  char trackId[17];   // 最多 16 字元 + 結尾 '\0'
  uint32_t durMs;
  uint32_t posMs;
  char state;          // 'P' 或 'S'
};

// 解析 payload 第 1–5 欄到 *out。欄位不足 5 個、magic 不是 "M1"、track_id 超過 16 字元、
// dur_ms/pos_ms 不是純十進位數字、或 state 不是 'P'/'S' → 回 false（*out 內容不保證）。
inline bool icy_now_fields(const uint8_t* p, size_t len, IcyNow* out) {
  size_t starts[6];  // 第 0..4 欄起點 + 第 5 欄起點（= 第 4 欄結尾後一個位置）
  int field = 0;
  starts[0] = 0;
  for (size_t i = 0; i < len && field < 5; i++) {
    if (p[i] == 0x1f) {
      field++;
      starts[field] = i + 1;
    }
  }
  if (field < 5) return false;   // 分隔字元不足 5 個 ⇒ 欄位不足 5 欄
  size_t ends[5];
  for (int f = 0; f < 5; f++) ends[f] = starts[f + 1] - 1;

  size_t magicLen = ends[0] - starts[0];
  if (magicLen != 2 || p[starts[0]] != 'M' || p[starts[0] + 1] != '1') return false;

  size_t idLen = ends[1] - starts[1];
  if (idLen > 16) return false;
  for (size_t i = 0; i < idLen; i++) out->trackId[i] = (char)p[starts[1] + i];
  out->trackId[idLen] = '\0';

  auto parseDec = [&](size_t s, size_t e, uint32_t* v) -> bool {
    if (e <= s) return false;
    uint32_t acc = 0;
    for (size_t i = s; i < e; i++) {
      if (p[i] < '0' || p[i] > '9') return false;
      acc = acc * 10 + (uint32_t)(p[i] - '0');
    }
    *v = acc;
    return true;
  };
  if (!parseDec(starts[2], ends[2], &out->durMs)) return false;
  if (!parseDec(starts[3], ends[3], &out->posMs)) return false;

  size_t stateLen = ends[4] - starts[4];
  if (stateLen != 1) return false;
  uint8_t s = p[starts[4]];
  if (s != 'P' && s != 'S') return false;
  out->state = (char)s;
  return true;
}

// 取第 11 欄（index 10）：這則回覆開頭做的頭部動作，格式必須剛好是
// "<1-4位十進位seq>:<字母><一位數level>"。seq 1–9999，字母 T/Y/N/G/P 對應
// GESTURE_TILT=0,YES=1,NO=2,GLANCE=3,PONDER=4（這裡不 include face_motion.h，直接回整數），
// level 1–3。任何不符（缺欄位、seq 超界、字母不認得、level 不在 1-3、格式不符）→ 回 false。
inline bool icy_gesture_field(const uint8_t* p, size_t len, uint16_t* seq, int* gesture, int* level) {
  size_t field_start = 0, start = (size_t)-1, end = (size_t)-1;
  int field_idx = 0;
  for (size_t i = 0; i <= len; i++) {
    if (i == len || p[i] == 0x1f) {
      if (field_idx == 10) { start = field_start; end = i; break; }
      field_idx++;
      field_start = i + 1;
    }
  }
  if (start == (size_t)-1 || end <= start) return false;

  size_t colon = (size_t)-1;
  for (size_t i = start; i < end; i++) {
    if (p[i] == ':') { colon = i; break; }
  }
  if (colon == (size_t)-1) return false;

  size_t seqLen = colon - start;
  if (seqLen < 1 || seqLen > 4) return false;
  uint32_t seqVal = 0;
  for (size_t i = start; i < colon; i++) {
    if (p[i] < '0' || p[i] > '9') return false;
    seqVal = seqVal * 10 + (uint32_t)(p[i] - '0');
  }
  if (seqVal < 1 || seqVal > 9999) return false;

  if (end - (colon + 1) != 2) return false;
  uint8_t letter = p[colon + 1];
  uint8_t levelCh = p[colon + 2];
  int g;
  switch (letter) {
    case 'T': g = 0; break;
    case 'Y': g = 1; break;
    case 'N': g = 2; break;
    case 'G': g = 3; break;
    case 'P': g = 4; break;
    default: return false;
  }
  if (levelCh < '1' || levelCh > '3') return false;

  *seq = (uint16_t)seqVal;
  *gesture = g;
  *level = levelCh - '0';
  return true;
}

#endif  // ICY_FIELDS_H
