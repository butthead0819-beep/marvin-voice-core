// avrc_meta_wrap.h — avrc_meta_wrap.cpp 對外介面。見該檔檔頭說明背景與連結旗標。
#ifndef AVRC_META_WRAP_H
#define AVRC_META_WRAP_H

#include <stdint.h>

#include "now_playing.h"

void avrc_meta_set(const NowPlaying& np);
// 加鎖取快照：給其他 task（BT app task 的 TG 事件）讀，避免讀到 loop 寫到一半的資料。
void avrc_meta_get(NowPlaying& out);
uint32_t avrc_meta_hits_elem();
uint32_t avrc_meta_hits_status();

#endif  // AVRC_META_WRAP_H
