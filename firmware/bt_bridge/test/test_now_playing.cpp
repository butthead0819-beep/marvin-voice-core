// test_now_playing.cpp — now_playing.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o test_now_playing firmware/bt_bridge/test/test_now_playing.cpp && ./test_now_playing
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算，只用固定期望值。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <string>
#include "../now_playing.h"

static void feed(const char* line, NowPlaying& out, bool& ok) {
  ok = np_parse(line, strlen(line), out);
}

int main() {
  // T1：正常行
  {
    NowPlaying np;
    bool ok;
    feed("M1\x1f" "0123456789abcdef" "\x1f" "200000" "\x1f" "1000" "\x1f" "P" "\x1f"
         "Song" "\x1f" "Artist" "\x1f" "Album", np, ok);
    assert(ok);
    assert(np.valid);
    uint8_t expect_id[8] = {0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef};
    assert(memcmp(np.track_id, expect_id, 8) == 0);
    assert(np.dur_ms == 200000);
    assert(np.pos_ms == 1000);
    assert(np.state == 'P');
    assert(strcmp(np.title, "Song") == 0);
    assert(strcmp(np.artist, "Artist") == 0);
    assert(strcmp(np.album, "Album") == 0);
    printf("T1 ok\n");
  }

  // T2：中文 UTF-8
  {
    NowPlaying np;
    bool ok;
    feed("M1\x1f" "aaaaaaaaaaaaaaaa" "\x1f" "0" "\x1f" "0" "\x1f" "P" "\x1f"
         "歌曲名" "\x1f" "歌手" "\x1f" "", np, ok);
    assert(ok);
    assert(strcmp(np.title, "歌曲名") == 0);
    assert(strcmp(np.artist, "歌手") == 0);
    assert(strcmp(np.album, "") == 0);
    printf("T2 ok\n");
  }

  // T3：欄位數錯（少一個）
  {
    NowPlaying np;
    bool ok;
    feed("M1\x1f" "aaaaaaaaaaaaaaaa" "\x1f" "0" "\x1f" "0" "\x1f" "P" "\x1f" "T" "\x1f" "A", np, ok);
    assert(!ok);
    assert(!np.valid);
    printf("T3 ok\n");
  }

  // T3b：欄位數錯（多一個）
  {
    NowPlaying np;
    bool ok;
    feed("M1\x1f" "aaaaaaaaaaaaaaaa" "\x1f" "0" "\x1f" "0" "\x1f" "P" "\x1f" "T" "\x1f" "A" "\x1f" "B" "\x1f" "C",
         np, ok);
    assert(!ok);
    printf("T3b ok\n");
  }

  // T4：開頭不是 M1
  {
    NowPlaying np;
    bool ok;
    feed("X1\x1f" "aaaaaaaaaaaaaaaa" "\x1f" "0" "\x1f" "0" "\x1f" "P" "\x1f" "T" "\x1f" "A" "\x1f" "B", np, ok);
    assert(!ok);
    printf("T4 ok\n");
  }

  // T5：track_id 非 hex
  {
    NowPlaying np;
    bool ok;
    feed("M1\x1f" "zzzzzzzzzzzzzzzz" "\x1f" "0" "\x1f" "0" "\x1f" "P" "\x1f" "T" "\x1f" "A" "\x1f" "B", np, ok);
    assert(!ok);
    printf("T5 ok\n");
  }

  // T5b：track_id 長度錯（15 個字元）
  {
    NowPlaying np;
    bool ok;
    feed("M1\x1f" "aaaaaaaaaaaaaaa" "\x1f" "0" "\x1f" "0" "\x1f" "P" "\x1f" "T" "\x1f" "A" "\x1f" "B", np, ok);
    assert(!ok);
    printf("T5b ok\n");
  }

  // T6：空 track_id → 全 0xFF
  {
    NowPlaying np;
    bool ok;
    feed("M1\x1f" "\x1f" "0" "\x1f" "0" "\x1f" "S" "\x1f" "" "\x1f" "" "\x1f" "", np, ok);
    assert(ok);
    uint8_t expect_ff[8];
    memset(expect_ff, 0xFF, 8);
    assert(memcmp(np.track_id, expect_ff, 8) == 0);
    printf("T6 ok\n");
  }

  // T7：字串欄位超長截斷不溢位（title 容量 121，給 300 個 'a'）
  {
    NowPlaying np;
    bool ok;
    std::string long_title(300, 'a');
    std::string long_artist(300, 'b');
    std::string long_album(300, 'c');
    std::string line = std::string("M1\x1f") + "aaaaaaaaaaaaaaaa" + "\x1f" + "0" + "\x1f" + "0" + "\x1f" + "P" +
                        "\x1f" + long_title + "\x1f" + long_artist + "\x1f" + long_album;
    ok = np_parse(line.c_str(), line.size(), np);
    assert(ok);
    assert(strlen(np.title) == sizeof(np.title) - 1);
    assert(strlen(np.artist) == sizeof(np.artist) - 1);
    assert(strlen(np.album) == sizeof(np.album) - 1);
    printf("T7 ok\n");
  }

  // ---------- np_position_now ----------

  // T8：播放中前進
  {
    NowPlaying np;
    np.state = 'P';
    np.pos_ms = 1000;
    np.dur_ms = 200000;
    np.rx_ms = 1000;
    assert(np_position_now(np, 3000) == 3000);  // 1000 + (3000-1000)
    printf("T8 ok\n");
  }

  // T9：夾到 dur_ms
  {
    NowPlaying np;
    np.state = 'P';
    np.pos_ms = 199000;
    np.dur_ms = 200000;
    np.rx_ms = 0;
    assert(np_position_now(np, 50000) == 200000);
    printf("T9 ok\n");
  }

  // T10：停止不前進
  {
    NowPlaying np;
    np.state = 'S';
    np.pos_ms = 5000;
    np.dur_ms = 200000;
    np.rx_ms = 1000;
    assert(np_position_now(np, 999999) == 5000);
    printf("T10 ok\n");
  }

  // T11：millis 溢位
  {
    NowPlaying np;
    np.state = 'P';
    np.pos_ms = 1000;
    np.dur_ms = 0;  // 不夾
    np.rx_ms = 0xFFFFFFF0u;
    uint32_t now_ms = 0x10u;  // 溢位後回繞
    uint32_t elapsed = now_ms - np.rx_ms;  // uint32_t 減法天然處理：0x20
    assert(elapsed == 0x20u);
    assert(np_position_now(np, now_ms) == np.pos_ms + elapsed);
    printf("T11 ok\n");
  }

  // ---------- np_same_track ----------

  // T12：同 track_id → true；不同 → false
  {
    NowPlaying a, b;
    memset(a.track_id, 0x01, 8);
    memset(b.track_id, 0x01, 8);
    assert(np_same_track(a, b));
    b.track_id[7] = 0x02;
    assert(!np_same_track(a, b));
    printf("T12 ok\n");
  }

  printf("all now_playing tests passed\n");
  return 0;
}
