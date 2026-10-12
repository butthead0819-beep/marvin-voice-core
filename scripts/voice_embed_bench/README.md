# voice_embed_bench

離線 benchmark：EmbeddingGemma 2 的 audio encoder 把一句語音轉成向量，跟「預先算好的指令文字向量」做 cosine 比對，
量能不能認出控制指令（skip / pause / resume / stop）、誤觸發率、記憶體與延遲。**不碰 bot 本體。**
測試語音用 edge-tts 合成，再做 Opus 壓縮與加噪音模擬 Discord。

## 在 M1 上的步驟

```bash
python3 -m venv venv_embed && source venv_embed/bin/activate
pip install "sentence-transformers[audio]>=6.1.0" torch edge-tts numpy
# 模型若需授權：huggingface-cli login 後到模型頁接受授權
# 另需 ffmpeg（brew install ffmpeg）

python -m scripts.voice_embed_bench.corpus            # 可加 --transcripts-db <sqlite> 混入真實閒聊逐字稿
python -m scripts.voice_embed_bench.synth_tts         # edge-tts 合成 → clean 條件（可續跑）
python -m scripts.voice_embed_bench.augment           # opus / noise{20,10,5}_opus，可加 --noise-dir
python -m scripts.voice_embed_bench.bench             # 跑 benchmark，輸出 report
```

## 真人錄音（`--real-dir`）

`<real-dir>/labels.csv`，欄位 `file,label,kind`：

```csv
file,label,kind
a01.wav,skip,real
a02.wav,,chat
```

`label` 空字串＝反例（不該觸發）；`kind` 空＝`real`。`file` 相對於 `<real-dir>`。
錄音須照 `validation/RECORDING_SETUP.md` 取得同意。

## 輸出

全部在 `records/voice_embed_bench/`（`records/` 已 gitignore）。
含逐字稿衍生資料，**不要提交**。`results/` 下有 `results.json`、`predictions.jsonl`、`report.md`。

## 怎麼讀 report

- 重點看 `noise*_opus` 與 `real` 這幾列的 **recall@FPR≤1%**（誤觸發壓在 1% 以內時抓得到幾成指令）。
- TTS 合成語音偏乾淨、發音一致，結果偏樂觀，**以 real 為準**。
- `thr@FPR…` 是達成該 FPR 上限時 recall 最高的最低門檻，可當實際部署門檻的參考。
- 另看 latency 與 RSS，確認 M1 8GB 吃得下。
