import json
import base64

with open('assets/acks/marvin_ref_base64.txt') as f:
    b64_audio = f.read().strip()

nb = {
 "cells": [
  {
   "cell_type": "markdown",
   "metadata": {},
   "source": [
    "# 😄 Marvin 開懷笑聲生成器 (長度 2~3.5 秒・哈哈/哇哈哈/嘻嘻/笑到停不下來)\n",
    "\n",
    "專門解決『笑聲時間太短、不夠開心』的問題！\n",
    "透過長擬聲詞與呼吸標點，讓 F5-TTS 用 Marvin 的聲線完整演繹**具備起伏韻律、真正開懷大笑**的純笑聲音訊：\n",
    "* **爽朗開懷大笑**（哈哈哈哈哈哈！）\n",
    "* **狂放豪邁大笑**（哇哈哈哈哈哈！）\n",
    "* **促狹嬉笑偷笑**（嘻嘻嘻嘻嘻……）\n",
    "* **忍俊不禁笑噴**（噗哈哈哈哈哈！）\n",
    "* **捧腹笑到停不下來**（哈哈哈哈哈，哇哈哈！）\n",
    "* **低沉漸進大笑**（呵呵呵哈哈哈哈……）\n",
    "* **笑到嗆到氣音**（哈哈哈哈咳……哇哈哈！）\n",
    "\n",
    "### ⚡ 一鍵執行步驟：\n",
    "1. 點擊頂部選單 **「執行階段」 -> 「變更執行階段類型」** 確認選 **T4 GPU** 或 **A100 GPU**。\n",
    "2. 點擊 **「執行階段」 -> 「全部執行」 (Run all)**。\n",
    "3. 跑完後在左側下載 `marvin_happy_laughs.zip`！"
   ]
  },
  {
   "cell_type": "markdown",
   "metadata": {},
   "source": [
    "### 1. 安裝 F5-TTS 語音克隆庫與環境"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": None,
   "metadata": {},
   "outputs": [],
   "source": [
    "!pip install -q f5-tts soundfile pyloudnorm torchaudio\n",
    "print('✅ 依賴庫安裝完成！')"
   ]
  },
  {
   "cell_type": "markdown",
   "metadata": {},
   "source": [
    "### 2. 載入 Marvin 原始聲線樣本 (`marvin_ref.wav`)"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": None,
   "metadata": {},
   "outputs": [],
   "source": [
    "import base64\n",
    "import IPython.display as ipd\n",
    "\n",
    f"b64_data = '{b64_audio}'\n",
    "with open('marvin_ref.wav', 'wb') as f:\n",
    "    f.write(base64.b64decode(b64_data))\n",
    "\n",
    "print('✅ Marvin 原始聲音樣本已就緒 (ack_4: 說來聽聽。。。)')\n",
    "ipd.Audio('marvin_ref.wav')"
   ]
  },
  {
   "cell_type": "markdown",
   "metadata": {},
   "source": [
    "### 3. 批次生成 8 款長段開心笑聲（長度 2~3.5 秒）"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": None,
   "metadata": {},
   "outputs": [],
   "source": [
    "import os\n",
    "import torch\n",
    "import soundfile as sf\n",
    "import pyloudnorm as pyln\n",
    "from f5_tts.api import F5TTS\n",
    "\n",
    "device = 'cuda' if torch.cuda.is_available() else 'cpu'\n",
    "print(f'🚀 正在載入 F5-TTS 預訓練模型 (裝置: {device})...')\n",
    "f5tts = F5TTS(device=device)\n",
    "\n",
    "# 8 款長度足夠、情緒開心且多樣化的純笑聲擬聲詞\n",
    "HAPPY_LAUGHS = {\n",
    "    'laugh_01_haha_hearty':        ('哈哈哈哈哈哈！', '爽朗開懷大笑 (~2.5s)'),\n",
    "    'laugh_02_wahaha_boisterous':  ('哇哈哈哈哈哈！', '狂放豪邁大笑 (~2.5s)'),\n",
    "    'laugh_03_xixi_mischievous':   ('嘻嘻嘻嘻嘻……', '促狹嬉笑偷笑 (~2.2s)'),\n",
    "    'laugh_04_burst_haha':         ('噗哈哈哈哈哈！', '忍不住噴笑 (~2.3s)'),\n",
    "    'laugh_05_belly_uncontrollable': ('哈哈哈哈哈，哇哈哈！', '笑到停不下來 (~3.2s)'),\n",
    "    'laugh_06_chuckle_rolling':    ('呵呵呵哈哈哈哈……', '由低沉轉大笑 (~2.8s)'),\n",
    "    'laugh_07_choke_giggle':       ('哈哈哈哈咳……哇哈哈！', '笑到嗆到氣音 (~3.0s)'),\n",
    "    'laugh_08_snicker_haha':       ('哼哼哼哈哈哈哈！', '得意好笑 (~2.5s)'),\n",
    "}\n",
    "\n",
    "out_dir = 'marvin_happy_laughs'\n",
    "os.makedirs(out_dir, exist_ok=True)\n",
    "meter = pyln.Meter(24000)\n",
    "\n",
    "print('\\n🎙️ 開始批次生成 Marvin 專屬開懷長笑聲...')\n",
    "for key, (vocable, desc) in HAPPY_LAUGHS.items():\n",
    "    print(f'  [生成] {key} -> \"{vocable}\" ({desc})')\n",
    "    temp_wav = f'{out_dir}/{key}_raw.wav'\n",
    "    f5tts.infer(\n",
    "        ref_file='marvin_ref.wav',\n",
    "        ref_text='說來聽聽。。。',\n",
    "        gen_text=vocable,\n",
    "        file_wave=temp_wav,\n",
    "    )\n",
    "    \n",
    "    # 響度標準化至 -14 LUFS\n",
    "    data, sr = sf.read(temp_wav)\n",
    "    loudness = meter.integrated_loudness(data)\n",
    "    norm_data = pyln.normalize.loudness(data, loudness, -14.0)\n",
    "    \n",
    "    final_wav = f'{out_dir}/{key}.wav'\n",
    "    sf.write(final_wav, norm_data, sr)\n",
    "    if os.path.exists(temp_wav):\n",
    "        os.remove(temp_wav)\n",
    "    print(f'    ✅ 完成: {final_wav}')\n",
    "\n",
    "# 打包所有笑聲音效\n",
    "!zip -r marvin_happy_laughs.zip marvin_happy_laughs/\n",
    "print('\\n🎉 全部 8 款開懷長笑聲生成完成！請在左側檔案下載 marvin_happy_laughs.zip！')\n"
   ]
  },
  {
   "cell_type": "markdown",
   "metadata": {},
   "source": [
    "### 4. 線上試聽爽朗大笑與哇哈哈"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": None,
   "metadata": {},
   "outputs": [],
   "source": [
    "# 試聽爽朗大笑\n",
    "ipd.Audio('marvin_happy_laughs/laugh_01_haha_hearty.wav')"
   ]
  }
 ],
 "metadata": {
  "language_info": {
   "name": "python"
  }
 },
 "nbformat": 4,
 "nbformat_minor": 2
}

with open('generate_marvin_laughs.ipynb', 'w', encoding='utf-8') as f:
    json.dump(nb, f, indent=1, ensure_ascii=False)

print('Updated generate_marvin_laughs.ipynb for Extended Happy Laughs!')
