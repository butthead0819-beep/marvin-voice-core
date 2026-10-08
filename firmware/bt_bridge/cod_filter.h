// cod_filter.h — 配對模式自動認「車機/藍牙喇叭」（純 C++，不依賴 Arduino / ESP-IDF，可在 host 端測試）
//
// 依 Bluetooth Class of Device：主類別 Audio/Video（bits 12..8 = 0x04）且服務含
// Rendering（bit 18）或 Audio（bit 21）。手機（主類別 Phone）即使服務含 Audio 也不收。
// 換車/換喇叭不用再改韌體加名稱或位址（BMW 04900 的搜尋回應沒有名稱，見 10/7 [DIAG]）。
#ifndef COD_FILTER_H
#define COD_FILTER_H

#include <stdint.h>

static const uint32_t COD_MAJOR_AV = 0x04;
static const uint32_t COD_SRVC_RENDERING = 1u << 18;
static const uint32_t COD_SRVC_AUDIO = 1u << 21;

inline bool cod_is_audio_sink(uint32_t cod) {
  uint32_t major = (cod >> 8) & 0x1F;
  if (major != COD_MAJOR_AV) return false;
  return (cod & (COD_SRVC_RENDERING | COD_SRVC_AUDIO)) != 0;
}

// 配對模式整輪掃描挑 RSSI 最強的候選；還沒有候選，或新訊號比目前候選強就換。
inline bool cod_candidate_better(bool has_best, int rssi, int best_rssi) {
  return !has_best || rssi > best_rssi;
}

#endif  // COD_FILTER_H
