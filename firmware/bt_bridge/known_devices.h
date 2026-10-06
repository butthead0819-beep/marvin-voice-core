// known_devices.h — bt_bridge 已配對裝置清單（純 C++，不依賴 Arduino / ESP-IDF，可在 host 端測試）
//
// addr[0] 永遠是最近連上的裝置；失敗時依游標輪替到下一台。
#ifndef KNOWN_DEVICES_H
#define KNOWN_DEVICES_H

#include <stdint.h>
#include <string.h>

static const int KNOWN_MAX = 4;
struct KnownDevices {
  uint8_t addr[KNOWN_MAX][6];
  uint8_t count = 0;      // 0..KNOWN_MAX，addr[0] = 最近連上的
  uint8_t cursor = 0;     // 輪替游標，指向「下一次失敗後要換去的」索引基準
};

inline int known_find(const KnownDevices& k, const uint8_t a[6]) {
  for (int i = 0; i < k.count; i++) {
    if (memcmp(k.addr[i], a, 6) == 0) return i;
  }
  return -1;
}

// 連上時呼叫：已存在 → 移到最前；不存在 → 插到最前，滿了丟掉最後一個。
inline void known_touch(KnownDevices& k, const uint8_t a[6]) {
  int i = known_find(k, a);
  if (i < 0) {
    if (k.count < KNOWN_MAX) k.count++;
    i = k.count - 1;
  }
  for (int j = i; j > 0; j--) memcpy(k.addr[j], k.addr[j - 1], 6);
  memcpy(k.addr[0], a, 6);
  k.cursor = 0;
}

// 一次 page 失敗後，回傳要換去的下一台。
inline bool known_next_after_fail(KnownDevices& k, const uint8_t current[6], uint8_t out[6]) {
  if (k.count == 0) return false;
  if (k.count == 1) {
    memcpy(out, k.addr[0], 6);
    return true;
  }
  int i = known_find(k, current);
  if (i < 0) i = k.cursor;
  int next = (i + 1) % k.count;
  k.cursor = next;
  memcpy(out, k.addr[next], 6);
  return true;
}

inline bool addr_is_zero(const uint8_t a[6]) {
  for (int i = 0; i < 6; i++) {
    if (a[i] != 0) return false;
  }
  return true;
}

// 格式：[count][addr0..addrN-1]，共 1+6*count bytes。cap 不夠回 0。
inline size_t known_serialize(const KnownDevices& k, uint8_t* buf, size_t cap) {
  size_t need = 1 + 6 * (size_t)k.count;
  if (cap < need) return 0;
  buf[0] = k.count;
  for (int i = 0; i < k.count; i++) memcpy(buf + 1 + 6 * i, k.addr[i], 6);
  return need;
}

// 解析失敗（count 超界或長度不符）→ k 清空回 false。
inline bool known_deserialize(KnownDevices& k, const uint8_t* buf, size_t len) {
  if (len < 1 || buf[0] > KNOWN_MAX || len != 1 + 6 * (size_t)buf[0]) {
    memset(k.addr, 0, sizeof(k.addr));
    k.count = 0;
    k.cursor = 0;
    return false;
  }
  k.count = buf[0];
  for (int i = 0; i < k.count; i++) memcpy(k.addr[i], buf + 1 + 6 * i, 6);
  k.cursor = 0;
  return true;
}

#endif  // KNOWN_DEVICES_H
