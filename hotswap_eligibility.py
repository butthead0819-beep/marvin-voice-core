"""hotswap_eligibility — 決定一段 TTS 是否短到可走中途熱切換注入（Plan 11 Slice 3）。

中途熱切換成本高（背景起第二條 stream + loudnorm 量測），接縫只在「短句 + 低音量 +
ducking onset」遮掩下可接受（seam test 證實）。所以只有夠短、單行的即時 ack 才走，
其餘維持原本「串流中靜音 / 貼文」。意圖白名單由呼叫端 opt-in（傳 allow_hotswap=True），
這個純函式只管長度與 sanity——長句接縫突兀且佔用音樂太久。
"""
from __future__ import annotations

# 走熱切換的最長字數。seam test 在短 ack 下驗證可接受；更長的句子接縫突兀。
MAX_HOTSWAP_CHARS = 12


def is_hotswap_eligible(text: str | None, *, max_chars: int = MAX_HOTSWAP_CHARS) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "\n" in t:  # 多行 = 結構化長回應，不走
        return False
    return len(t) <= max_chars
