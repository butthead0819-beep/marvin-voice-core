// avrc_meta_wrap.cpp — 攔截 Bluedroid 對車機 AVRCP GetElementAttributes(0x20)/
// GetPlayStatus(0x30) 的拒絕回應，改回真資料。
//
// 背景（已讀過 IDF v5.5.4 原始碼確認）：btc_avrc.c::btc_rc_upstreams_evt 收到這兩種
// PDU 時呼叫 static send_reject_response()，內部 AVRC_BldResponse(rc_handle,
// &rsp{pdu, status=AVRC_STS_BAD_CMD, opcode=AVRC_OP_VENDOR}, &p_msg) 然後
// BTA_AvMetaRsp(rc_handle, label, AVRC_RSP_REJ, p_msg)。這兩個是 libbt.a 裡跨 .o 的
// 全域符號，用 -Wl,--wrap 攔（連結旗標見 bt_bridge.ino 檔頭）。
//
// Arduino 沒附內部標頭，本檔自行宣告結構——照 IDF 5.5.4 的 avrc_defs.h 抄，升級
// ESP32 core 版本要重新核對欄位配置，否則可能寫壞記憶體或編譯不過（見下面 #error）。
#include "avrc_meta_wrap.h"

#include <Arduino.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#if ESP_IDF_VERSION != ESP_IDF_VERSION_VAL(5, 5, 4)
#error "avrc_meta_wrap.cpp 的結構定義是照 IDF 5.5.4 的 avrc_defs.h 抄的；升級 ESP32 core 版本後要重新核對欄位配置，否則可能寫壞記憶體或編譯不過。"
#endif

typedef uint8_t tAVRC_STS;
typedef struct { uint16_t charset_id; uint16_t str_len; uint8_t *p_str; } tAVRC_FULL_NAME;
typedef struct { uint32_t attr_id; tAVRC_FULL_NAME name; } tAVRC_ATTR_ENTRY;
typedef struct { uint8_t pdu; tAVRC_STS status; uint8_t opcode; } tAVRC_RSP;
typedef struct { uint8_t pdu; tAVRC_STS status; uint8_t opcode; uint8_t num_attr; tAVRC_ATTR_ENTRY *p_attrs; } tAVRC_GET_ELEM_ATTRS_RSP;
typedef struct { uint8_t pdu; tAVRC_STS status; uint8_t opcode; uint32_t song_len; uint32_t song_pos; uint8_t play_status; } tAVRC_GET_PLAY_STATUS_RSP;
typedef struct { uint8_t ctype; uint8_t subunit_type; uint8_t subunit_id; uint8_t opcode; } tAVRC_HDR;
typedef struct { tAVRC_HDR hdr; uint32_t company_id; uint8_t *p_vendor_data; uint16_t vendor_len; } tAVRC_MSG_VENDOR;
#define AVRC_CAP_MAX_NUM_COMP_ID 4
#define AVRC_CAP_MAX_NUM_EVT_ID 16
typedef union { uint32_t company_id[AVRC_CAP_MAX_NUM_COMP_ID]; uint8_t event_id[AVRC_CAP_MAX_NUM_EVT_ID]; } tAVRC_CAPS_PARAM;
typedef struct { uint8_t pdu; tAVRC_STS status; uint8_t opcode; uint8_t capability_id; uint8_t count; tAVRC_CAPS_PARAM param; } tAVRC_GET_CAPS_RSP;
typedef union { uint8_t pdu; tAVRC_RSP rsp; tAVRC_GET_ELEM_ATTRS_RSP get_elem_attrs; tAVRC_GET_PLAY_STATUS_RSP get_play_status; tAVRC_GET_CAPS_RSP get_caps; uint8_t _pad[64]; } tAVRC_RESPONSE;

static_assert(offsetof(tAVRC_GET_CAPS_RSP, param) == 8, "tAVRC_GET_CAPS_RSP 欄位配置跟 IDF 5.5.4 不符");
static_assert(offsetof(tAVRC_MSG_VENDOR, p_vendor_data) == 8, "tAVRC_MSG_VENDOR 欄位配置跟 IDF 5.5.4 不符");
static_assert(offsetof(tAVRC_MSG_VENDOR, vendor_len) == 12, "tAVRC_MSG_VENDOR 欄位配置跟 IDF 5.5.4 不符");

static const uint8_t  AVRC_PDU_GET_ELEMENT_ATTR = 0x20;
static const uint8_t  AVRC_PDU_GET_PLAY_STATUS  = 0x30;
static const uint8_t  AVRC_PDU_GET_CAPABILITIES = 0x10;
static const uint8_t  AVRC_CAP_EVENTS_SUPPORTED = 0x03;
static const tAVRC_STS AVRC_STS_BAD_CMD         = 0x00;
static const tAVRC_STS AVRC_STS_NO_ERROR        = 0x04;
static const uint8_t  AVRC_RSP_REJ              = 10;
static const uint8_t  AVRC_RSP_IMPL_STBL        = 12;
static const uint16_t AVRC_CHARSET_ID_UTF8      = 0x006a;
static const uint32_t AVRC_ATTR_ID_TITLE        = 1;
static const uint32_t AVRC_ATTR_ID_ARTIST       = 2;
static const uint32_t AVRC_ATTR_ID_ALBUM        = 3;
static const uint32_t AVRC_ATTR_ID_PLAYING_TIME = 7;
extern "C" tAVRC_STS __real_AVRC_BldResponse(uint8_t handle, tAVRC_RESPONSE *p_rsp, void **pp_pkt);
extern "C" void __real_BTA_AvMetaRsp(uint8_t rc_handle, uint8_t label, uint8_t rsp_code, void *p_pkt);
extern "C" uint16_t __real_AVRC_VendorRsp(uint8_t handle, uint8_t label, tAVRC_MSG_VENDOR *p_msg);

// 回報給車機的播放狀態（IDF esp_avrc_playback_stat_t 的原始值，PLAYING=1/PAUSED=2）
// ——bt_bridge.ino 的 on_wheel_key 跟著方向盤 PLAY/PAUSE 鍵寫，單一 byte 不需要鎖。
extern volatile uint8_t g_reported_playback;

// ---- 狀態：BTC task（__wrap_* 這裡）跟 loop()（avrc_meta_set）兩個 task 互斥保護 ----
static NowPlaying s_np;
static portMUX_TYPE s_np_mux = portMUX_INITIALIZER_UNLOCKED;
static uint32_t s_hits_elem = 0;
static uint32_t s_hits_status = 0;
static volatile uint32_t s_unserved_elem = 0;
static volatile uint32_t s_unserved_status = 0;
// __wrap_AVRC_BldResponse 跟 __wrap_BTA_AvMetaRsp 在 send_reject_response() 裡同一
// task 緊接著呼叫（中間不會被其他事件插進來），這個旗標不需要鎖。
static bool s_upgrade_next = false;

// ---- AVRCP metadata PDU 追蹤（診斷用，不影響任何回應內容）----
// 獨立的 mux：跟 s_np_mux 保護的資料無關，不共用避免臨界區互相拖長。
static PduTrace s_pt;
static portMUX_TYPE s_pt_mux = portMUX_INITIALIZER_UNLOCKED;

// 連結防呆：沒加 --wrap 旗標時 __wrap_* 沒人呼叫、會被 gc-sections 整段丟掉，連帶
// 對 __real_* 的引用也消失 → 照樣連結成功、燒進去一個靜默不顯示曲名的版本（10/13 實測）。
// 這裡從一定會被保留的 avrc_meta_set（loop 每筆都呼叫）讀一次 volatile 指標表，
// 逼連結器一定要解析 __real_*，少了旗標就 undefined reference。
static void* const volatile s_force_wrap_link[] = {
  (void*)&__real_AVRC_BldResponse, (void*)&__real_BTA_AvMetaRsp, (void*)&__real_AVRC_VendorRsp,
};

void avrc_meta_set(const NowPlaying& np) {
  (void)s_force_wrap_link[0];
  portENTER_CRITICAL(&s_np_mux);
  s_np = np;
  portEXIT_CRITICAL(&s_np_mux);
}

void avrc_meta_get(NowPlaying& out) {
  portENTER_CRITICAL(&s_np_mux);
  out = s_np;
  portEXIT_CRITICAL(&s_np_mux);
}

uint32_t avrc_meta_hits_elem() { return s_hits_elem; }
uint32_t avrc_meta_hits_status() { return s_hits_status; }
uint32_t avrc_meta_unserved_elem() { return s_unserved_elem; }
uint32_t avrc_meta_unserved_status() { return s_unserved_status; }

void avrc_pdu_trace_get(PduTrace& out) {
  portENTER_CRITICAL(&s_pt_mux);
  out = s_pt;
  portEXIT_CRITICAL(&s_pt_mux);
}

extern "C" tAVRC_STS __wrap_AVRC_BldResponse(uint8_t handle, tAVRC_RESPONSE *p_rsp, void **pp_pkt) {
  if (p_rsp != nullptr) {
    portENTER_CRITICAL(&s_pt_mux);
    pt_record(s_pt, 'B', p_rsp->pdu, p_rsp->rsp.status);
    if (p_rsp->pdu == AVRC_PDU_GET_CAPABILITIES && p_rsp->get_caps.capability_id == AVRC_CAP_EVENTS_SUPPORTED &&
        p_rsp->get_caps.status == AVRC_STS_NO_ERROR) {
      uint8_t count = p_rsp->get_caps.count > 16 ? 16 : p_rsp->get_caps.count;
      pt_set_caps(s_pt, count, p_rsp->get_caps.param.event_id);
    }
    portEXIT_CRITICAL(&s_pt_mux);
  }

  if (p_rsp == nullptr || p_rsp->rsp.status != AVRC_STS_BAD_CMD ||
      !(p_rsp->rsp.pdu == AVRC_PDU_GET_ELEMENT_ATTR || p_rsp->rsp.pdu == AVRC_PDU_GET_PLAY_STATUS)) {
    return __real_AVRC_BldResponse(handle, p_rsp, pp_pkt);
  }

  NowPlaying np;
  portENTER_CRITICAL(&s_np_mux);
  np = s_np;
  portEXIT_CRITICAL(&s_np_mux);

  tAVRC_RESPONSE r;
  memset(&r, 0, sizeof(r));
  tAVRC_STS result;

  if (p_rsp->rsp.pdu == AVRC_PDU_GET_ELEMENT_ATTR) {
    // 沒收過 S3 曲目資訊、或收過但標題是空字串，都算「沒真資料」——回佔位標題，不拒絕。
    bool has_title = np.valid && np.title[0];
    const char* title = has_title ? np.title : "Marvin";
    const char* artist = np.valid ? np.artist : "";
    const char* album = np.valid ? np.album : "";
    bool have_duration = np.valid && np.dur_ms != 0;
    char dur_str[16];
    if (have_duration) snprintf(dur_str, sizeof(dur_str), "%lu", (unsigned long)np.dur_ms);

    tAVRC_ATTR_ENTRY attrs[4];
    attrs[0].attr_id = AVRC_ATTR_ID_TITLE;
    attrs[0].name.charset_id = AVRC_CHARSET_ID_UTF8;
    attrs[0].name.p_str = (uint8_t*)title;
    attrs[0].name.str_len = (uint16_t)strlen(title);
    attrs[1].attr_id = AVRC_ATTR_ID_ARTIST;
    attrs[1].name.charset_id = AVRC_CHARSET_ID_UTF8;
    attrs[1].name.p_str = (uint8_t*)artist;
    attrs[1].name.str_len = (uint16_t)strlen(artist);
    attrs[2].attr_id = AVRC_ATTR_ID_ALBUM;
    attrs[2].name.charset_id = AVRC_CHARSET_ID_UTF8;
    attrs[2].name.p_str = (uint8_t*)album;
    attrs[2].name.str_len = (uint16_t)strlen(album);
    uint8_t num_attr = 3;
    if (have_duration) {
      attrs[3].attr_id = AVRC_ATTR_ID_PLAYING_TIME;
      attrs[3].name.charset_id = AVRC_CHARSET_ID_UTF8;
      attrs[3].name.p_str = (uint8_t*)dur_str;
      attrs[3].name.str_len = (uint16_t)strlen(dur_str);
      num_attr = 4;
    }

    r.get_elem_attrs.pdu = AVRC_PDU_GET_ELEMENT_ATTR;
    r.get_elem_attrs.status = AVRC_STS_NO_ERROR;
    r.get_elem_attrs.opcode = p_rsp->rsp.opcode;
    r.get_elem_attrs.num_attr = num_attr;
    r.get_elem_attrs.p_attrs = attrs;

    result = __real_AVRC_BldResponse(handle, &r, pp_pkt);
    if (result == AVRC_STS_NO_ERROR) {
      s_upgrade_next = true;
      if (has_title) s_hits_elem++; else s_unserved_elem++;
    }
    return result;
  }

  // AVRC_PDU_GET_PLAY_STATUS：沒資料時 song_len/song_pos 回 0xFFFFFFFF（AVRCP 的
  // 「不支援/未知」值），播放狀態仍照車機最後下的指令回報（見 g_reported_playback）。
  bool have_duration = np.valid && np.dur_ms != 0;
  r.get_play_status.pdu = AVRC_PDU_GET_PLAY_STATUS;
  r.get_play_status.status = AVRC_STS_NO_ERROR;
  r.get_play_status.opcode = p_rsp->rsp.opcode;
  if (have_duration) {
    r.get_play_status.song_len = np.dur_ms;
    r.get_play_status.song_pos = np_position_now(np, millis());
  } else {
    r.get_play_status.song_len = 0xFFFFFFFF;
    r.get_play_status.song_pos = 0xFFFFFFFF;
  }
  r.get_play_status.play_status = g_reported_playback;

  result = __real_AVRC_BldResponse(handle, &r, pp_pkt);
  if (result == AVRC_STS_NO_ERROR) {
    s_upgrade_next = true;
    if (have_duration) s_hits_status++; else s_unserved_status++;
  }
  return result;
}

extern "C" void __wrap_BTA_AvMetaRsp(uint8_t rc_handle, uint8_t label, uint8_t rsp_code, void *p_pkt) {
  if (s_upgrade_next && rsp_code == AVRC_RSP_REJ) {
    rsp_code = AVRC_RSP_IMPL_STBL;
  }
  s_upgrade_next = false;
  __real_BTA_AvMetaRsp(rc_handle, label, rsp_code, p_pkt);
}

// 功能旗標不符（沒 VENDOR）或 subunit 不是 PANEL 時，Bluedroid 改走這條回 NOT_IMPL/REJ，
// 不經過 AVRC_BldResponse——追蹤用，不改行為。
extern "C" uint16_t __wrap_AVRC_VendorRsp(uint8_t handle, uint8_t label, tAVRC_MSG_VENDOR *p_msg) {
  uint8_t pdu = (p_msg != nullptr && p_msg->p_vendor_data != nullptr && p_msg->vendor_len != 0) ? p_msg->p_vendor_data[0] : 0xFF;
  uint8_t st = p_msg != nullptr ? p_msg->hdr.ctype : 0xFF;
  portENTER_CRITICAL(&s_pt_mux);
  pt_record(s_pt, 'V', pdu, st);
  portEXIT_CRITICAL(&s_pt_mux);
  return __real_AVRC_VendorRsp(handle, label, p_msg);
}
