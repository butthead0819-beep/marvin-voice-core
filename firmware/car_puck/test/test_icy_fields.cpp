// test_icy_fields.cpp — icy_fields.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o /tmp/test_icy_fields firmware/car_puck/test/test_icy_fields.cpp && /tmp/test_icy_fields
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <string>
#include "../icy_fields.h"

static const uint8_t* U(const char* s) { return (const uint8_t*)s; }

int main() {
  // T1: 8 欄原樣（只有 7 個分隔字元，不足 8 個，回 len）
  {
    std::string s = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album";
    size_t n = icy_prefix_len(U(s.data()), s.size(), 8);
    assert(n == s.size());
    printf("T1 ok\n");
  }

  // T2: 9 欄砍成 8 欄——比對位元組內容等於前 8 欄字串
  {
    std::string first8 = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album";
    std::string s = first8 + "\x1f""120";
    size_t n = icy_prefix_len(U(s.data()), s.size(), 8);
    assert(n == first8.size());
    assert(memcmp(s.data(), first8.data(), n) == 0);
    printf("T2 ok\n");
  }

  // T3: 少於 8 欄回 len
  {
    std::string s = "M1\x1f""abc\x1f""1000";
    size_t n = icy_prefix_len(U(s.data()), s.size(), 8);
    assert(n == s.size());
    printf("T3 ok\n");
  }

  // T4: 10 欄只取前 8（多餘欄位一併砍掉）
  {
    std::string first8 = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album";
    std::string s = first8 + "\x1f""120\x1f""extra";
    size_t n = icy_prefix_len(U(s.data()), s.size(), 8);
    assert(n == first8.size());
    printf("T4 ok\n");
  }

  // T5: len=0
  {
    size_t n = icy_prefix_len(U(""), 0, 8);
    assert(n == 0);
    printf("T5 ok\n");
  }

  // ---- icy_bpm_field ----
  auto bpm9 = [](const char* bpmField) {
    std::string first8 = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album";
    std::string s = first8 + "\x1f" + bpmField;
    return icy_bpm_field(U(s.data()), s.size());
  };

  // T6: 第 9 欄 "120" -> 120
  { assert(bpm9("120") == 120); printf("T6 ok\n"); }

  // T7: 空 -> 0
  { assert(bpm9("") == 0); printf("T7 ok\n"); }

  // T8: "12a" -> 0（含非數字）
  { assert(bpm9("12a") == 0); printf("T8 ok\n"); }

  // T9: "0" -> 0
  { assert(bpm9("0") == 0); printf("T9 ok\n"); }

  // T10: "301" -> 0（超過上限）
  { assert(bpm9("301") == 0); printf("T10 ok\n"); }

  // T11: 10 欄只取第 9 欄（第 9 欄後面還有更多欄位）
  {
    std::string first8 = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album";
    std::string s = first8 + "\x1f""128\x1f""extra";
    int v = icy_bpm_field(U(s.data()), s.size());
    assert(v == 128);
    printf("T11 ok\n");
  }

  // T12: len=0
  { assert(icy_bpm_field(U(""), 0) == 0); printf("T12 ok\n"); }

  // T13: 缺第 9 欄（只有 8 欄）-> 0
  {
    std::string s = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album";
    assert(icy_bpm_field(U(s.data()), s.size()) == 0);
    printf("T13 ok\n");
  }

  printf("all icy_fields tests passed\n");
  return 0;
}
