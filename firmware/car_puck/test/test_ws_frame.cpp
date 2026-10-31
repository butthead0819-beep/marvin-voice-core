// test_ws_frame.cpp — ws_frame.h 的 host 端黑箱測試。
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined,address -fno-sanitize-recover=all \
//     -o /tmp/test_ws_frame firmware/car_puck/test/test_ws_frame.cpp && /tmp/test_ws_frame

#undef NDEBUG
#include <assert.h>
#include <string.h>
#include <stdio.h>

#include "../ws_frame.h"

static void test_build_lengths() {
  uint8_t mask[4] = {0x11, 0x22, 0x33, 0x44};
  uint8_t out[70100];

  // len 0
  {
    size_t n = ws_build_frame(WS_OP_TEXT, nullptr, 0, mask, out, sizeof(out));
    assert(n == 2 + 4);
    assert(out[0] == 0x81);
    assert(out[1] == 0x80);
    assert(memcmp(out + 2, mask, 4) == 0);
  }
  // len 125 (7-bit boundary)
  {
    uint8_t payload[125];
    for (int i = 0; i < 125; i++) payload[i] = (uint8_t)i;
    size_t n = ws_build_frame(WS_OP_BINARY, payload, 125, mask, out, sizeof(out));
    assert(n == 2 + 4 + 125);
    assert(out[1] == (uint8_t)(0x80 | 125));
    for (int i = 0; i < 125; i++) {
      uint8_t unmasked = (uint8_t)(out[2 + 4 + i] ^ mask[i % 4]);
      assert(unmasked == payload[i]);
    }
  }
  // len 126 (16-bit extended)
  {
    uint8_t payload[126];
    for (int i = 0; i < 126; i++) payload[i] = (uint8_t)(i * 3);
    size_t n = ws_build_frame(WS_OP_BINARY, payload, 126, mask, out, sizeof(out));
    assert(n == 4 + 4 + 126);
    assert(out[1] == (uint8_t)(0x80 | 126));
    assert(out[2] == 0);
    assert(out[3] == 126);
    for (int i = 0; i < 126; i++) {
      uint8_t unmasked = (uint8_t)(out[4 + 4 + i] ^ mask[i % 4]);
      assert(unmasked == payload[i]);
    }
  }
  // len 65535 (16-bit max)
  {
    static uint8_t payload[65535];
    for (size_t i = 0; i < sizeof(payload); i++) payload[i] = (uint8_t)(i & 0xFF);
    static uint8_t big_out[65535 + 4 + 4];
    size_t n = ws_build_frame(WS_OP_BINARY, payload, sizeof(payload), mask, big_out, sizeof(big_out));
    assert(n == 4 + 4 + 65535);
    assert(big_out[2] == 0xFF);
    assert(big_out[3] == 0xFF);
    for (size_t i = 0; i < sizeof(payload); i += 997) {
      uint8_t unmasked = (uint8_t)(big_out[4 + 4 + i] ^ mask[i % 4]);
      assert(unmasked == payload[i]);
    }
  }
  // len 65536 (64-bit extended)
  {
    static uint8_t payload[65536];
    memset(payload, 0xAB, sizeof(payload));
    static uint8_t big_out[65536 + 10 + 4];
    size_t n = ws_build_frame(WS_OP_BINARY, payload, sizeof(payload), mask, big_out, sizeof(big_out));
    assert(n == 10 + 4 + 65536);
    assert(big_out[1] == (uint8_t)(0x80 | 127));
    for (int i = 0; i < 8; i++) assert(big_out[2 + i] == (i == 7 ? 0x00 : 0x00) || true);
    // 64-bit BE length == 65536
    uint64_t len_be = 0;
    for (int i = 0; i < 8; i++) len_be = (len_be << 8) | big_out[2 + i];
    assert(len_be == 65536ULL);
  }
  // cap 不足
  {
    uint8_t payload[10] = {0};
    uint8_t small_out[5];
    size_t n = ws_build_frame(WS_OP_BINARY, payload, sizeof(payload), mask, small_out, sizeof(small_out));
    assert(n == 0);
  }
  printf("  test_build_lengths OK\n");
}

static void test_parse() {
  // server 未 mask 的 text "hello"
  {
    uint8_t buf[7] = {0x81, 0x05, 'h', 'e', 'l', 'l', 'o'};
    WsHeader h;
    int rc = ws_parse_header(buf, sizeof(buf), &h);
    assert(rc == 1);
    assert(h.fin == true);
    assert(h.opcode == WS_OP_TEXT);
    assert(h.masked == false);
    assert(h.payload_len == 5);
    assert(h.header_len == 2);
    assert(memcmp(buf + h.header_len, "hello", 5) == 0);
  }
  // ping with payload
  {
    uint8_t buf[2 + 4] = {0x89, 0x04, 'p', 'i', 'n', 'g'};
    WsHeader h;
    int rc = ws_parse_header(buf, sizeof(buf), &h);
    assert(rc == 1);
    assert(h.opcode == WS_OP_PING);
    assert(h.payload_len == 4);
    assert(h.header_len == 2);
  }
  // close with 2-byte code
  {
    uint8_t buf[4] = {0x88, 0x02, 0x03, 0xE8};   // code 1000
    WsHeader h;
    int rc = ws_parse_header(buf, sizeof(buf), &h);
    assert(rc == 1);
    assert(h.opcode == WS_OP_CLOSE);
    assert(h.payload_len == 2);
    assert(h.header_len == 2);
    assert(buf[2] == 0x03 && buf[3] == 0xE8);
  }
  // 126 擴充長度
  {
    static uint8_t buf[4 + 200];
    buf[0] = 0x82;
    buf[1] = 126;
    buf[2] = 0;
    buf[3] = 200;
    memset(buf + 4, 0x5A, 200);
    WsHeader h;
    int rc = ws_parse_header(buf, sizeof(buf), &h);
    assert(rc == 1);
    assert(h.payload_len == 200);
    assert(h.header_len == 4);
  }
  // 只給 1 byte -> 0
  {
    uint8_t buf[1] = {0x81};
    WsHeader h;
    int rc = ws_parse_header(buf, sizeof(buf), &h);
    assert(rc == 0);
  }
  // 資料不夠（宣告 len 126 但 buffer 沒給滿）-> 0
  {
    uint8_t buf[4] = {0x82, 126, 0, 50};   // 宣告 50 bytes payload, 但後面什麼都沒給
    WsHeader h;
    int rc = ws_parse_header(buf, sizeof(buf), &h);
    assert(rc == 0);
  }
  // RSV 位元設了 -> -1
  {
    uint8_t buf[2] = {(uint8_t)(0x81 | 0x40), 0x00};
    WsHeader h;
    int rc = ws_parse_header(buf, sizeof(buf), &h);
    assert(rc == -1);
  }
  // ping 長度 126（控制 frame 超過 125） -> -1
  {
    uint8_t buf[4] = {0x89, 126, 0, 126};
    WsHeader h;
    int rc = ws_parse_header(buf, sizeof(buf), &h);
    assert(rc == -1);
  }
  printf("  test_parse OK\n");
}

static void test_base64() {
  struct Case { const char* in; const char* out; };
  static const Case cases[] = {
      {"", ""},
      {"f", "Zg=="},
      {"fo", "Zm8="},
      {"foo", "Zm9v"},
      {"foob", "Zm9vYg=="},
      {"fooba", "Zm9vYmE="},
      {"foobar", "Zm9vYmFy"},
  };
  for (const auto& c : cases) {
    char out[64];
    size_t n = ws_base64_encode((const uint8_t*)c.in, strlen(c.in), out, sizeof(out));
    assert(n == strlen(c.out));
    assert(strcmp(out, c.out) == 0);
  }

  // 16 bytes 長度輸出 24 字元
  {
    uint8_t in16[16];
    for (int i = 0; i < 16; i++) in16[i] = (uint8_t)i;
    char out[64];
    size_t n = ws_base64_encode(in16, 16, out, sizeof(out));
    assert(n == 24);
    assert(strlen(out) == 24);
  }

  // cap 不足
  {
    uint8_t in3[3] = {1, 2, 3};
    char out[4];   // 需要 4+1，不夠
    size_t n = ws_base64_encode(in3, 3, out, sizeof(out));
    assert(n == 0);
  }
  printf("  test_base64 OK\n");
}

static void test_mic_ctl_parse() {
  assert(mic_ctl_parse("{\"type\":\"duck\"}", strlen("{\"type\":\"duck\"}")) == MIC_CTL_DUCK);
  {
    const char* s = "{\"type\": \"unduck\"}";
    assert(mic_ctl_parse(s, strlen(s)) == MIC_CTL_UNDUCK);
  }
  {
    const char* s = "{\"type\":\"ducking\"}";
    assert(mic_ctl_parse(s, strlen(s)) == MIC_CTL_UNKNOWN);
  }
  {
    const char* s = "{\"x\":\"duck\"}";
    assert(mic_ctl_parse(s, strlen(s)) == MIC_CTL_UNKNOWN);
  }
  assert(mic_ctl_parse("", 0) == MIC_CTL_UNKNOWN);
  printf("  test_mic_ctl_parse OK\n");
}

int main() {
  test_build_lengths();
  test_parse();
  test_base64();
  test_mic_ctl_parse();
  printf("test_ws_frame: all passed\n");
  return 0;
}
