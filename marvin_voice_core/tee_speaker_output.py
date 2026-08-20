"""write(frame)/close() 扇出給多個下游 output——讓同一份 mixer 輸出同時餵給多個消費者
（例如家用衛星 WyomingSpeakerOutput + 車 puck StreamSpeakerOutput），不用二選一。

執行緒界線：跟被包住的 output 一樣，由 LocalSpeakerDevice 泵執行緒同步呼叫，這裡不
額外加鎖/跨 loop——純粹逐一轉呼叫，各 output 自己的執行緒安全（call_soon_threadsafe
等）各自負責。

容錯：其中一個 output 的 write()/close() 丟例外不該拖垮其他 output（也不該讓泵執行緒
掛掉）——比照專案既有的優雅降級哲學，單一下游死了照樣繼續送其他下游。"""
from __future__ import annotations


class TeeSpeakerOutput:
    def __init__(self, outputs: list) -> None:
        self._outputs = list(outputs)

    def write(self, frame: bytes) -> None:
        for out in self._outputs:
            try:
                out.write(frame)
            except Exception:   # noqa: BLE001
                pass

    def close(self) -> None:
        for out in self._outputs:
            try:
                out.close()
            except Exception:   # noqa: BLE001
                pass
