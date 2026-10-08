// test_diag_log.cpp — diag_log.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o test_diag firmware/bt_bridge/test/test_diag_log.cpp && ./test_diag
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../diag_log.h"

static void make_text(char* out, int i) {
  snprintf(out, DIAG_TEXT, "line-%02d", i);
}

int main() {
  // D1：少於 16 條，順序正確（最舊在 0）
  {
    DiagLog d;
    for (int i = 0; i < 5; i++) {
      char t[DIAG_TEXT];
      make_text(t, i);
      diag_add(d, t);
    }
    assert(d.count == 5);
    for (int i = 0; i < 5; i++) {
      char out[DIAG_TEXT];
      assert(diag_get(d, i, out) == 1);
      char expect[DIAG_TEXT];
      make_text(expect, i);
      assert(strcmp(out, expect) == 0);
    }
    char out[DIAG_TEXT];
    assert(diag_get(d, 5, out) == 0);
    assert(diag_get(d, -1, out) == 0);
    printf("D1 ok\n");
  }

  // D2：寫 20 條只剩最新 16 條，最舊是第 5 條（index 4）
  {
    DiagLog d;
    for (int i = 0; i < 20; i++) {
      char t[DIAG_TEXT];
      make_text(t, i);
      diag_add(d, t);
    }
    assert(d.count == DIAG_MAX);
    char out[DIAG_TEXT];
    assert(diag_get(d, 0, out) == 1);
    assert(strcmp(out, "line-04") == 0);
    assert(diag_get(d, DIAG_MAX - 1, out) == 1);
    assert(strcmp(out, "line-19") == 0);
    assert(diag_get(d, DIAG_MAX, out) == 0);
    printf("D2 ok\n");
  }

  // D3：超長字串被截斷，且以 '\0' 結尾
  {
    DiagLog d;
    char longs[200];
    memset(longs, 'x', sizeof(longs) - 1);
    longs[sizeof(longs) - 1] = '\0';
    diag_add(d, longs);
    char out[DIAG_TEXT];
    assert(diag_get(d, 0, out) == 1);
    assert(strlen(out) == DIAG_TEXT - 1);
    assert(out[DIAG_TEXT - 1] == '\0');
    for (int i = 0; i < DIAG_TEXT - 1; i++) assert(out[i] == 'x');
    printf("D3 ok\n");
  }

  // D4：序列化往返相等（含環形繞回後）
  {
    DiagLog d;
    for (int i = 0; i < 23; i++) {  // 繞回過一次
      char t[DIAG_TEXT];
      make_text(t, i);
      diag_add(d, t);
    }
    uint8_t buf[1 + DIAG_TEXT * DIAG_MAX];
    size_t n = diag_serialize(d, buf, sizeof(buf));
    assert(n == 1 + (size_t)DIAG_TEXT * DIAG_MAX);

    DiagLog r;
    assert(diag_deserialize(r, buf, n) == true);
    assert(r.count == d.count);
    for (int i = 0; i < d.count; i++) {
      char a[DIAG_TEXT], b[DIAG_TEXT];
      assert(diag_get(d, i, a) == 1);
      assert(diag_get(r, i, b) == 1);
      assert(strcmp(a, b) == 0);
    }
    // 反序列化後再寫入，最舊的要被覆蓋（head 位置正確）
    char t[DIAG_TEXT];
    make_text(t, 99);
    diag_add(r, t);
    char out[DIAG_TEXT];
    assert(diag_get(r, 0, out) == 1);
    assert(strcmp(out, "line-08") == 0);

    // cap 不夠 → 0
    assert(diag_serialize(d, buf, 10) == 0);
    printf("D4 ok\n");
  }

  // D5：長度錯 / count=17 → false 且 count==0
  {
    DiagLog d;
    uint8_t buf[1 + DIAG_TEXT * DIAG_MAX];
    memset(buf, 0, sizeof(buf));

    buf[0] = 2;  // count=2 但長度只給 1+DIAG_TEXT
    assert(diag_deserialize(d, buf, 1 + DIAG_TEXT) == false);
    assert(d.count == 0);

    buf[0] = DIAG_MAX + 1;  // count=17
    assert(diag_deserialize(d, buf, sizeof(buf)) == false);
    assert(d.count == 0);

    // count=17 且長度剛好吻合 1+DIAG_TEXT*17：只靠長度檢查擋不住，必須靠 count 上限擋（否則寫爆 line[]）
    static uint8_t big[1 + DIAG_TEXT * (DIAG_MAX + 1)];
    memset(big, 'y', sizeof(big));
    big[0] = DIAG_MAX + 1;
    assert(diag_deserialize(d, big, sizeof(big)) == false);
    assert(d.count == 0);

    printf("D5 ok\n");
  }

  // D6：未滿時反序列化後再寫入，新的一條接在最後、舊的都還在（head 要接在 count 後面）
  {
    DiagLog d;
    for (int i = 0; i < 3; i++) {
      char t[DIAG_TEXT];
      make_text(t, i);
      diag_add(d, t);
    }
    uint8_t buf[1 + DIAG_TEXT * DIAG_MAX];
    size_t n = diag_serialize(d, buf, sizeof(buf));
    DiagLog r;
    assert(diag_deserialize(r, buf, n) == true);
    char t[DIAG_TEXT];
    make_text(t, 50);
    diag_add(r, t);
    assert(r.count == 4);
    const char* expect[] = {"line-00", "line-01", "line-02", "line-50"};
    for (int i = 0; i < 4; i++) {
      char out[DIAG_TEXT];
      assert(diag_get(r, i, out) == 1);
      assert(strcmp(out, expect[i]) == 0);
    }
    printf("D6 ok\n");
  }

  // D7：diag_budget_take max=3，前 3 次回 true 且 used 依序變 1/2/3，第 4、5 次回 false 且 used 仍 3
  {
    uint8_t used = 0;
    assert(diag_budget_take(used, 3) == true);
    assert(used == 1);
    assert(diag_budget_take(used, 3) == true);
    assert(used == 2);
    assert(diag_budget_take(used, 3) == true);
    assert(used == 3);
    assert(diag_budget_take(used, 3) == false);
    assert(used == 3);
    assert(diag_budget_take(used, 3) == false);
    assert(used == 3);
    printf("D7 ok\n");
  }

  // D8：diag_budget_take max=0，第一次就回 false，used 仍 0
  {
    uint8_t used = 0;
    assert(diag_budget_take(used, 0) == false);
    assert(used == 0);
    printf("D8 ok\n");
  }

  // D9：diag_add_ts 前綴開機秒數；超長字串截到 DIAG_TEXT-1 並以 "7s x" 開頭
  {
    DiagLog d;
    diag_add_ts(d, 123, "C 064C72");
    char out[DIAG_TEXT];
    assert(diag_get(d, 0, out) == 1);
    assert(strcmp(out, "123s C 064C72") == 0);

    char longs[51];
    memset(longs, 'x', sizeof(longs) - 1);
    longs[sizeof(longs) - 1] = '\0';
    diag_add_ts(d, 7, longs);
    assert(diag_get(d, 1, out) == 1);
    assert(strlen(out) == DIAG_TEXT - 1);
    assert(strncmp(out, "7s x", 4) == 0);
    printf("D9 ok\n");
  }

  printf("ALL PASS\n");
  return 0;
}
