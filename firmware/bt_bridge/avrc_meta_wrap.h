// avrc_meta_wrap.h — avrc_meta_wrap.cpp 對外介面。見該檔檔頭說明背景與連結旗標。
#ifndef AVRC_META_WRAP_H
#define AVRC_META_WRAP_H

#include <stdint.h>

#include "now_playing.h"
#include "pdu_trace.h"

void avrc_meta_set(const NowPlaying& np);
// 加鎖取快照：給其他 task（BT app task 的 TG 事件）讀，避免讀到 loop 寫到一半的資料。
void avrc_meta_get(NowPlaying& out);
uint32_t avrc_meta_hits_elem();
uint32_t avrc_meta_hits_status();
// 車機有問（pdu 0x20/0x30 走到拒絕路徑）但當下沒有真資料（GetElementAttributes：標題
// 用了佔位字串「Marvin」；GetPlayStatus：沒收過曲目或 dur_ms==0）而用佔位資料回應的
// 次數——用來跟 hits_elem/hits_status 分辨「車機沒來問」還是「來問了但沒真資料」。
uint32_t avrc_meta_unserved_elem();
uint32_t avrc_meta_unserved_status();

// 加鎖取快照：AVRCP metadata PDU 追蹤（診斷用，見 pdu_trace.h）。
void avrc_pdu_trace_get(PduTrace& out);

// 自己組 RegisterNotification 回應送給車機（繞過 btc_avrc_tg_send_rn_rsp 只實作
// VOLUME_CHANGE 的限制，見 avrc_meta_wrap.cpp 檔頭與 rc_cb_view.h）。
// event_id 只接受 1(PLAY_STATUS_CHANGE)/2(TRACK_CHANGE)/5(PLAY_POS_CHANGED)，其餘回 false。
// changed=false 送 INTERIM；true 送 CHANGED（送完會把 btc 的 registered 旗標清掉，車機收到
// 後會重新註冊）。track8：TRACK_CHANGE 用的 8 bytes UID；play_status：PLAY_STATUS_CHANGE
// 用；play_pos：PLAY_POS_CHANGED 用（ms）；不相關的參數會被忽略。
// 回傳是否真的送出（車機沒註冊/結構對不上/組包失敗都回 false，不會丟例外或當掉）。
bool avrc_rn_send(uint8_t event_id, bool changed, const uint8_t track8[8], uint8_t play_status, uint32_t play_pos);
uint32_t avrc_rn_fail_count();

// GetElementAttributes 請求/回應探針（診斷用，不影響任何行為，見 avrc_meta_wrap.cpp）。
struct ElemProbe {
  uint32_t req_cnt;             // GetElementAttributes 請求總數（經 AVRC_ParsCommand 解析的）
  bool req_captured;
  uint8_t req_num_attr;
  uint8_t req_attrs[8];
  bool rsp_captured;
  uint16_t rsp_len;
  uint8_t rsp_n;
  uint8_t rsp_bytes[36];
};
// 加鎖取快照。
void avrc_elem_probe_get(ElemProbe& out);

#endif  // AVRC_META_WRAP_H
