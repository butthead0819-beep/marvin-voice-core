// rc_cb_view.h — 以 byte 偏移讀寫 IDF btc_avrc.c 的全域 btc_rc_cb_t（純 C++，不依賴
// Arduino / ESP-IDF，可在 host 端測試）。
//
// 背景：IDF TG 只准 VOLUME_CHANGE 通知（cs_rn_allowed_evt = 0x2000），
// RegisterNotification 送 TRACK_CHANGE/PLAY_STATUS_CHANGE/PLAY_POS_CHANGED 都被 BAD_PARAM
// 拒絕，車機因此從不問 GetElementAttributes。avrc_meta_wrap.cpp 用 -Wl,--wrap 攔
// btc_avrc_tg_rn_evt_supported/btc_avrc_tg_get_rn_supported_evt 讓 IDF 以為這三種事件有
// 支援，但 btc_avrc_tg_send_rn_rsp（static）只實作 VOLUME_CHANGE，其餘事件沒機會送出——
// 所以我們自己組包送（見 avrc_meta_wrap.cpp::avrc_rn_send），這裡只是讀/寫 btc 用來記錄
// 「車機是否已對某事件送出 RegisterNotification」的那塊全域狀態（btc_rc_cb_ptr）。
//
// 不直接宣告 btc_rc_cb_t 結構（避免 padding 猜錯），改用寫死的 byte 偏移——偏移是照
// Arduino ESP32 core 3.3.10 的 sdkconfig（CONFIG_BT_AVRCP_CT_COVER_ART_ENABLED=1）算的，
// 升級 core 版本要重新核對（見 avrc_meta_wrap.cpp 的 #error 防呆）。
#ifndef RC_CB_VIEW_H
#define RC_CB_VIEW_H

#include <stddef.h>
#include <stdint.h>

static const size_t RCV_OFF_CONNECTED = 0;
static const size_t RCV_OFF_HANDLE = 2;
static const size_t RCV_OFF_NTF = 16;
static const uint8_t RCV_MAX_EVT = 13;

// rc_ntf[event_id-1] 的 byte 偏移（registered 欄位；label 在 +1）。event_id 須先經過
// 呼叫端的範圍檢查（1..RCV_MAX_EVT），這裡不再檢查。
inline size_t rcv_ntf_off(uint8_t event_id) {
  return RCV_OFF_NTF + 2u * (size_t)(event_id - 1u);
}

// 可以送這個事件的通知嗎：cb 非空、event_id 在 [1,13]、rc_connected==1、
// rc_ntf[event_id-1].registered==1 才回 true，並寫出 handle/label。
// registered 欄位只可能是 0 或 1，其他值視為結構對不上，回 false。
inline bool rcv_can_notify(const uint8_t* cb, uint8_t event_id, uint8_t* handle, uint8_t* label) {
  if (cb == nullptr || event_id < 1 || event_id > RCV_MAX_EVT) return false;
  if (cb[RCV_OFF_CONNECTED] != 1) return false;
  size_t off = rcv_ntf_off(event_id);
  uint8_t registered = cb[off];
  if (registered != 1) return false;
  if (handle != nullptr) *handle = cb[RCV_OFF_HANDLE];
  if (label != nullptr) *label = cb[off + 1];
  return true;
}

// 送完 CHANGED 後模仿 btc 自己送通知時的行為：把 rc_ntf[event_id-1].registered 設回
// 0（車機收到 CHANGED 後會重新註冊）。event_id 不合法或 cb 為空就不動。
inline void rcv_mark_unregistered(uint8_t* cb, uint8_t event_id) {
  if (cb == nullptr || event_id < 1 || event_id > RCV_MAX_EVT) return;
  cb[rcv_ntf_off(event_id)] = 0;
}

// 我們宣告支援的事件：PLAY_STATUS(1)、TRACK_CHANGE(2)、PLAY_POS(5)、VOLUME(13)。
static const uint16_t RCV_SUPPORTED_EVT_MASK = 0x2026;

inline bool rcv_evt_supported(uint8_t event_id) {
  return event_id < 16 && ((RCV_SUPPORTED_EVT_MASK >> event_id) & 1u) != 0;
}

#endif  // RC_CB_VIEW_H
