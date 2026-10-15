// test_pdu_trace.cpp — pdu_trace.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o test_pdu_trace firmware/bt_bridge/test/test_pdu_trace.cpp && ./test_pdu_trace
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../pdu_trace.h"

int main() {
  // S1：新 (src,pdu) 回 true、n+1、cnt=1；同鍵再記回 false、n 不變、st 更新、cnt+1
  {
    PduTrace t;
    bool is_new = pt_record(t, 'B', 0x10, 0x04);
    assert(is_new == true);
    assert(t.n == 1);
    assert(t.e[0].cnt == 1);
    assert(t.e[0].st == 0x04);

    is_new = pt_record(t, 'B', 0x10, 0x00);
    assert(is_new == false);
    assert(t.n == 1);
    assert(t.e[0].st == 0x00);
    assert(t.e[0].cnt == 2);
    printf("S1 ok\n");
  }

  // S2：'B' 與 'V' 同 pdu 是兩筆
  {
    PduTrace t;
    pt_record(t, 'B', 0x20, 0x04);
    pt_record(t, 'V', 0x20, 0x08);
    assert(t.n == 2);
    printf("S2 ok\n");
  }

  // S3：第 9 個不同鍵被忽略回 false、n 停在 8
  {
    PduTrace t;
    for (uint8_t i = 0; i < 8; i++) {
      bool is_new = pt_record(t, 'B', i, 0);
      assert(is_new == true);
    }
    assert(t.n == 8);
    bool is_new = pt_record(t, 'B', 0xAA, 0);
    assert(is_new == false);
    assert(t.n == 8);
    printf("S3 ok\n");
  }

  // S4：cnt 從 0xFFFE 記兩次停在 0xFFFF
  {
    PduTrace t;
    pt_record(t, 'B', 0x10, 0x04);
    t.e[0].cnt = 0xFFFE;
    pt_record(t, 'B', 0x10, 0x04);
    assert(t.e[0].cnt == 0xFFFF);
    pt_record(t, 'B', 0x10, 0x04);
    assert(t.e[0].cnt == 0xFFFF);
    printf("S4 ok\n");
  }

  // S5：pt_set_caps count=4 → caps_seen、caps_n=4、前 4 個 ev 正確
  {
    PduTrace t;
    uint8_t ev[4] = {0x0d, 0x01, 0x02, 0x05};
    pt_set_caps(t, 4, ev);
    assert(t.caps_seen == true);
    assert(t.caps_n == 4);
    assert(t.caps_ev[0] == 0x0d);
    assert(t.caps_ev[1] == 0x01);
    assert(t.caps_ev[2] == 0x02);
    assert(t.caps_ev[3] == 0x05);
    printf("S5 ok\n");
  }

  // S6：count=12 → caps_n=12 但只存 8 個
  {
    PduTrace t;
    uint8_t ev[12] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12};
    pt_set_caps(t, 12, ev);
    assert(t.caps_n == 12);
    for (int i = 0; i < 8; i++) assert(t.caps_ev[i] == ev[i]);
    printf("S6 ok\n");
  }

  // S7：count=0、ev=nullptr 不崩
  {
    PduTrace t;
    pt_set_caps(t, 0, nullptr);
    assert(t.caps_seen == true);
    assert(t.caps_n == 0);
    printf("S7 ok\n");
  }

  // S8：pt_format_entry 輸出精確等於 "T B10 s04"、"T Vff s0a"
  {
    PduEntry e1;
    e1.src = 'B';
    e1.pdu = 0x10;
    e1.st = 0x04;
    char out[32];
    pt_format_entry(e1, out, sizeof(out));
    assert(strcmp(out, "T B10 s04") == 0);

    PduEntry e2;
    e2.src = 'V';
    e2.pdu = 0xff;
    e2.st = 0x0a;
    pt_format_entry(e2, out, sizeof(out));
    assert(strcmp(out, "T Vff s0a") == 0);
    printf("S8 ok\n");
  }

  // S9：pt_format_caps 精確等於 "T caps n4 0d,01,02,05"；count 0 時 "T caps n0"
  {
    PduTrace t;
    uint8_t ev[4] = {0x0d, 0x01, 0x02, 0x05};
    pt_set_caps(t, 4, ev);
    char out[64];
    pt_format_caps(t, out, sizeof(out));
    assert(strcmp(out, "T caps n4 0d,01,02,05") == 0);

    PduTrace t0;
    pt_set_caps(t0, 0, nullptr);
    pt_format_caps(t0, out, sizeof(out));
    assert(strcmp(out, "T caps n0") == 0);
    printf("S9 ok\n");
  }

  // S10：cap=8 時截斷且 '\0' 結尾不溢位
  {
    PduTrace t;
    uint8_t ev[8] = {1, 2, 3, 4, 5, 6, 7, 8};
    pt_set_caps(t, 8, ev);
    char out[8];
    memset(out, 'X', sizeof(out));
    pt_format_caps(t, out, sizeof(out));
    assert(out[7] == '\0');
    size_t len = strlen(out);
    assert(len < 8);
    printf("S10 ok\n");
  }

  printf("ALL OK\n");
  return 0;
}
