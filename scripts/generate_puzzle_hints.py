#!/usr/bin/env python3
"""海龜湯題目作者工具：產出 hint 編織網（節點 + 提示 + 揭露關係）。

工作流（top-down 抽節點 → bottom-up 組提示）：
    1. 作者在 puzzles.py 寫好 Puzzle（含 surface / truth / key_facts / leak_keywords）
    2. 跑此腳本：python scripts/generate_puzzle_hints.py <puzzle_id> [-n N]
    3. LLM 輸出 hint_nodes + hints（含 reveals）
    4. 作者人工檢視（網結構視覺化在 CLI 印出）後挑選 / 改寫
    5. 貼回 puzzles.py 對應 Puzzle 的 hint_nodes 與 hints 欄位

用法：
    python scripts/generate_puzzle_hints.py                # 跑 ELEVATOR_18F
    python scripts/generate_puzzle_hints.py elevator_18f
    python scripts/generate_puzzle_hints.py --list
    python scripts/generate_puzzle_hints.py -n 3           # 跑 3 次取最好
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
    found = {}
    for name in dir(puzzles):
        obj = getattr(puzzles, name)
        if isinstance(obj, puzzles.Puzzle):
            found[obj.id] = obj
    return found


def _print_graph(graph: dict, run_idx: int = 1):
    print(f"\n────── Run #{run_idx} ({graph['_provider']}) ──────")
    print("\n  📐 hint_nodes（推理鏈節點）")
    for n in graph["hint_nodes"]:
        kws = n.get("keywords", [])
        kw_str = f"  keywords={list(kws)}" if kws else "  (no keywords)"
        print(f"     [{n['id']:18s}] {n['fact']}")
        print(f"     {' ':18s}  {kw_str}")

    node_index = {n["id"]: i for i, n in enumerate(graph["hint_nodes"])}

    print("\n  💡 hints（提示 + 節點覆蓋網）")
    # 印 header（節點 id 縮寫）
    headers = [n["id"][:3] for n in graph["hint_nodes"]]
    print(f"     {'    ':>6s}  {' '.join(h.rjust(3) for h in headers)}")
    for i, h in enumerate(graph["hints"], 1):
        revealed_indices = {node_index.get(rid) for rid in h["reveals"] if rid in node_index}
        coverage = " ".join(
            (" ■ " if j in revealed_indices else " · ")
            for j in range(len(graph["hint_nodes"]))
        )
        print(f"     [{i}]    {coverage}  ({len(h['reveals'])}/{len(graph['hint_nodes'])}) {h['text']}")


def _print_paste_block(graph: dict):
    print("\n" + "=" * 72)
    print("如果你滿意，把下面這兩段貼進 puzzles.py 對應 Puzzle：")
    print("=" * 72)
    print()
    print("    hint_nodes=[")
    for n in graph["hint_nodes"]:
        kws = n.get("keywords", [])
        if kws:
            kw_repr = ", ".join(repr(k) for k in kws)
            print(f"        HintNode(")
            print(f"            id={n['id']!r},")
            print(f"            fact={n['fact']!r},")
            print(f"            keywords=({kw_repr},),")
            print(f"        ),")
        else:
            print(f"        HintNode(id={n['id']!r}, fact={n['fact']!r}),")
    print("    ],")
    print("    hints=[")
    for h in graph["hints"]:
        reveals_repr = ", ".join(repr(r) for r in h["reveals"])
        print(f"        Hint(")
        print(f"            text={h['text']!r},")
        print(f"            reveals=({reveals_repr},),")
        print(f"        ),")
    print("    ],")
    print("=" * 72)


async def run_one(puzzle: puzzles.Puzzle, run_idx: int = 1) -> dict:
    result = await hint_generator.generate_hint_graph(
        surface=puzzle.surface,
        truth=puzzle.truth,
        key_facts=list(puzzle.key_facts),
        leak_keywords=list(puzzle.leak_keywords),
    )
    _print_graph(result, run_idx=run_idx)
    return result


async def main():
    parser = argparse.ArgumentParser(description="海龜湯 hint 編織網 generator")
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
    print("=" * 72)
    print(f"📜 題目：{puzzle.id}")
    print("=" * 72)
    print(f"\n【湯面】\n{puzzle.surface}\n")
    print(f"【湯底】\n{puzzle.truth}\n")
    print(f"【key_facts】")
    for kf in puzzle.key_facts:
        print(f"  - {kf}")
    print(f"\n【leak_keywords】\n  {puzzle.leak_keywords}")

    runs = []
    for i in range(args.n):
        r = await run_one(puzzle, run_idx=i + 1)
        runs.append(r)
        if r["_provider"] == "fallback":
            print("  ⚠️  全部 LLM provider 失敗，自行處理")

    if args.n == 1:
        if runs[0]["_provider"] != "fallback":
            _print_paste_block(runs[0])
    else:
        print("\n" + "=" * 72)
        print(f"已產生 {args.n} 組候選。挑你最滿意的一組（可混合）後貼回 puzzles.py")
        print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
