// ws_frame.h — 最小 WebSocket client 端 frame 組裝/解析 + base64（純 C++，不 include
// Arduino，host 可測）。只實作 /mic_ws 需要的子集：client→server 一定要 mask，
// server→client 一定不 mask（RFC 6455），不處理 fragmentation（server 端訊息都是單一
// frame，見 car_http_app.py aiohttp 行為）。
#ifndef WS_FRAME_H
#define WS_FRAME_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define WS_OP_CONT 0x0
#define WS_OP_TEXT 0x1
#define WS_OP_BINARY 0x2
#define WS_OP_CLOSE 0x8
#define WS_OP_PING 0x9
#define WS_OP_PONG 0xA

// 組一個 client→server frame（FIN=1、MASK=1）。回傳總長度；cap 不夠回 0。
static inline size_t ws_build_frame(uint8_t opcode, const uint8_t* payload, size_t len,
                                      const uint8_t mask[4], uint8_t* out, size_t cap) {
  size_t hdr_len;
  if (len <= 125) hdr_len = 2;
  else if (len <= 65535) hdr_len = 4;
  else hdr_len = 10;
  size_t total = hdr_len + 4 + len;
  if (total > cap) return 0;

  out[0] = (uint8_t)(0x80 | (opcode & 0x0F));   // FIN=1
  if (len <= 125) {
    out[1] = (uint8_t)(0x80 | len);
  } else if (len <= 65535) {
    out[1] = 0x80 | 126;
    out[2] = (uint8_t)((len >> 8) & 0xFF);
    out[3] = (uint8_t)(len & 0xFF);
  } else {
    out[1] = 0x80 | 127;
    for (int i = 0; i < 8; i++) {
      out[2 + i] = (uint8_t)((len >> (8 * (7 - i))) & 0xFF);
    }
  }
  memcpy(out + hdr_len, mask, 4);
  for (size_t i = 0; i < len; i++) {
    out[hdr_len + 4 + i] = (uint8_t)(payload[i] ^ mask[i % 4]);
  }
  return total;
}

struct WsHeader {
  bool fin;
  uint8_t opcode;
  bool masked;
  uint64_t payload_len;
  uint8_t mask[4];
  size_t header_len;   // 不含 payload——payload 起點 = buf + header_len
};

// 解析 server→client frame header。資料不夠回 0；格式錯（RSV 非 0、控制 frame 長度
// >125 或 FIN=0）回 -1；成功回 1。
static inline int ws_parse_header(const uint8_t* buf, size_t len, WsHeader* out) {
  if (len < 2) return 0;

  uint8_t b0 = buf[0];
  uint8_t b1 = buf[1];
  bool fin = (b0 & 0x80) != 0;
  uint8_t rsv = b0 & 0x70;
  uint8_t opcode = b0 & 0x0F;
  bool masked = (b1 & 0x80) != 0;
  uint8_t len7 = b1 & 0x7F;

  if (rsv != 0) return -1;
  bool is_control = (opcode == WS_OP_CLOSE || opcode == WS_OP_PING || opcode == WS_OP_PONG);
  if (is_control && (!fin || len7 > 125)) return -1;

  size_t pos = 2;
  uint64_t payload_len;
  if (len7 <= 125) {
    payload_len = len7;
  } else if (len7 == 126) {
    if (len < pos + 2) return 0;
    payload_len = ((uint64_t)buf[pos] << 8) | buf[pos + 1];
    pos += 2;
  } else {
    if (len < pos + 8) return 0;
    payload_len = 0;
    for (int i = 0; i < 8; i++) payload_len = (payload_len << 8) | buf[pos + i];
    pos += 8;
  }

  uint8_t mask[4] = {0, 0, 0, 0};
  if (masked) {
    if (len < pos + 4) return 0;
    memcpy(mask, buf + pos, 4);
    pos += 4;
  }

  if (len < pos + payload_len) return 0;

  out->fin = fin;
  out->opcode = opcode;
  out->masked = masked;
  out->payload_len = payload_len;
  memcpy(out->mask, mask, 4);
  out->header_len = pos;
  return 1;
}

static const char WS_B64_ALPHABET[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

// 標準 base64 含 '=' padding，NUL 結尾。cap 不夠回 0；成功回輸出字串長度（不含 NUL）。
static inline size_t ws_base64_encode(const uint8_t* in, size_t len, char* out, size_t cap) {
  size_t out_len = ((len + 2) / 3) * 4;
  if (out_len + 1 > cap) return 0;

  size_t oi = 0;
  size_t i = 0;
  while (i + 3 <= len) {
    uint32_t v = ((uint32_t)in[i] << 16) | ((uint32_t)in[i + 1] << 8) | in[i + 2];
    out[oi++] = WS_B64_ALPHABET[(v >> 18) & 0x3F];
    out[oi++] = WS_B64_ALPHABET[(v >> 12) & 0x3F];
    out[oi++] = WS_B64_ALPHABET[(v >> 6) & 0x3F];
    out[oi++] = WS_B64_ALPHABET[v & 0x3F];
    i += 3;
  }
  size_t rem = len - i;
  if (rem == 1) {
    uint32_t v = (uint32_t)in[i] << 16;
    out[oi++] = WS_B64_ALPHABET[(v >> 18) & 0x3F];
    out[oi++] = WS_B64_ALPHABET[(v >> 12) & 0x3F];
    out[oi++] = '=';
    out[oi++] = '=';
  } else if (rem == 2) {
    uint32_t v = ((uint32_t)in[i] << 16) | ((uint32_t)in[i + 1] << 8);
    out[oi++] = WS_B64_ALPHABET[(v >> 18) & 0x3F];
    out[oi++] = WS_B64_ALPHABET[(v >> 12) & 0x3F];
    out[oi++] = WS_B64_ALPHABET[(v >> 6) & 0x3F];
    out[oi++] = '=';
  }
  out[oi] = '\0';
  return oi;
}

#define MIC_CTL_UNKNOWN 0
#define MIC_CTL_DUCK 1
#define MIC_CTL_UNDUCK 2

// 找 `"key"` 鍵（key 含兩側引號），接著（容忍空白）冒號，再接著（容忍空白）引號
// 字串；回傳值字串內容起點，*out_len 填長度（不含引號）。找不到回 (size_t)-1。
// mic_ctl_parse/mic_face_parse 共用，只在本檔內用。
static inline size_t ws_find_string_value(const char* s, size_t len, const char* key, size_t* out_len) {
  size_t key_len = strlen(key);
  size_t key_pos = (size_t)-1;
  for (size_t i = 0; i + key_len <= len; i++) {
    if (memcmp(s + i, key, key_len) == 0) { key_pos = i; break; }
  }
  if (key_pos == (size_t)-1) return (size_t)-1;

  size_t p = key_pos + key_len;
  while (p < len && (s[p] == ' ' || s[p] == '\t')) p++;
  if (p >= len || s[p] != ':') return (size_t)-1;
  p++;
  while (p < len && (s[p] == ' ' || s[p] == '\t')) p++;
  if (p >= len || s[p] != '"') return (size_t)-1;
  p++;

  size_t start = p;
  while (p < len && s[p] != '"') p++;
  if (p >= len) return (size_t)-1;
  *out_len = p - start;
  return start;
}

// 判斷 /mic_ws 收到的文字 frame 是 duck/unduck 控制訊息：找 `"type"` 鍵的字串值，
// 比對剛好是 "duck"/"unduck"。
static inline int mic_ctl_parse(const char* s, size_t len) {
  size_t vlen;
  size_t vpos = ws_find_string_value(s, len, "\"type\"", &vlen);
  if (vpos == (size_t)-1) return MIC_CTL_UNKNOWN;

  if (vlen == 6 && memcmp(s + vpos, "unduck", 6) == 0) return MIC_CTL_UNDUCK;
  if (vlen == 4 && memcmp(s + vpos, "duck", 4) == 0) return MIC_CTL_DUCK;
  return MIC_CTL_UNKNOWN;
}

#define MIC_FACE_NONE 0
#define MIC_FACE_LISTEN 1
#define MIC_FACE_THINK 2
#define MIC_FACE_SPEAK 3
#define MIC_FACE_IDLE 4

// 判斷 /mic_ws 收到的文字 frame 是臉部狀態訊息：`"type"` 鍵的字串值剛好是 "face"，
// 且 `"state"` 鍵的字串值剛好是 listen/think/speak/idle 之一（鍵順序任意）。其他情況
// 回 MIC_FACE_NONE。
static inline int mic_face_parse(const char* s, size_t len) {
  size_t tlen;
  size_t tpos = ws_find_string_value(s, len, "\"type\"", &tlen);
  if (tpos == (size_t)-1) return MIC_FACE_NONE;
  if (tlen != 4 || memcmp(s + tpos, "face", 4) != 0) return MIC_FACE_NONE;

  size_t slen;
  size_t spos = ws_find_string_value(s, len, "\"state\"", &slen);
  if (spos == (size_t)-1) return MIC_FACE_NONE;

  if (slen == 6 && memcmp(s + spos, "listen", 6) == 0) return MIC_FACE_LISTEN;
  if (slen == 5 && memcmp(s + spos, "think", 5) == 0) return MIC_FACE_THINK;
  if (slen == 5 && memcmp(s + spos, "speak", 5) == 0) return MIC_FACE_SPEAK;
  if (slen == 4 && memcmp(s + spos, "idle", 4) == 0) return MIC_FACE_IDLE;
  return MIC_FACE_NONE;
}

#endif  // WS_FRAME_H
