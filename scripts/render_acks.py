"""統一 ack 渲染器 — 掃 ack_templates.POOLS 補缺檔（已存在跳過）。

取代 generate_acks / generate_acks_en / generate_music_acks /
generate_music_fail_acks / render_status_acks 五支腳本。加新 ack 只要在
ack_templates.py 的 POOLS / CATEGORIES 加宣告，再跑這支即可。

用 SukiTTS（與舊腳本一致，英文台詞自動切 en-GB-RyanNeural）。
重跑只補缺檔；--force 全部重渲。--pool <key> 只渲指定 pool。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import ack_templates as A  # noqa: E402

try:
    from tts_engine import SukiTTS
except ImportError:
    print("❌ 錯誤：找不到 tts_engine.py。")
    sys.exit(1)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


async def render(force: bool, only_pool: str | None) -> None:
    engine = SukiTTS()
    made, skipped, failed = 0, 0, 0

    for pool in A.POOLS.values():
        if only_pool and pool.key != only_pool:
            continue
        out_dir = os.path.join(ROOT, pool.directory)
        os.makedirs(out_dir, exist_ok=True)
        print(f"\n🎙️  pool={pool.key} → {pool.directory}（{len(pool.items)} 條）")

        for text, filename in pool.items:
            save_path = os.path.join(out_dir, filename)
            if not force and os.path.exists(save_path) and os.path.getsize(save_path) > 100:
                skipped += 1
                continue
            temp_file = await engine.generate_audio(text)
            if temp_file and os.path.exists(temp_file):
                if os.path.exists(save_path):
                    os.remove(save_path)
                shutil.move(temp_file, save_path)
                made += 1
                print(f"  ✅ {filename}：「{text}」")
            else:
                failed += 1
                print(f"  ❌ {filename} 生成失敗")

    print(f"\n完成：新生 {made}、跳過 {skipped}、失敗 {failed}。")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="全部重渲（預設只補缺檔）")
    ap.add_argument("--pool", default=None, help="只渲指定 pool key")
    args = ap.parse_args()
    asyncio.run(render(args.force, args.pool))


if __name__ == "__main__":
    main()
