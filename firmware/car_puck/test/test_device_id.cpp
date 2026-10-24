// test_device_id.cpp — device_id.h 的 host 端黑箱測試。
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o /tmp/test_device_id firmware/car_puck/test/test_device_id.cpp && /tmp/test_device_id

#undef NDEBUG
#include <assert.h>
#include <string.h>

#include "../device_id.h"

int main() {
  // T1: 範例 MAC e8:3d:c1:f1:c0:90 → efuse 0x90C0F1C13DE8 → "puck-f1c090"
  {
    char out[16];
    format_device_id(0x90C0F1C13DE8ULL, out, sizeof(out));
    assert(strcmp(out, "puck-f1c090") == 0);
  }

  // T2: 全 0 → "puck-000000"
  {
    char out[16];
    format_device_id(0ULL, out, sizeof(out));
    assert(strcmp(out, "puck-000000") == 0);
  }

  // T3: 高 16 bit 有雜值不影響結果（只看 bit 24-47）
  {
    char out[16];
    format_device_id(0xFFFF90C0F1C13DE8ULL, out, sizeof(out));
    assert(strcmp(out, "puck-f1c090") == 0);
  }

  // T4: 緩衝區很小也不溢位、字串有 NUL 結尾
  {
    char out[8];
    format_device_id(0x90C0F1C13DE8ULL, out, sizeof(out));
    assert(strlen(out) < sizeof(out));
  }

  printf("test_device_id: all passed\n");
  return 0;
}
