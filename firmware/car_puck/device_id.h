// device_id.h — 由晶片 MAC 產生裝置 ID，純 C++、不 include Arduino，host 可測。
//
// ESP.getEfuseMac() 回傳的 uint64 是 little-endian：MAC 第 0 byte 在最低位。
// 例：MAC e8:3d:c1:f1:c0:90 → efuse 值 0x90C0F1C13DE8 → "puck-f1c090"（取 MAC 後三個
// byte，小寫 hex）。
#ifndef DEVICE_ID_H
#define DEVICE_ID_H

#include <stdint.h>
#include <stdio.h>

inline void format_device_id(uint64_t efuse_mac, char* out, size_t n) {
  uint8_t b3 = (efuse_mac >> 24) & 0xff;
  uint8_t b4 = (efuse_mac >> 32) & 0xff;
  uint8_t b5 = (efuse_mac >> 40) & 0xff;
  snprintf(out, n, "puck-%02x%02x%02x", b3, b4, b5);
}

#endif  // DEVICE_ID_H
