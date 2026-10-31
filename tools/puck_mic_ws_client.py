"""端到端測試客戶端 — 取代韌體，模擬 puck 連 /mic_ws 送 ADPCM、驗證 PuckMicSession。

用法：
  venv_simon/bin/python tools/puck_mic_ws_client.py <ws_url> <audio> [--device ID] [--token T]
    [--blocks-per-msg 8] [--start S] [--duration D]

讀音檔（wav 或 mp3，重取樣到 16k mono）→ IMA ADPCM 編碼 → 每 --blocks-per-msg 個
block 併成一則 binary message，照真實時間送（絕對時鐘排程，不是单純 sleep 累積漂移）。
另開一個 task 收 server 訊息（duck/unduck），印出音訊時間軸對應的收到時間。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ima_adpcm import BLOCK_SAMPLES, ImaAdpcmEncoder
from puck_vad_replay import load_audio_as_16k_mono


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("ws_url")
    p.add_argument("audio")
    p.add_argument("--device", default=None)
    p.add_argument("--token", default=None)
    p.add_argument("--blocks-per-msg", type=int, default=8)
    p.add_argument("--start", type=float, default=None)
    p.add_argument("--duration", type=float, default=None)
    return p.parse_args()


def _build_url(ws_url: str, device: str | None, token: str | None) -> str:
    params = []
    if device:
        params.append(f"device={device}")
    if token:
        params.append(f"t={token}")
    if not params:
        return ws_url
    sep = "&" if "?" in ws_url else "?"
    return ws_url + sep + "&".join(params)


async def _recv_loop(ws, t_start: float) -> None:
    async for msg in ws:
        from aiohttp import WSMsgType
        if msg.type == WSMsgType.TEXT:
            t = time.monotonic() - t_start
            print(f"[{t:7.2f}s] RECV {msg.data}")
        elif msg.type == WSMsgType.CLOSE:
            break


async def main() -> None:
    args = parse_args()
    pcm = load_audio_as_16k_mono(args.audio, args.start, args.duration)
    total_samples = len(pcm)

    n_blocks = (total_samples + BLOCK_SAMPLES - 1) // BLOCK_SAMPLES
    encoder = ImaAdpcmEncoder()

    import numpy as np
    from aiohttp import ClientSession

    url = _build_url(args.ws_url, args.device, args.token)
    print(f"連線: {url}")

    async with ClientSession() as session:
        async with session.ws_connect(url) as ws:
            t_start = time.monotonic()
            recv_task = asyncio.create_task(_recv_loop(ws, t_start))

            msg_buf = bytearray()
            blocks_in_msg = 0
            msg_index = 0

            for i in range(n_blocks):
                chunk = pcm[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES]
                if len(chunk) < BLOCK_SAMPLES:
                    chunk = np.pad(chunk, (0, BLOCK_SAMPLES - len(chunk)))
                msg_buf += encoder.encode_block(chunk)
                blocks_in_msg += 1

                if blocks_in_msg >= args.blocks_per_msg or i == n_blocks - 1:
                    due = t_start + (msg_index + 1) * args.blocks_per_msg * BLOCK_SAMPLES / 16000.0
                    lag = due - time.monotonic()
                    if lag > 0:
                        await asyncio.sleep(lag)
                    await ws.send_bytes(bytes(msg_buf))
                    msg_index += 1
                    msg_buf = bytearray()
                    blocks_in_msg = 0

            print(f"送完 {n_blocks} blocks（{total_samples / 16000.0:.1f}s 音訊），等 15 秒收尾訊息")
            await asyncio.sleep(15.0)
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
