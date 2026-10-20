// test_diag_summary.cpp — diag_summary.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o test_diag_summary firmware/bt_bridge/test/test_diag_summary.cpp && ./test_diag_summary
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../diag_summary.h"

int main() {
  // S1：ds_inc 飽和在 0xFFFF，不繞回 0
  {
    uint16_t c = 0xFFFE;
    ds_inc(c);
    assert(c == 0xFFFF);
    ds_inc(c);
    assert(c == 0xFFFF);
    printf("S1 ok\n");
  }

  // S2：ds_add_key 去重，滿 8 個後忽略新 key
  {
    DiagSummary s;
    ds_add_key(s, 0x41);
    ds_add_key(s, 0x42);
    ds_add_key(s, 0x41);  // 重複，不應增加
    assert(s.key_n == 2);
    assert(s.keys[0] == 0x41);
    assert(s.keys[1] == 0x42);

    for (uint8_t k = 0x50; k < 0x50 + 10; k++) {
      ds_add_key(s, k);
    }
    assert(s.key_n == 8);  // 2 個舊的 + 6 個新的填滿，剩下忽略
    assert(s.keys[7] == 0x55);

    // 滿了之後再加任何新 key 都不動
    ds_add_key(s, 0x99);
    assert(s.key_n == 8);
    printf("S2 ok\n");
  }

  // S3：ds_format 對固定輸入產生完整字串
  {
    DiagSummary s;
    s.uptime_s = 123;
    s.meta_lines = 7;
    ds_add_key(s, 0x41);
    ds_add_key(s, 0x42);
    s.peer[0] = 0xAB;
    s.peer[1] = 0xCD;
    s.peer[2] = 0xEF;

    char out[160];
    ds_format(s, out, sizeof(out));
    const char* expect =
        "t=123s m=7 k=41,42 p=ABCDEF";
    assert(strcmp(out, expect) == 0);
    printf("S3 ok\n");
  }

  // S4：ds_format 對空 keys 輸出 k=（空字串）
  {
    DiagSummary s;
    s.uptime_s = 1;
    char out[160];
    ds_format(s, out, sizeof(out));
    assert(strstr(out, "k= p=") != nullptr);
    printf("S4 ok\n");
  }

  // S5：cap 很小時不溢位，仍以 '\0' 結尾
  {
    DiagSummary s;
    s.uptime_s = 999999;
    ds_add_key(s, 0x11);
    char out[10];
    memset(out, 'X', sizeof(out));
    ds_format(s, out, sizeof(out));
    assert(out[9] == '\0');
    size_t len = strlen(out);
    assert(len < 10);
    printf("S5 ok\n");
  }

  printf("ALL OK\n");
  return 0;
}
