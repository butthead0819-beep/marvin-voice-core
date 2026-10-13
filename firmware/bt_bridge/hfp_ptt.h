// hfp_ptt.h — HFP AG（扮手機）實驗：方向盤語音鍵長按 → BVRA → 通知 S3
//
// 這是實驗，不是正式功能。見 bt_bridge.ino 檔頭說明。
#ifndef HFP_PTT_H
#define HFP_PTT_H

#include <stdint.h>

// setup() 在 a2dp_source.start() 之後呼叫一次。
void hfp_ptt_init();

// loop() 處理 g_connected_evt 時呼叫，傳入剛連上的 A2DP 對方位址。
void hfp_ptt_on_a2dp_connected(const uint8_t bda[6]);

// loop() 每圈呼叫：排出事件佇列、印 log、記 DIAG、通知 S3、SCO 安全逾時。
void hfp_ptt_poll();

#endif  // HFP_PTT_H
