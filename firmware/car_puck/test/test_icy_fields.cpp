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

  // ---- icy_now_fields ----
  // T14: 正常 8 欄
  {
    std::string s = "M1\x1f""abc123\x1f""180000\x1f""5000\x1f""P\x1f""title\x1f""artist\x1f""album";
    IcyNow now{};
    bool ok = icy_now_fields(U(s.data()), s.size(), &now);
    assert(ok);
    assert(strcmp(now.trackId, "abc123") == 0);
    assert(now.durMs == 180000);
    assert(now.posMs == 5000);
    assert(now.state == 'P');
    printf("T14 ok\n");
  }

  // T15: 正常 9 欄（多的 bpm 欄不影響前 5 欄解析）
  {
    std::string s = "M1\x1f""abc123\x1f""180000\x1f""5000\x1f""S\x1f""title\x1f""artist\x1f""album\x1f""128";
    IcyNow now{};
    bool ok = icy_now_fields(U(s.data()), s.size(), &now);
    assert(ok);
    assert(now.state == 'S');
    printf("T15 ok\n");
  }

  // T16: magic 錯
  {
    std::string s = "X1\x1f""abc\x1f""0\x1f""0\x1f""P\x1f""t\x1f""a\x1f""al";
    IcyNow now{};
    assert(!icy_now_fields(U(s.data()), s.size(), &now));
    printf("T16 ok\n");
  }

  // T17: state 'X'
  {
    std::string s = "M1\x1f""abc\x1f""0\x1f""0\x1f""X\x1f""t\x1f""a\x1f""al";
    IcyNow now{};
    assert(!icy_now_fields(U(s.data()), s.size(), &now));
    printf("T17 ok\n");
  }

  // T18: dur 非數字
  {
    std::string s = "M1\x1f""abc\x1f""18a\x1f""0\x1f""P\x1f""t\x1f""a\x1f""al";
    IcyNow now{};
    assert(!icy_now_fields(U(s.data()), s.size(), &now));
    printf("T18 ok\n");
  }

  // T19: 欄位不足（只有 4 欄）
  {
    std::string s = "M1\x1f""abc\x1f""0\x1f""0";
    IcyNow now{};
    assert(!icy_now_fields(U(s.data()), s.size(), &now));
    printf("T19 ok\n");
  }

  // T20: track_id 恰 16 字元
  {
    std::string id(16, 'a');
    std::string s = "M1\x1f" + id + "\x1f""0\x1f""0\x1f""P\x1f""t\x1f""a\x1f""al";
    IcyNow now{};
    bool ok = icy_now_fields(U(s.data()), s.size(), &now);
    assert(ok);
    assert(strcmp(now.trackId, id.c_str()) == 0);
    printf("T20 ok\n");
  }

  // T21: track_id 17 字元（超過上限）
  {
    std::string id(17, 'a');
    std::string s = "M1\x1f" + id + "\x1f""0\x1f""0\x1f""P\x1f""t\x1f""a\x1f""al";
    IcyNow now{};
    assert(!icy_now_fields(U(s.data()), s.size(), &now));
    printf("T21 ok\n");
  }

  // ---- icy_speak_field ----
  auto speak10 = [](const char* speakField) {
    std::string first9 = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album\x1f""120";
    std::string s = first9 + "\x1f" + speakField;
    return icy_speak_field(U(s.data()), s.size());
  };

  // T22: 10 欄 "1" -> 1
  { assert(speak10("1") == 1); printf("T22 ok\n"); }

  // T23: 10 欄 "0" -> 0
  { assert(speak10("0") == 0); printf("T23 ok\n"); }

  // T24: 8 欄（缺第 9/10 欄）-> -1
  {
    std::string s = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album";
    assert(icy_speak_field(U(s.data()), s.size()) == -1);
    printf("T24 ok\n");
  }

  // T25: 9 欄（缺第 10 欄）-> -1
  {
    std::string s = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album\x1f""120";
    assert(icy_speak_field(U(s.data()), s.size()) == -1);
    printf("T25 ok\n");
  }

  // T26: 第 10 欄為空 -> -1
  { assert(speak10("") == -1); printf("T26 ok\n"); }

  // T27: 第 10 欄為 "2" -> -1
  { assert(speak10("2") == -1); printf("T27 ok\n"); }

  // T28: 第 10 欄為 "10" -> -1
  { assert(speak10("10") == -1); printf("T28 ok\n"); }

  // T29: 第 10 欄後面還有欄位，仍只看第 10 欄本身
  {
    std::string first9 = "M1\x1f""abc\x1f""1000\x1f""0\x1f""P\x1f""title\x1f""artist\x1f""album\x1f""120";
    std::string s = first9 + "\x1f""1\x1f""extra";
    assert(icy_speak_field(U(s.data()), s.size()) == 1);
    printf("T29 ok\n");
  }

  // ---- icy_track_cmp_len（換歌判斷只看前 8 欄，最多 cap bytes）----
  // T30: 沒歌時第 10 欄 0/1 翻轉，比對長度與內容都不變（不再誤判成換歌）
  {
    std::string s0 = std::string("M1\x1f\x1f""0\x1f""0\x1fS\x1f\x1f\x1f\x1f\x1f") + "0";
    std::string s1 = std::string("M1\x1f\x1f""0\x1f""0\x1fS\x1f\x1f\x1f\x1f\x1f") + "1";
    size_t n0 = icy_track_cmp_len(U(s0.data()), s0.size(), 19);
    size_t n1 = icy_track_cmp_len(U(s1.data()), s1.size(), 19);
    assert(n0 == n1);
    assert(memcmp(s0.data(), s1.data(), n0) == 0);
    printf("T30 ok\n");
  }
  // T31: 有歌時比對長度封頂在 cap（M1␟ + 16 hex track_id）
  {
    std::string s = "M1\x1f""0123456789abcdef\x1f""1000\x1f""0\x1fP\x1ftitle\x1f""artist\x1f""album\x1f\x1f""1";
    assert(icy_track_cmp_len(U(s.data()), s.size(), 19) == 19);
    printf("T31 ok\n");
  }
  // T32: 舊 8 欄格式、短於 cap → 整段
  {
    std::string s = "M1\x1f\x1f""0\x1f""0\x1fS\x1f\x1f\x1f";
    assert(icy_track_cmp_len(U(s.data()), s.size(), 19) == s.size());
    printf("T32 ok\n");
  }

  printf("all icy_fields tests passed\n");
  return 0;
}
