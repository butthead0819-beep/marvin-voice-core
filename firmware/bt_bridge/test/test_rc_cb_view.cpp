// test_rc_cb_view.cpp — rc_cb_view.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o test_rc_cb_view firmware/bt_bridge/test/test_rc_cb_view.cpp && ./test_rc_cb_view
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../rc_cb_view.h"

int main() {
  // S1：connected=1、event 2 的 registered=1 label=7、handle=3 → can_notify true，回 handle/label
  {
    uint8_t cb[42] = {0};
    cb[0] = 1;   // rc_connected
    cb[2] = 3;   // rc_handle
    cb[16 + 2] = 1;  // rc_ntf[1].registered（event_id=2）
    cb[16 + 3] = 7;  // rc_ntf[1].label
    uint8_t handle = 0, label = 0;
    bool ok = rcv_can_notify(cb, 2, &handle, &label);
    assert(ok == true);
    assert(handle == 3);
    assert(label == 7);
    printf("S1 ok\n");
  }

  // S2：connected=0 → false
  {
    uint8_t cb[42] = {0};
    cb[0] = 0;
    cb[16 + 2] = 1;
    uint8_t handle = 0, label = 0;
    assert(rcv_can_notify(cb, 2, &handle, &label) == false);
    printf("S2 ok\n");
  }

  // S3：registered=0 → false
  {
    uint8_t cb[42] = {0};
    cb[0] = 1;
    cb[16 + 2] = 0;
    uint8_t handle = 0, label = 0;
    assert(rcv_can_notify(cb, 2, &handle, &label) == false);
    printf("S3 ok\n");
  }

  // S4：registered=2（結構對不上）→ false
  {
    uint8_t cb[42] = {0};
    cb[0] = 1;
    cb[16 + 2] = 2;
    uint8_t handle = 0, label = 0;
    assert(rcv_can_notify(cb, 2, &handle, &label) == false);
    printf("S4 ok\n");
  }

  // S5：event_id 超出範圍（0、14）→ false
  // 陣列刻意開大並在「假如有第 14 格」的位置擺 registered=1：上限沒擋住時會讀到它而回 true，
  // 不能靠讀到陣列外的隨機值碰巧回 false。
  {
    uint8_t cb[64] = {0};
    cb[0] = 1;
    cb[16 + 0] = 1;  // event_id=1 的 registered
    cb[16 + 2 * 13] = 1;  // 若 event_id=14 合法，它的 registered 會在這裡
    uint8_t handle = 0, label = 0;
    assert(rcv_can_notify(cb, 0, &handle, &label) == false);
    assert(rcv_can_notify(cb, 14, &handle, &label) == false);
    printf("S5 ok\n");
  }

  // S6：cb=nullptr → false，不崩
  {
    uint8_t handle = 0, label = 0;
    assert(rcv_can_notify(nullptr, 2, &handle, &label) == false);
    printf("S6 ok\n");
  }

  // S7：rcv_mark_unregistered 後 registered byte 歸零，其餘 byte 不變
  {
    uint8_t cb[42];
    memset(cb, 0xAB, sizeof(cb));
    uint8_t before[42];
    memcpy(before, cb, sizeof(cb));
    rcv_mark_unregistered(cb, 2);
    uint8_t expect[42];
    memcpy(expect, before, sizeof(expect));
    expect[16 + 2] = 0;
    assert(memcmp(cb, expect, sizeof(cb)) == 0);
    printf("S7 ok\n");
  }

  // S8：event_id 不合法（0）或 cb=nullptr 時 rcv_mark_unregistered 不動、不崩
  {
    uint8_t cb[42];
    memset(cb, 0xCD, sizeof(cb));
    uint8_t before[42];
    memcpy(before, cb, sizeof(cb));
    rcv_mark_unregistered(cb, 0);
    rcv_mark_unregistered(cb, 14);
    assert(memcmp(cb, before, sizeof(cb)) == 0);
    rcv_mark_unregistered(nullptr, 2);  // 不崩即可
    printf("S8 ok\n");
  }

  // S9：rcv_evt_supported — 1/2/5/13 true；0/3/4/6/12/14/15/200 false
  {
    assert(rcv_evt_supported(1) == true);
    assert(rcv_evt_supported(2) == true);
    assert(rcv_evt_supported(5) == true);
    assert(rcv_evt_supported(13) == true);
    assert(rcv_evt_supported(0) == false);
    assert(rcv_evt_supported(3) == false);
    assert(rcv_evt_supported(4) == false);
    assert(rcv_evt_supported(6) == false);
    assert(rcv_evt_supported(12) == false);
    assert(rcv_evt_supported(14) == false);
    assert(rcv_evt_supported(15) == false);
    assert(rcv_evt_supported(200) == false);
    printf("S9 ok\n");
  }

  // S10：RCV_SUPPORTED_EVT_MASK == 0x2026
  {
    assert(RCV_SUPPORTED_EVT_MASK == 0x2026);
    printf("S10 ok\n");
  }

  printf("ALL OK\n");
  return 0;
}
