// test_known_devices.cpp — known_devices.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o test_kd firmware/bt_bridge/test/test_known_devices.cpp && ./test_kd
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../known_devices.h"

// 用一個位元組區分裝置：只有最後一碼是 v，其餘為 0
static void mk(uint8_t out[6], uint8_t v) {
  memset(out, 0, 6);
  out[5] = v;
}

// K1 touch 新裝置插到前面；重複 touch 已存在者移到前面且 count 不變
static void test_touch_order() {
  KnownDevices k;
  uint8_t A[6], B[6];
  mk(A, 1);
  mk(B, 2);

  known_touch(k, A);
  assert(k.count == 1 && known_find(k, A) == 0);

  known_touch(k, B);
  assert(k.count == 2 && known_find(k, B) == 0 && known_find(k, A) == 1);

  known_touch(k, A);
  assert(k.count == 2 && known_find(k, A) == 0 && known_find(k, B) == 1);
}

// K2 滿 4 台再 touch 第 5 台 → count==4、最舊的被丟、新的在 [0]
static void test_touch_evicts_oldest() {
  KnownDevices k;
  uint8_t d[5][6];
  for (int i = 0; i < 5; i++) mk(d[i], (uint8_t)(i + 1));

  for (int i = 0; i < 4; i++) known_touch(k, d[i]);  // 順序 4,3,2,1 → [0..3] = d3,d2,d1,d0
  assert(k.count == 4);

  known_touch(k, d[4]);
  assert(k.count == 4);
  assert(known_find(k, d[4]) == 0);
  assert(known_find(k, d[0]) == -1);  // 最舊（最早 touch）被丟
  for (int i = 1; i < 4; i++) assert(known_find(k, d[i]) >= 0);
}

// K3 輪替（count=2：A 在 [0]、B 在 [1]）
static void test_rotation_two() {
  KnownDevices k;
  uint8_t A[6], B[6], out[6];
  mk(A, 1);
  mk(B, 2);
  known_touch(k, B);
  known_touch(k, A);  // [A, B]

  assert(known_next_after_fail(k, A, out) && memcmp(out, B, 6) == 0);
  assert(known_next_after_fail(k, B, out) && memcmp(out, A, 6) == 0);

  // 連續失敗：每次把剛失敗的那台當 current，依序應為 B,A,B,A
  KnownDevices k2;
  known_touch(k2, B);
  known_touch(k2, A);
  uint8_t cur[6];
  memcpy(cur, A, 6);
  uint8_t expect[4][6];
  memcpy(expect[0], B, 6);
  memcpy(expect[1], A, 6);
  memcpy(expect[2], B, 6);
  memcpy(expect[3], A, 6);
  for (int i = 0; i < 4; i++) {
    assert(known_next_after_fail(k2, cur, out));
    assert(memcmp(out, expect[i], 6) == 0);
    memcpy(cur, out, 6);
  }
}

// K4 current 不在清單（全 0）→ 依 cursor 輪替，不卡同一台，會走遍所有裝置
static void test_rotation_unknown_current() {
  KnownDevices k;
  uint8_t d[3][6], zero[6] = {0, 0, 0, 0, 0, 0}, out[6];
  for (int i = 0; i < 3; i++) mk(d[i], (uint8_t)(i + 1));
  for (int i = 0; i < 3; i++) known_touch(k, d[i]);  // [d2, d1, d0]

  bool seen[3] = {false, false, false};
  uint8_t prev[6];
  memset(prev, 0xFF, 6);  // 哨兵：第一次不可能跟它相同
  for (int call = 0; call < 6; call++) {
    assert(known_next_after_fail(k, zero, out));
    assert(memcmp(out, prev, 6) != 0);  // 連續兩次不同台
    memcpy(prev, out, 6);
    for (int i = 0; i < 3; i++) {
      if (memcmp(out, d[i], 6) == 0) seen[i] = true;
    }
  }
  assert(seen[0] && seen[1] && seen[2]);
}

// K5 count==0 回 false；count==1 永遠回同一台
static void test_rotation_edge_counts() {
  KnownDevices empty;
  uint8_t A[6], B[6], out[6];
  mk(A, 1);
  mk(B, 2);
  assert(!known_next_after_fail(empty, A, out));

  KnownDevices one;
  known_touch(one, A);
  for (int i = 0; i < 3; i++) {
    assert(known_next_after_fail(one, A, out) && memcmp(out, A, 6) == 0);
    assert(known_next_after_fail(one, B, out) && memcmp(out, A, 6) == 0);
  }
}

// K6 序列化往返 + 長度不符 / count 超界 → false 且 count==0
static void test_serialize_roundtrip() {
  KnownDevices k;
  uint8_t d[3][6];
  for (int i = 0; i < 3; i++) mk(d[i], (uint8_t)(i + 1));
  for (int i = 0; i < 3; i++) known_touch(k, d[i]);

  uint8_t buf[1 + 6 * KNOWN_MAX];
  assert(known_serialize(k, buf, sizeof(buf)) == 19);
  assert(known_serialize(k, buf, 18) == 0);  // cap 不夠

  KnownDevices r;
  assert(known_deserialize(r, buf, 19));
  assert(r.count == k.count);
  for (int i = 0; i < k.count; i++) assert(memcmp(r.addr[i], k.addr[i], 6) == 0);

  // 長度不符 → false 且清空（先放一個有效值進去，確認真的被清掉）
  assert(known_deserialize(r, buf, 19));
  assert(r.count == 3);
  assert(!known_deserialize(r, buf, 18));
  assert(r.count == 0);

  // count 超界（5）→ false 且清空
  uint8_t bad[1 + 6 * 5];
  memset(bad, 0, sizeof(bad));
  bad[0] = 5;
  assert(!known_deserialize(r, bad, sizeof(bad)));
  assert(r.count == 0);

  // 空緩衝 → false
  assert(!known_deserialize(r, buf, 0));
  assert(r.count == 0);
}

int main() {
  test_touch_order();
  test_touch_evicts_oldest();
  test_rotation_two();
  test_rotation_unknown_current();
  test_rotation_edge_counts();
  test_serialize_roundtrip();
  printf("ALL PASS\n");
  return 0;
}
