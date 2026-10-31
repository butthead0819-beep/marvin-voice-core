// test_ima_adpcm.cpp — ima_adpcm.h 的 host 端黑箱測試，比對 gen_ima_adpcm_vectors.py 用
// Python 版 ImaAdpcmEncoder 產生的向量。
//
// 編譯執行：
//   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined,address -fno-sanitize-recover=all \
//     -o /tmp/test_ima_adpcm firmware/car_puck/test/test_ima_adpcm.cpp && /tmp/test_ima_adpcm

#undef NDEBUG
#include <assert.h>
#include <string.h>
#include <stdio.h>

#include "../ima_adpcm.h"
#include "ima_adpcm_vectors.h"

static void run_case(const char* name, const int16_t* pcm, const uint8_t* encoded, int n_blocks) {
  ImaAdpcmState st;
  ima_adpcm_reset(&st);

  uint8_t out[IMA_BLOCK_BYTES];
  uint8_t header_before_block2[4] = {0};

  for (int b = 0; b < n_blocks; b++) {
    if (b == 1) {
      // 驗第 2 個 block 的 header 等於第 1 個 block 編完後的 state。
      header_before_block2[0] = (uint8_t)(st.predictor & 0xFF);
      header_before_block2[1] = (uint8_t)((st.predictor >> 8) & 0xFF);
      header_before_block2[2] = st.index;
      header_before_block2[3] = 0;
    }
    ima_adpcm_encode_block(&st, pcm + b * IMA_BLOCK_SAMPLES, out);
    int rc = memcmp(out, encoded + b * IMA_BLOCK_BYTES, IMA_BLOCK_BYTES);
    if (rc != 0) {
      fprintf(stderr, "FAIL %s block %d mismatch\n", name, b);
      assert(rc == 0);
    }
    if (b == 1) {
      assert(memcmp(encoded + 1 * IMA_BLOCK_BYTES, header_before_block2, 4) == 0);
    }
  }
  printf("  %s: %d blocks OK\n", name, n_blocks);
}

int main() {
  run_case("silence", IMA_VEC_silence_pcm, IMA_VEC_silence_encoded, IMA_VEC_silence_NBLOCKS);
  run_case("sine440", IMA_VEC_sine440_pcm, IMA_VEC_sine440_encoded, IMA_VEC_sine440_NBLOCKS);
  run_case("square_fullscale", IMA_VEC_square_fullscale_pcm, IMA_VEC_square_fullscale_encoded,
           IMA_VEC_square_fullscale_NBLOCKS);
  run_case("step_to_max_index", IMA_VEC_step_to_max_index_pcm, IMA_VEC_step_to_max_index_encoded,
           IMA_VEC_step_to_max_index_NBLOCKS);
  run_case("white_noise", IMA_VEC_white_noise_pcm, IMA_VEC_white_noise_encoded,
           IMA_VEC_white_noise_NBLOCKS);

  printf("test_ima_adpcm: all passed\n");
  return 0;
}
