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

#endif  // AVRC_META_WRAP_H
