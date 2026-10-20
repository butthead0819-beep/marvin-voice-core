// test_cover_fetch.cpp — cover_fetch.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o /tmp/test_cover_fetch firmware/car_puck/test/test_cover_fetch.cpp && /tmp/test_cover_fetch
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../cover_fetch.h"

int main() {
  // ---- cover_jpeg_valid ----
  {
    const uint8_t p[] = {0xFF, 0xD8, 0x00, 0xFF, 0xD9};
    assert(cover_jpeg_valid(p, sizeof(p)) == true);
    printf("T1 ok\n");
  }
  {
    const uint8_t p[] = {0xFF, 0xD8, 0xFF, 0xD9};
    assert(cover_jpeg_valid(p, sizeof(p)) == true);
    printf("T2 ok\n");
  }
  {
    const uint8_t p[] = {0xFF, 0xD8, 0xD9};
    assert(cover_jpeg_valid(p, sizeof(p)) == false);
    printf("T3 ok\n");
  }
  {
    const uint8_t p[] = {0xFF, 0xD9, 0x00, 0xFF, 0xD9};
    assert(cover_jpeg_valid(p, sizeof(p)) == false);
    printf("T4 ok\n");
  }
  {
    const uint8_t p[] = {0xFF, 0xD8, 0x00, 0xFF, 0xD8};
    assert(cover_jpeg_valid(p, sizeof(p)) == false);
    printf("T5 ok\n");
  }
  {
    assert(cover_jpeg_valid(nullptr, 0) == false);
    printf("T6 ok\n");
  }

  // ---- http_status_code ----
  { assert(http_status_code("HTTP/1.1 200 OK") == 200); printf("T7 ok\n"); }
  { assert(http_status_code("HTTP/1.1 404 Not Found") == 404); printf("T8 ok\n"); }
  { assert(http_status_code("HTTP/1.1 200") == 200); printf("T9 ok\n"); }
  { assert(http_status_code("garbage") == 0); printf("T10 ok\n"); }
  { assert(http_status_code("HTTP/1.1 2x0 OK") == 0); printf("T11 ok\n"); }
  { assert(http_status_code("") == 0); printf("T12 ok\n"); }

  // ---- http_content_length ----
  { assert(http_content_length("Content-Length: 12345") == 12345); printf("T13 ok\n"); }
  { assert(http_content_length("content-length:7") == 7); printf("T14 ok\n"); }
  { assert(http_content_length("CONTENT-LENGTH:   42\r") == 42); printf("T15 ok\n"); }
  { assert(http_content_length("Content-Type: image/jpeg") == -1); printf("T16 ok\n"); }
  { assert(http_content_length("Content-Length: ") == -1); printf("T17 ok\n"); }
  { assert(http_content_length("Content-Length: 12a") == -1); printf("T18 ok\n"); }
  { assert(http_content_length("Content-Lengthx: 5") == -1); printf("T19 ok\n"); }

  // ---- cover_sum_format ----
  {
    CoverStats s = {3, 1, 2, 0, 12000, 850, 120, 1500, 'F', 91000, 60000};
    char buf[160];
    cover_sum_format(buf, sizeof(buf), s);
    assert(strcmp(buf, "ok=3 none=1 fail=2 busy=0 last=F/12000B/850ms wait=120ms max=1500ms ifree=91000 imin=60000") == 0);
    printf("T20 ok\n");
  }
  {
    CoverStats s = {3, 1, 2, 0, 12000, 850, 120, 1500, 'F', 91000, 60000};
    char buf[10];
    cover_sum_format(buf, sizeof(buf), s);
    assert(strlen(buf) < 10);
    assert(buf[strlen(buf)] == '\0');
    printf("T21 ok\n");
  }

  printf("all cover_fetch tests passed\n");
  return 0;
}
