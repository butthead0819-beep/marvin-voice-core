"""多人自動推薦的種子輪替（純函式，無 IO）。

設計（見對話 2026-06-23）：在場每人都要影響種子走向，不被單一人霸佔。
- round-robin 主種子者：每 swap_every 首換下一位在場者當主種子 → 用他的種子餵 radio。
- 最後手動點歌者：當 fresh lead 種子，swap_every 首內有效、之後淡出（None）。
- 永遠混入其他在場者：每人先貢獻 1 顆種子，確保混合（showay 在也不會整晚台語）。

state（epoch / since_manual）由 cog 持有並遞增；本模組只做純排序，方便 TDD。
"""
from __future__ import annotations


def primary_member(members: list[str], epoch: int, swap_every: int = 3) -> str | None:
    """round-robin 主種子者。epoch=自開播以來 auto-rec 計數；每 swap_every 首換人。"""
    if not members:
        return None
    return members[(epoch // swap_every) % len(members)]


def order_rotating_seeds(
    members: list[str],
    seeds_by_member: dict[str, list[str]],
    *,
    epoch: int,
    since_manual: int,
    last_seed: str | None,
    swap_every: int = 3,
    n: int = 3,
) -> list[str]:
    """回傳排序後的種子（≤n）：fresh 手動 lead + 主種子者 + 混入其他在場者。

    - since_manual < swap_every 且有 last_seed → last_seed 當 lead（之後淡出）。
    - 主種子者 = round-robin(members, epoch)。
    - 其餘在場者依序各貢獻 1 顆 → 確保混合、無人霸佔。
    去重後取前 n。
    """
    if not members:
        return [last_seed] if last_seed else []
    out: list[str] = []
    if last_seed and since_manual < swap_every:
        out.append(last_seed)
    primary = primary_member(members, epoch, swap_every)
    order = [primary] + [m for m in members if m != primary]
    for m in order:
        for vid in seeds_by_member.get(m, []):
            if vid not in out:
                out.append(vid)
                break  # 每人先貢獻 1 顆，確保混合
        if len(out) >= n:
            break
    return out[:n]
