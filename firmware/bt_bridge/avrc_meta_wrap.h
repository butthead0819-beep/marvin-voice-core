// avrc_meta_wrap.h — avrc_meta_wrap.cpp 對外介面。見該檔檔頭說明背景與連結旗標。
#ifndef AVRC_META_WRAP_H
#define AVRC_META_WRAP_H

#include <stdint.h>

#include "now_playing.h"

void avrc_meta_set(const NowPlaying& np);
// 加鎖取快照：給其他 task（BT app task 的 TG 事件）讀，避免讀到 loop 寫到一半的資料。
void avrc_meta_get(NowPlaying& out);

// 自己組 RegisterNotification 回應送給車機（繞過 btc_avrc_tg_send_rn_rsp 只實作
// VOLUME_CHANGE 的限制，見 avrc_meta_wrap.cpp 檔頭與 rc_cb_view.h）。
// event_id 只接受 1(PLAY_STATUS_CHANGE)/2(TRACK_CHANGE)/5(PLAY_POS_CHANGED)，其餘回 false。
// changed=false 送 INTERIM；true 送 CHANGED（送完會把 btc 的 registered 旗標清掉，車機收到
// 後會重新註冊）。track8：TRACK_CHANGE 用的 8 bytes UID；play_status：PLAY_STATUS_CHANGE
// 用；play_pos：PLAY_POS_CHANGED 用（ms）；不相關的參數會被忽略。
// 回傳是否真的送出（車機沒註冊/結構對不上/組包失敗都回 false，不會丟例外或當掉）。
bool avrc_rn_send(uint8_t event_id, bool changed, const uint8_t track8[8], uint8_t play_status, uint32_t play_pos);

#endif  // AVRC_META_WRAP_H
