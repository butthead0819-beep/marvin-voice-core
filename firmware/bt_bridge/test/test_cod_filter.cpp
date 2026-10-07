// test_cod_filter.cpp — cod_filter.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o test_cod firmware/bt_bridge/test/test_cod_filter.cpp && ./test_cod
//
// 測資全部取自 10/6–10/7 [SCAN]/[DIAG] 實際掃到的裝置。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include "../cod_filter.h"

int main() {
  // 要接受：車機、藍牙喇叭
  assert(cod_is_audio_sink(0x260408));  // BMW 04900（Audio/Video 免持，服務 Audio+Rendering+Networking）
  assert(cod_is_audio_sink(0x240404));  // Soundcore Mini 3 Pro（Audio/Video，服務 Audio+Rendering）
  assert(cod_is_audio_sink(0x240420));  // CK-BT（Audio/Video）

  // 要拒絕：電腦、手機、其他類別
  assert(!cod_is_audio_sink(0x38010c));  // 黃子嘉的MacBook Air（Computer）
  assert(!cod_is_audio_sink(0x7a020c));  // 黃子嘉的iPhone（Phone，雖然服務含 Audio）
  assert(!cod_is_audio_sink(0x5a020c));  // Muku 的 S26（Phone）
  assert(!cod_is_audio_sink(0x001f00));  // 1564CYM006433（Uncategorized）
  assert(!cod_is_audio_sink(0));         // 沒有 COD

  // Audio/Video 但完全沒宣告 Rendering/Audio 服務 → 拒絕（例如只有麥克風之類）
  assert(!cod_is_audio_sink(0x000404));
  // 車機在配對畫面可能開 Limited Discoverable（bit 13）——不能被誤讀成主類別的一部分
  assert(cod_is_audio_sink(0x260408 | (1u << 13)));
  // 只有 Rendering 或只有 Audio 服務其一 → 接受
  assert(cod_is_audio_sink(0x040404));  // Rendering only
  assert(cod_is_audio_sink(0x200404));  // Audio only

  printf("ALL PASS\n");
  return 0;
}
