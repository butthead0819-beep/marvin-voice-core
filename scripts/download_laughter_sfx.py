#!/usr/bin/env python3
"""
download_laughter_sfx.py — 下載多種情緒的免費開源笑聲/反應音效
轉為 48kHz 16-bit WAV 格式，供 Marvin 混音台與語音表情使用。
"""

import os
import subprocess
import urllib.request
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TARGET_DIR = os.path.abspath("assets/sfx/laughter")
os.makedirs(TARGET_DIR, exist_ok=True)

# 免版稅音效清單 (Mixkit 免費授權 & Wikimedia Commons CC0 / Public Domain)
SOUNDS = [
    {
        "filename": "chuckle_casual_male.wav",
        "url": "https://assets.mixkit.co/active_storage/sfx/2993/2993-preview.mp3",
        "emotion": "厭世乾笑 / 男生隨性笑 (Chuckle)",
        "scenario": "Marvin 專屬吐槽、冷面笑匠接話、無奈冷笑",
    },
    {
        "filename": "snicker_nasal_scorn.wav",
        "url": "https://assets.mixkit.co/active_storage/sfx/107/107-preview.mp3",
        "emotion": "嗤笑 / 鼻音冷哼 (Snicker)",
        "scenario": "抓到朋友痛腳、嘲笑蠢事、輕蔑不屑",
    },
    {
        "filename": "evil_laugh_mischief.wav",
        "url": "https://assets.mixkit.co/active_storage/sfx/423/423-preview.mp3",
        "emotion": "壞笑 / 腹黑暗笑 (Evil Laugh)",
        "scenario": "惡作劇得逞、突然準備放冷門怪歌、計畫通",
    },
    {
        "filename": "giggle_funny.wav",
        "url": "https://assets.mixkit.co/active_storage/sfx/2885/2885-preview.mp3",
        "emotion": "忍不住輕笑 / 偷笑 (Giggle)",
        "scenario": "被逗樂但強忍著、覺得場面太滑稽",
    },
    {
        "filename": "sarcastic_cartoon_laugh.wav",
        "url": "https://assets.mixkit.co/active_storage/sfx/2882/2882-preview.mp3",
        "emotion": "諷刺狂笑 / 誇張嘲諷笑 (Sarcastic Laugh)",
        "scenario": "極致嘲諷、大肆嘲笑某人出糗",
    },
    {
        "filename": "crowd_laughter_cheer.wav",
        "url": "https://assets.mixkit.co/active_storage/sfx/424/424-preview.mp3",
        "emotion": "全場起鬨大笑 (Crowd Laugh)",
        "scenario": "客廳氣氛烘托、營造綜藝效果",
    },
    {
        "filename": "gasp_astonished_male.wav",
        "url": "https://assets.mixkit.co/active_storage/sfx/966/966-preview.mp3",
        "emotion": "倒抽一口氣 / 驚訝倒吸 (Astonished Gasp)",
        "scenario": "聽到離譜八卦、不敢置信、裝震驚",
    },
    {
        "filename": "cackle_classic_evil.wav",
        "url": "https://upload.wikimedia.org/wikipedia/commons/9/96/WAPP-Evil_Laugh.wav",
        "emotion": "經典魔王大笑 (Classic Evil Cackle)",
        "scenario": "萬聖節、整人遊戲、遊戲勝利宣告",
    },
]

def download_and_convert():
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    manifest_lines = [
        "# Marvin 笑聲與副語言音效庫 (Laughter SFX)",
        "",
        "存放各類情緒的免費開源笑聲，皆轉為標準 48kHz, 16-bit PCM WAV（符合 Discord 音訊格式）。",
        "",
        "| 檔案名稱 | 情緒標籤 | 建議使用時機 |",
        "|---|---|---|",
    ]

    for item in SOUNDS:
        target_path = os.path.join(TARGET_DIR, item["filename"])
        temp_input = os.path.join(TARGET_DIR, f"temp_{item['filename']}.tmp")
        logging.info(f"正在下載: {item['emotion']} -> {item['filename']}")

        try:
            req = urllib.request.Request(item["url"], headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp, open(temp_input, "wb") as f:
                f.write(resp.read())

            # 用 ffmpeg 轉成 Discord 語音標準：48000Hz, 16-bit signed, mono, 峰值正規化
            cmd = [
                "ffmpeg", "-y", "-i", temp_input,
                "-ar", "48000",
                "-ac", "2",
                "-c:a", "pcm_s16le",
                "-af", "loudnorm=I=-14:LRA=7:tp=-1.5",
                target_path
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logging.info(f"  ✓ 成功轉換並正規化: {target_path}")

            manifest_lines.append(f"| `{item['filename']}` | **{item['emotion']}** | {item['scenario']} |")

        except Exception as e:
            logging.error(f"  ✗ 處理 {item['filename']} 失敗: {e}")
        finally:
            if os.path.exists(temp_input):
                os.remove(temp_input)

    readme_path = os.path.join(TARGET_DIR, "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write("\n".join(manifest_lines) + "\n")
    logging.info(f"清單文件已產生: {readme_path}")

if __name__ == "__main__":
    download_and_convert()
