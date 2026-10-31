// test_chime.cpp — chime.h 的 host 端黑箱測試。
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined,address -fno-sanitize-recover=all \
//     -o /tmp/test_chime firmware/car_puck/test/test_chime.cpp && /tmp/test_chime

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

#include "../chime.h"

int main() {
  const int16_t amp = 3000;

  // T1: 總長 3200 樣本
  {
    int16_t buf[4000];
    size_t n = chime_render(buf, sizeof(buf) / sizeof(buf[0]), amp);
    assert(n == 3200);
  }

  // T2: 全部 |x| <= amp
  {
    int16_t buf[3200];
    size_t n = chime_render(buf, 3200, amp);
    for (size_t i = 0; i < n; i++) assert(abs(buf[i]) <= amp);
  }

  // T3: 第 0 個樣本與最後一個樣本 |x| <= amp/10（有 envelope、無爆音）
  {
    int16_t buf[3200];
    size_t n = chime_render(buf, 3200, amp);
    assert(abs(buf[0]) <= amp / 10);
    assert(abs(buf[n - 1]) <= amp / 10);
  }

  // T4: 兩音之間的靜音段（tone1 90ms=1440 樣本 結束後、tone2 開始前的 20ms=320 樣本）
  // 全為 0。結構：[0,1440)=tone1 [1440,1760)=靜音 [1760,3200)=tone2。
  {
    int16_t buf[3200];
    chime_render(buf, 3200, amp);
    for (int i = 1440; i < 1760; i++) assert(buf[i] == 0);
  }

  // T5: cap 不夠就截斷，回傳值等於 cap，且不越界寫
  {
    int16_t buf[100];
    size_t n = chime_render(buf, 100, amp);
    assert(n == 100);
  }

  printf("test_chime: all passed\n");
  return 0;
}
