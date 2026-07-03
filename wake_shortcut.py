"""Wake 後 fastpath 入隊前短路 — 完整指令不排隊（2026-07-03）。

問題（當晚現行犯）：fastpath 掛在 worker 內，音樂指令排在同 speaker 前一句
聊天回覆後面（晚間 LLM 降級一句可卡 60s+），26s 觸發 Stale Drop 被丟——
fastpath 0ms 能答的事連被問到的機會都沒有。wakeless T0 早就是佇列外直派
（實測 7s 到播歌），wake 路徑卻要排隊＝結構不對稱。

shortcut_query：喚醒句剝完喚醒詞後若是「完整、可直派」的指令
（歌表命中 / 控制指令），回改寫後 query 讓 caller 直接 dispatch IntentBus，
跳過 query_queue/worker/確認流。聊天、問句、不完整指令回 None 照走 worker。
"""
from __future__ import annotations


def shortcut_query(fp, stripped: str) -> str | None:
    """完整指令 → 改寫後 query（直派 bus 用）；其他 → None（走 worker）。

    與 wakeless T0 同判定來源（music_fastpath / command_fastpath），
    確定性同級——wakeless 敢直派的，wake 沒理由要排隊。
    """
    if not stripped or not stripped.strip():
        return None
    # 歌表拼音命中 → 直解成播放指令（fastpath_play_query 未命中會原樣回）
    if fp is not None:
        from music_fastpath import fastpath_play_query
        q = fastpath_play_query(fp, stripped)
        if q != stripped:
            return q
    # 糊字控制指令（下一手→下一首）
    from command_fastpath import normalize_command
    cmd = normalize_command(stripped)
    if cmd:
        return cmd
    return None


SERVED_WINDOW_S = 20.0   # debounce 晚關窗最長觀測 ~8s，抓寬到 20s


def served_recently(mark, raw_text: str, *, now: float,
                    window_s: float = SERVED_WINDOW_S) -> bool:
    """這句是否已被 shortcut 服務過（wakeless 救援據此讓路，防重複派發）。

    首命中實戰（2026-07-03 23:20）：使用者點歌後嘴巴沒停，debounce 8s 後
    關窗，wakeless 救援不知道 shortcut 已服務 → 同句重派兩次（多 ack、白搜）。
    判定：mark 的 query 是當前 raw_text 的子字串（尾巴贅語不影響）且在窗內。
    """
    if not mark or not raw_text:
        return False
    query, ts = mark
    if now - ts > window_s:
        return False
    return bool(query) and query in raw_text
