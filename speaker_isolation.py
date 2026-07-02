"""防線② 跨人記憶隔離 — 記憶注入名單的唯一建構點。

Tier 4 失效模式：A 的 per-person 記憶被 Marvin 對全房說出來——
發生一次就結案的信任事故。防線核心不變量：

    **prompt 記憶注入的名單 = 當前 speaker + 此刻在場的人，僅此而已。**

這條不變量原本是 gemini_router_llm 裡的一行 inline 代碼
（`target_speakers = [speaker] + online_members`），refactor 改壞不會有
任何測試爆。升格為有名字的函式後，tests/test_speaker_isolation.py
的 I1-I4 invariant 測試守著它。

相關既有機制（審計 2026-07-02，勿重造）：
  - VectorStore.search：chroma where 硬過濾 speaker+guild
  - suki_memory shareable flag：callback 的私密/可分享閘
  - recall_handler：task 查詢 speaker-scoped
"""
from __future__ import annotations


def present_speakers(speaker: str, online_members=None) -> list[str]:
    """記憶注入名單：當前 speaker 排首位 + 在場成員，去重、濾空。

    任何要把 per-person 記憶放進 prompt 的路徑都必須用這個名單——
    不在名單內的人的記憶不得出現在 prompt 裡。
    """
    out: list[str] = [speaker]
    for m in online_members or []:
        if m and m != speaker and m not in out:
            out.append(m)
    return out
