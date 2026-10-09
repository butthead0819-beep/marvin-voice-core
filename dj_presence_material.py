"""DJ 串場素材：語音頻道在場成員的 Discord 動態（10/8 使用者定案）。

把「正在玩」(ActivityType.playing) 跟「自訂狀態」(ActivityType.custom) 當成跟
life/interest/news 同等的扭蛋池素材——老朋友會順口提一句你現在在幹嘛。

隱私：只取語音頻道「現在在場」的成員，不是全伺服器線上成員——人離開頻道這份
素材就跟著消失。冷卻沿用 `dj_topic_selector.TopicCooldownStore` 的預設
COOLDOWN_S（8 小時），由呼叫端（`select_mode`）處理：同一款遊戲一場只講一次。
"""
from __future__ import annotations


def presence_materials(members) -> list[str]:
    """把在場成員的 Discord activity 轉成串場素材句子（去重保序）。members=None 或
    迭代時出錯都回 []（fail-open，不擋 DJ 生成）。"""
    try:
        import discord

        if members is None:
            return []

        out: list[str] = []
        for m in members:
            if getattr(m, "bot", False):
                continue
            name = m.display_name
            for act in (getattr(m, "activities", None) or ()):
                if act.type == discord.ActivityType.playing:
                    game = (act.name or "").strip()
                    if game:
                        out.append(f"{name} 正在玩《{game}》")
                elif act.type == discord.ActivityType.custom:
                    text = (act.name or act.state or "").strip()
                    if not text or text == "Custom Status":
                        continue
                    if len(text) > 30:
                        text = text[:30]
                    out.append(f"{name} 的 Discord 狀態寫著「{text}」")

        seen = set()
        deduped = []
        for line in out:
            if line not in seen:
                seen.add(line)
                deduped.append(line)
        return deduped
    except Exception:
        return []
