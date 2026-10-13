// test_icy_demux.cpp — icy_demux.h 的 host 端黑箱測試
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o test_icy_demux firmware/car_puck/test/test_icy_demux.cpp && ./test_icy_demux
//
// 原則：只透過公開介面驗證性質，不在測試裡複製被測邏輯來驗算——用測試內自己組的
// ICY 位元流（metaint 小一點方便手工建構），驗證音訊還原、metadata 回呼內容是否正確。

#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <vector>
#include "../icy_demux.h"

static std::vector<uint8_t> build_block(uint8_t length_byte, const char* payload) {
  std::vector<uint8_t> out;
  out.push_back(length_byte);
  size_t total = (size_t)length_byte * 16;
  size_t plen = strlen(payload);
  for (size_t i = 0; i < total; i++) {
    out.push_back(i < plen ? (uint8_t)payload[i] : 0);
  }
  return out;
}

struct MetaCollector {
  std::vector<std::string> metas;
};

static void collect_meta(const uint8_t* payload, size_t len, void* ctx) {
  auto* c = (MetaCollector*)ctx;
  c->metas.emplace_back((const char*)payload, len);
}

int main() {
  // T1：音訊拼接正確——metaint=10，兩段音訊中間插一個完整 payload 區塊
  {
    const uint32_t metaint = 10;
    std::vector<uint8_t> audio1(metaint, 'a');
    std::vector<uint8_t> audio2(metaint, 'b');
    auto block = build_block(1, "hello");  // 1*16=16 bytes 容量，夠放 "hello"

    std::vector<uint8_t> stream;
    stream.insert(stream.end(), audio1.begin(), audio1.end());
    stream.insert(stream.end(), block.begin(), block.end());
    stream.insert(stream.end(), audio2.begin(), audio2.end());

    IcyDemux d;
    icy_init(d, metaint);
    MetaCollector mc;
    std::vector<uint8_t> audio_out(stream.size());
    size_t an = icy_feed(d, stream.data(), stream.size(), audio_out.data(), collect_meta, &mc);

    std::vector<uint8_t> expect_audio;
    expect_audio.insert(expect_audio.end(), audio1.begin(), audio1.end());
    expect_audio.insert(expect_audio.end(), audio2.begin(), audio2.end());
    assert(an == expect_audio.size());
    assert(memcmp(audio_out.data(), expect_audio.data(), an) == 0);
    assert(mc.metas.size() == 1);
    assert(mc.metas[0] == "hello");
    printf("T1 ok\n");
  }

  // T2：空區塊（L=0）不觸發回呼
  {
    const uint32_t metaint = 5;
    std::vector<uint8_t> audio1(metaint, 'x');
    std::vector<uint8_t> stream = audio1;
    stream.push_back(0);  // 空區塊
    std::vector<uint8_t> audio2(metaint, 'y');
    stream.insert(stream.end(), audio2.begin(), audio2.end());

    IcyDemux d;
    icy_init(d, metaint);
    MetaCollector mc;
    std::vector<uint8_t> audio_out(stream.size());
    size_t an = icy_feed(d, stream.data(), stream.size(), audio_out.data(), collect_meta, &mc);

    std::vector<uint8_t> expect_audio = audio1;
    expect_audio.insert(expect_audio.end(), audio2.begin(), audio2.end());
    assert(an == expect_audio.size());
    assert(memcmp(audio_out.data(), expect_audio.data(), an) == 0);
    assert(mc.metas.empty());
    printf("T2 ok\n");
  }

  // T3：跨 feed 邊界——每次只餵 1 byte，結果要跟整批餵一樣
  {
    const uint32_t metaint = 10;
    std::vector<uint8_t> audio1(metaint, 'a');
    std::vector<uint8_t> audio2(metaint, 'b');
    auto block = build_block(2, "world!");  // 2*16=32 bytes

    std::vector<uint8_t> stream;
    stream.insert(stream.end(), audio1.begin(), audio1.end());
    stream.insert(stream.end(), block.begin(), block.end());
    stream.insert(stream.end(), audio2.begin(), audio2.end());

    IcyDemux d;
    icy_init(d, metaint);
    MetaCollector mc;
    std::vector<uint8_t> audio_out;
    audio_out.resize(stream.size());
    size_t total_an = 0;
    for (size_t i = 0; i < stream.size(); i++) {
      uint8_t one_byte_out[1];
      size_t an = icy_feed(d, &stream[i], 1, one_byte_out, collect_meta, &mc);
      if (an > 0) {
        audio_out[total_an++] = one_byte_out[0];
      }
    }

    std::vector<uint8_t> expect_audio;
    expect_audio.insert(expect_audio.end(), audio1.begin(), audio1.end());
    expect_audio.insert(expect_audio.end(), audio2.begin(), audio2.end());
    assert(total_an == expect_audio.size());
    assert(memcmp(audio_out.data(), expect_audio.data(), total_an) == 0);
    assert(mc.metas.size() == 1);
    assert(mc.metas[0] == "world!");
    printf("T3 ok\n");
  }

  // T4：metaint=0 全部當音訊，不拆
  {
    IcyDemux d;
    icy_init(d, 0);
    std::vector<uint8_t> stream = {1, 2, 3, 4, 5, 0, 99, 100};  // 刻意放一個看起來像長度 byte 的 0
    MetaCollector mc;
    std::vector<uint8_t> audio_out(stream.size());
    size_t an = icy_feed(d, stream.data(), stream.size(), audio_out.data(), collect_meta, &mc);
    assert(an == stream.size());
    assert(memcmp(audio_out.data(), stream.data(), an) == 0);
    assert(mc.metas.empty());
    printf("T4 ok\n");
  }

  // T5：跨兩個 metaint 邊界的大 chunk 一次餵進去
  {
    const uint32_t metaint = 8;
    std::vector<uint8_t> a1(metaint, 1), a2(metaint, 2), a3(metaint, 3);
    auto b1 = build_block(1, "m1");
    auto b2 = build_block(1, "m2");

    std::vector<uint8_t> stream;
    stream.insert(stream.end(), a1.begin(), a1.end());
    stream.insert(stream.end(), b1.begin(), b1.end());
    stream.insert(stream.end(), a2.begin(), a2.end());
    stream.insert(stream.end(), b2.begin(), b2.end());
    stream.insert(stream.end(), a3.begin(), a3.end());

    IcyDemux d;
    icy_init(d, metaint);
    MetaCollector mc;
    std::vector<uint8_t> audio_out(stream.size());
    size_t an = icy_feed(d, stream.data(), stream.size(), audio_out.data(), collect_meta, &mc);

    std::vector<uint8_t> expect_audio;
    expect_audio.insert(expect_audio.end(), a1.begin(), a1.end());
    expect_audio.insert(expect_audio.end(), a2.begin(), a2.end());
    expect_audio.insert(expect_audio.end(), a3.begin(), a3.end());
    assert(an == expect_audio.size());
    assert(memcmp(audio_out.data(), expect_audio.data(), an) == 0);
    assert(mc.metas.size() == 2);
    assert(mc.metas[0] == "m1");
    assert(mc.metas[1] == "m2");
    printf("T5 ok\n");
  }

  printf("all icy_demux tests passed\n");
  return 0;
}
