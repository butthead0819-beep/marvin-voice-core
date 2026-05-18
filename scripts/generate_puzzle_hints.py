#!/usr/bin/env python3
"""海龜湯題目作者工具：給定題目，產生 1D/2D/3D 三層 hint 候選。

用法：
    python scripts/generate_puzzle_hints.py                # 跑 ELEVATOR_18F（預設）
    python scripts/generate_puzzle_hints.py elevator_18f   # 跑指定題目
    python scripts/generate_puzzle_hints.py -n 3           # 同題跑 3 次取最好

跑完印 3 條候選給作者人工挑選 / 改寫，貼回 puzzles.py。
不會自動寫檔（避免手抖蓋掉手寫精品）。
"""
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from game.turtle_soup import puzzles, hint_generator  # noqa: E402


def _list_puzzles():
    """列出 puzzles 模組裡的所有 Puzzle 物件，回 {id: Puzzle}。"""
    found = {}
    for name in dir(puzzles):
        obj = getattr(puzzles, name)
        if isinstance(obj, puzzles.Puzzle):
            found[obj.id] = obj
    return found


async def run_one(puzzle: puzzles.Puzzle, run_idx: int = 1) -> dict:
    print(f"\n────── Run #{run_idx} ──────")
    result = await hint_generator.generate_hint_tiers(
        surface=puzzle.surface,
        truth=puzzle.truth,
        key_facts=list(puzzle.key_facts),
        leak_keywords=list(puzzle.leak_keywords),
    )
    print(f"  provider: {result['_provider']}")
    print(f"  1D 直接關聯：  {result['direct']}")
    print(f"  2D 二維關聯：  {result['two_dimensional']}")
    print(f"  3D 三維關聯：  {result['three_dimensional']}")
    return result


def _print_paste_block(best: dict):
    print("\n" + "=" * 70)
    print("如果你滿意，把下面這段貼進 puzzles.py 對應 Puzzle 的 hints=[]")
    print("=" * 70)
    print("    hints=[")
    print(f"        # 1D 直接關聯")
    print(f"        {best['direct']!r},")
    print(f"        # 2D 二維關聯")
    print(f"        {best['two_dimensional']!r},")
    print(f"        # 3D 三維關聯")
    print(f"        {best['three_dimensional']!r},")
    print("    ],")
    print("=" * 70)


async def main():
    parser = argparse.ArgumentParser(description="海龜湯 hint generator CLI")
    parser.add_argument("puzzle_id", nargs="?", default="elevator_18f",
                        help="題目 id（預設 elevator_18f）")
    parser.add_argument("-n", type=int, default=1,
                        help="跑幾次取最好（預設 1）")
    parser.add_argument("--list", action="store_true",
                        help="列出 puzzles.py 內所有可用題目")
    args = parser.parse_args()

    available = _list_puzzles()
    if args.list:
        print("可用題目：")
        for pid in sorted(available):
            print(f"  - {pid}")
        return

    if args.puzzle_id not in available:
        print(f"❌ 找不到題目 id={args.puzzle_id!r}")
        print(f"可用：{sorted(available)}")
        sys.exit(1)

    puzzle = available[args.puzzle_id]
    print("=" * 70)
    print(f"📜 題目：{puzzle.id}")
    print("=" * 70)
    print(f"\n【湯面】\n{puzzle.surface}\n")
    print(f"【湯底】\n{puzzle.truth}\n")
    print(f"【key_facts】\n  - " + "\n  - ".join(puzzle.key_facts))
    print(f"\n【leak_keywords】\n  {puzzle.leak_keywords}")

    runs = []
    for i in range(args.n):
        r = await run_one(puzzle, run_idx=i + 1)
        runs.append(r)

    # 如果跑多次，列出所有結果，作者自己挑
    if args.n > 1:
        print("\n" + "=" * 70)
        print(f"已產生 {args.n} 組候選，請挑選你最滿意的一組：")
        print("=" * 70)
        for i, r in enumerate(runs, 1):
            print(f"\n[#{i}] ({r['_provider']})")
            print(f"  1D: {r['direct']}")
            print(f"  2D: {r['two_dimensional']}")
            print(f"  3D: {r['three_dimensional']}")
    else:
        _print_paste_block(runs[0])


if __name__ == "__main__":
    asyncio.run(main())
