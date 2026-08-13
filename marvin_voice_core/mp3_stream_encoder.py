"""/audio_stream 車載端點專用即時 MP3 編碼包裝。

把 mixer 原始 PCM frame 流轉成 MP3 bytes 流，只套用在 /audio_stream 這個 HTTP 端點的
輸出路徑（見 main_satellite.py::handle_audio_stream），不動 StreamSpeakerOutput 本身
——Pi 喇叭 ALSA 輸出、Discord 語音輸出等其他消費者拿到的仍是未壓縮 frame，互不影響。

動機：Funnel+熱點實測 throughput 只有 ~60KB/s，遠低於未壓縮 stereo 48k 需要的
187.5KB/s（見 project_car_puck_funnel_tls_and_fallback_2026-07-26）。128kbps MP3
只需要 ~16KB/s，家用WiFi/熱點兩條路徑都套用同一份編碼，firmware 只需維護一套解碼
路徑（arduino-libhelix），不用為兩條網路路徑各寫一份格式。
"""
from __future__ import annotations

import lameenc


class Mp3StreamEncoder:
    def __init__(self, *, rate: int = 48000, channels: int = 2,
                 bitrate_kbps: int = 128, quality: int = 2):
        self._encoder = lameenc.Encoder()
        self._encoder.set_bit_rate(bitrate_kbps)
        self._encoder.set_in_sample_rate(rate)
        # ⚠️ 2026-08-13 實機踩到：LAME bitrate 低於門檻（128kbps沒事，96kbps會觸發）時會
        # 自作主張把輸出取樣率從48kHz降到32kHz省位元——firmware端I2S輸出寫死48kHz播放，
        # 吃到32kHz的PCM會變成1.5倍速+音調拉高（實機聽感：整首歌像卡通配音）。顯式鎖住
        # 輸出取樣率=輸入取樣率，不讓LAME自動降。
        self._encoder.set_out_sample_rate(rate)
        self._encoder.set_channels(channels)
        self._encoder.set_quality(quality)
        self._started = False   # lameenc.flush() 在從沒 encode() 過時會拋 RuntimeError

    def encode(self, pcm: bytes) -> bytes:
        """餵 s16 PCM bytes，回傳目前累積夠的 MP3 bytes（可能是空——lame 內部要湊滿
        一個 MP3 frame 的樣本數才會吐資料，短輸入很正常拿到空 bytes）。"""
        if not pcm:
            return b""
        self._started = True
        return bytes(self._encoder.encode(pcm))

    def flush(self) -> bytes:
        """串流結束時呼叫一次，把 lame 內部殘留、還沒湊滿一個 frame 的樣本吐乾淨。
        從沒餵過資料就 close（例如剛連線就斷）→ 底層 lameenc 對「沒 encode 過」的
        flush() 會直接拋 RuntimeError("Not currently encoding")，這裡短路避開。"""
        if not self._started:
            return b""
        return bytes(self._encoder.flush())
