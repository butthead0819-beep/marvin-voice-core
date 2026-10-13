// icy_demux.h — 車機顯示曲名第一刀：S3 從 /audio_stream 的 MP3 byte stream 裡拆出 ICY
// in-band metadata（純 C++，不 include Arduino，host 可測）。
//
// 規格見 marvin_voice_core/icy_meta.py：每 metaint bytes 音訊後跟 1 byte 長度 L，
// 接 L*16 bytes metadata（payload 後補 '\0'）；L=0＝空區塊（內容沒變，不重送）。
#ifndef ICY_DEMUX_H
#define ICY_DEMUX_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

static const size_t ICY_META_MAX = 255 * 16;

struct IcyDemux {
  uint32_t metaint;        // 0 = 不拆（伺服器沒回 icy-metaint），全部當音訊
  uint32_t until_meta;     // 距離下一個 metadata 區塊還剩幾 bytes 音訊
  int      state;          // 0=音訊 1=等長度byte 2=讀metadata
  uint32_t meta_len, meta_got;
  uint8_t  meta[ICY_META_MAX + 1];
};

inline void icy_init(IcyDemux& d, uint32_t metaint) {
  d.metaint = metaint;
  d.until_meta = metaint;
  d.state = 0;
  d.meta_len = 0;
  d.meta_got = 0;
}

inline size_t icy_feed(IcyDemux& d, const uint8_t* in, size_t n, uint8_t* audio_out,
                        void (*on_meta)(const uint8_t*, size_t, void*), void* ctx) {
  if (d.metaint == 0) {
    if (n > 0) memcpy(audio_out, in, n);
    return n;
  }
  size_t out_n = 0;
  for (size_t i = 0; i < n; i++) {
    uint8_t b = in[i];
    if (d.state == 0) {
      audio_out[out_n++] = b;
      d.until_meta--;
      if (d.until_meta == 0) {
        d.state = 1;
      }
    } else if (d.state == 1) {
      d.meta_len = (uint32_t)b * 16;
      d.meta_got = 0;
      if (d.meta_len == 0) {
        d.state = 0;
        d.until_meta = d.metaint;
      } else {
        d.state = 2;
      }
    } else {  // state == 2：讀 metadata
      if (d.meta_got < ICY_META_MAX) {
        d.meta[d.meta_got] = b;
      }
      d.meta_got++;
      if (d.meta_got >= d.meta_len) {
        // 去掉尾端 '\0' 補位，找出真正的 payload 長度。
        size_t cap = d.meta_got <= ICY_META_MAX ? d.meta_got : ICY_META_MAX;
        size_t payload_len = cap;
        while (payload_len > 0 && d.meta[payload_len - 1] == '\0') payload_len--;
        if (payload_len > 0 && on_meta != nullptr) {
          on_meta(d.meta, payload_len, ctx);
        }
        d.state = 0;
        d.until_meta = d.metaint;
      }
    }
  }
  return out_n;
}

#endif  // ICY_DEMUX_H
