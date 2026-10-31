// ima_adpcm.h — IMA ADPCM 編碼器：puck 麥克風上傳格式（純 C++，不 include Arduino，host 可測）。
//
// 規格見 ima_adpcm.py（唯一標準，韌體版逐 byte 跟它一致）：一個 block = 4 bytes header +
// 256 bytes data = 260 bytes，代表 512 個 int16 取樣（32ms @16kHz）。
//   header: int16 LE predictor（編碼這個 block 之前的預測值）、
//           uint8 step_index（0..88，block 之前的狀態）、uint8 reserved=0。
//   data:   512 個 4-bit nibble，每個 byte 低 nibble 先
//           （第 2k 個取樣在 byte k 的低 4 bit，第 2k+1 個在高 4 bit）。
#ifndef IMA_ADPCM_H
#define IMA_ADPCM_H

#include <stddef.h>
#include <stdint.h>

#define IMA_BLOCK_SAMPLES 512
#define IMA_BLOCK_BYTES 260

static const int16_t IMA_STEP_TABLE[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
};

static const int8_t IMA_INDEX_TABLE[16] = {
    -1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8,
};

struct ImaAdpcmState {
  int16_t predictor;
  uint8_t index;
};

static inline void ima_adpcm_reset(ImaAdpcmState* st) {
  st->predictor = 0;
  st->index = 0;
}

// 單個 nibble 的解碼步驟（編碼時也要跑一次算出下一步狀態，跟 ima_adpcm.py 的
// _decode_nibble 對應）。
static inline void ima_adpcm_decode_nibble(uint8_t n, int16_t* predictor, uint8_t* index) {
  int32_t step = IMA_STEP_TABLE[*index];
  int32_t diff = step >> 3;
  if (n & 4) diff += step;
  if (n & 2) diff += step >> 1;
  if (n & 1) diff += step >> 2;
  int32_t pred = *predictor;
  if (n & 8) pred -= diff;
  else pred += diff;
  if (pred > 32767) pred = 32767;
  if (pred < -32768) pred = -32768;
  *predictor = (int16_t)pred;

  int idx = (int)*index + IMA_INDEX_TABLE[n];
  if (idx < 0) idx = 0;
  if (idx > 88) idx = 88;
  *index = (uint8_t)idx;
}

static inline void ima_adpcm_encode_block(ImaAdpcmState* st, const int16_t* pcm512, uint8_t* out260) {
  int16_t header_predictor = st->predictor;
  uint8_t header_index = st->index;

  int16_t predictor = st->predictor;
  uint8_t index = st->index;
  uint8_t nibbles[IMA_BLOCK_SAMPLES];

  for (int i = 0; i < IMA_BLOCK_SAMPLES; i++) {
    int32_t sample = pcm512[i];
    int32_t diff = sample - predictor;
    uint8_t sign = (diff < 0) ? 0x8 : 0;
    if (diff < 0) diff = -diff;

    int32_t step = IMA_STEP_TABLE[index];
    uint8_t n = 0;
    if (diff >= step) { n |= 4; diff -= step; }
    int32_t step_half = step >> 1;
    if (diff >= step_half) { n |= 2; diff -= step_half; }
    int32_t step_quarter = step >> 2;
    if (diff >= step_quarter) { n |= 1; }
    n |= sign;

    ima_adpcm_decode_nibble(n, &predictor, &index);
    nibbles[i] = n;
  }

  st->predictor = predictor;
  st->index = index;

  out260[0] = (uint8_t)(header_predictor & 0xFF);
  out260[1] = (uint8_t)((header_predictor >> 8) & 0xFF);
  out260[2] = header_index;
  out260[3] = 0;

  uint8_t* data = out260 + 4;
  for (int k = 0; k < 256; k++) {
    uint8_t lo = nibbles[2 * k] & 0x0F;
    uint8_t hi = nibbles[2 * k + 1] & 0x0F;
    data[k] = (uint8_t)(lo | (hi << 4));
  }
}

#endif  // IMA_ADPCM_H
