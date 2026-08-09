#!/usr/bin/env python3
"""
用 Marvin 現役直播聲線（見 tts_engine.py 的 TTSEngine.__init__ 預設值：
zh-TW-YunJheNeural / rate=-20% / pitch=-15Hz）錄一支 DJ 報名 shout-out stamp，
落地存 assets/dj_sfx/shoutout.wav，供 _play_dj_tail_sfx 疊播（見 cogs/music_cog.py）。
跟 scripts/gen_dj_sfx.py 的合成音效不同——這支是真人聲線 TTS，不是 numpy 合成，
所以要重新錄一份就重跑這支腳本（edge-tts 需要網路）。
Run from repo root: python scripts/gen_dj_shoutout.py
"""
import asyncio
import os

import edge_tts

VOICE = "zh-TW-YunJheNeural"
RATE = "-20%"
PITCH = "-15Hz"
TEXT = "Yo，DJ 馬文！"
OUT_PATH = "assets/dj_sfx/shoutout.wav"


async def main() -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp_mp3 = OUT_PATH + ".tmp.mp3"
    comm = edge_tts.Communicate(text=TEXT, voice=VOICE, rate=RATE, pitch=PITCH)
    await comm.save(tmp_mp3)

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", tmp_mp3, "-ar", "44100", "-ac", "1", OUT_PATH,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    os.remove(tmp_mp3)
    print(f"✅  {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
