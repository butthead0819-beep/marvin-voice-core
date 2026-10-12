"""語音向量指令比對 benchmark：音訊向量 vs 預先算好的指令文字向量做 cosine。

量 top1、門檻掃描（recall @ FPR≤1%/5%）、延遲與記憶體。重依賴（torch /
sentence_transformers）一律 lazy import，run_bench 可用假 encoder 測。

CLI：python -m scripts.voice_embed_bench.bench [--root ...] [--model ...]
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import platform
import resource
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Protocol

import numpy as np

from scripts.voice_embed_bench import audio_io, metrics
from scripts.voice_embed_bench.audio_io import SR
from scripts.voice_embed_bench.corpus import ACTIONS

THRESHOLDS = np.round(np.arange(0, 1.0001, 0.01), 2)


class Encoder(Protocol):
    """回傳值皆須 L2 normalized。"""

    def encode_texts(self, texts: list[str]) -> np.ndarray: ...

    def encode_audio(self, audio: np.ndarray, sr: int) -> np.ndarray:
        """單句音訊 → 1D 向量。"""
        ...


class EmbeddingGemmaEncoder:
    def __init__(self, model_id: str = "google/embeddinggemma-2", device: str | None = None,
                 text_prompt: str = "document", dim: int | None = None):
        import sentence_transformers
        import torch
        from sentence_transformers import SentenceTransformer

        if device is None:
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model_id = model_id
        self.device = device
        self.text_prompt = text_prompt
        self.dim = dim
        self._torch_version = torch.__version__
        self._st_version = sentence_transformers.__version__
        try:
            self.model = SentenceTransformer(
                model_id, device=device, truncate_dim=dim,
                config_kwargs={"vision_config": None},
            )
            self.vision_disabled = True
        except Exception as e:  # noqa: BLE001
            logging.warning("停用 vision_config 載入失敗，改用預設載入：%s", e)
            self.model = SentenceTransformer(model_id, device=device, truncate_dim=dim)
            self.vision_disabled = False

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        if self.text_prompt == "document":
            emb = self.model.encode_document(texts, normalize_embeddings=True)
        elif self.text_prompt == "query":
            emb = self.model.encode_query(texts, normalize_embeddings=True)
        else:
            emb = self.model.encode(texts, normalize_embeddings=True)
        return np.asarray(emb, dtype=np.float32)

    def encode_audio(self, audio: np.ndarray, sr: int) -> np.ndarray:
        emb = self.model.encode([{"array": audio, "sampling_rate": sr}],
                                normalize_embeddings=True)[0]
        return np.asarray(emb, dtype=np.float32)

    def info(self) -> dict:
        return {
            "model_id": self.model_id,
            "device": self.device,
            "text_prompt": self.text_prompt,
            "dim": self.dim,
            "vision_disabled": self.vision_disabled,
            "torch": self._torch_version,
            "sentence_transformers": self._st_version,
        }


def peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 單位是 bytes，Linux 是 KB
    return rss / 1024 / 1024 if sys.platform == "darwin" else rss / 1024


def load_real(real_dir: Path) -> list[dict]:
    """讀真人錄音 labels.csv（欄位 file,label,kind）。"""
    real_dir = Path(real_dir)
    clips = []
    with open(real_dir / "labels.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            clips.append({
                "clip_id": f"real-{row['file']}",
                "item_id": None,
                "text": None,
                "label": row["label"] or None,
                "kind": row["kind"] or "real",
                "voice": None,
                "condition": "real",
                "path": str(real_dir / row["file"]),
            })
    return clips


def _default_load_audio(path) -> tuple[np.ndarray, int]:
    audio, sr = audio_io.read_wav(path)
    if sr != SR:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "r.wav"
            audio_io.ffmpeg_to_wav16k(path, tmp)
            audio, sr = audio_io.read_wav(tmp)
    return audio, sr


def run_bench(encoder: Encoder, anchors: dict[str, list[str]], clips: list[dict],
              root: Path, warmup: int = 3, load_audio=None) -> dict:
    """load_audio(path) -> (audio, sr)；相對 path 以 root 為基準。"""
    load_audio = load_audio or _default_load_audio
    root = Path(root)

    anchor_texts, anchor_labels = [], []
    for action in ACTIONS:
        for t in anchors.get(action, []):
            anchor_texts.append(t)
            anchor_labels.append(action)
    A = encoder.encode_texts(anchor_texts)

    rows = []  # (clip, pred, best)
    latencies: list[float] = []
    for i, clip in enumerate(clips):
        p = Path(clip["path"])
        audio, sr = load_audio(p if p.is_absolute() else root / p)
        t0 = time.perf_counter()
        emb = encoder.encode_audio(audio, sr)
        dt = time.perf_counter() - t0
        if i >= warmup:
            latencies.append(dt)
        scores = A @ emb
        preds, best = metrics.predict(scores[None, :], anchor_labels)
        rows.append((clip, preds[0], float(best[0])))

    by_cond: dict[str, list] = defaultdict(list)
    for r in rows:
        by_cond[r[0]["condition"]].append(r)

    conditions = {}
    for cond, group in by_cond.items():
        labels = [c["label"] for c, _, _ in group]
        preds = [p for _, p, _ in group]
        best = np.array([b for _, _, b in group])
        kinds = [c["kind"] for c, _, _ in group]
        sw = metrics.sweep(labels, preds, best, THRESHOLDS)
        op1 = metrics.operating_point(sw, 0.01)
        op5 = metrics.operating_point(sw, 0.05)

        top1_by_kind = {}
        for kind in sorted({k for k, lb in zip(kinds, labels) if lb is not None}):
            idx = [j for j, (k, lb) in enumerate(zip(kinds, labels))
                   if k == kind and lb is not None]
            top1_by_kind[kind] = metrics.top1([labels[j] for j in idx], [preds[j] for j in idx])

        fpr_by_kind = {}
        if op1:
            for kind in sorted({k for k, lb in zip(kinds, labels) if lb is None}):
                idx = [j for j, (k, lb) in enumerate(zip(kinds, labels))
                       if k == kind and lb is None]
                fpr_by_kind[kind] = sum(best[j] >= op1["threshold"] for j in idx) / len(idx)

        conditions[cond] = {
            "n_pos": sum(lb is not None for lb in labels),
            "n_neg": sum(lb is None for lb in labels),
            "top1": metrics.top1(labels, preds),
            "sweep": sw,
            "op_fpr1": op1,
            "op_fpr5": op5,
            "top1_by_kind": top1_by_kind,
            "fpr_by_kind_at_op1": fpr_by_kind,
        }

    return {
        "anchors": {"n": len(anchor_texts)},
        "latency_ms": {
            "p50": metrics.percentile_ms(latencies, 50),
            "p95": metrics.percentile_ms(latencies, 95),
            "mean": float(np.mean(latencies) * 1000) if latencies else None,
            "n": len(latencies),
        },
        "conditions": conditions,
        "predictions": [
            {"clip_id": c["clip_id"], "condition": c["condition"], "kind": c["kind"],
             "label": c["label"], "pred": p, "best": b}
            for c, p, b in rows
        ],
    }


def _f(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def render_report(results: dict) -> str:
    lines = ["# Voice embed bench 報告", ""]
    env = results.get("env")
    if env:
        lines.append("## 環境")
        lines += [f"- {k}: {v}" for k, v in env.items()]
        lines.append("")
    if "load_s" in results:
        lines.append(f"- 模型載入：{_f(results['load_s'])} s")
    rss = results.get("rss_mb")
    if rss:
        lines.append("- RSS (MB)：" + "、".join(f"{k}={_f(v)}" for k, v in rss.items()))
    lat = results["latency_ms"]
    lines.append(f"- 延遲 (ms)：p50={_f(lat['p50'])} p95={_f(lat['p95'])} "
                 f"mean={_f(lat['mean'])} n={lat['n']}")
    lines += [
        "",
        "| condition | n_pos | n_neg | top1 | thr@FPR≤1% | recall@FPR≤1% "
        "| thr@FPR≤5% | recall@FPR≤5% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cond, c in results["conditions"].items():
        o1, o5 = c["op_fpr1"], c["op_fpr5"]
        lines.append(
            f"| {cond} | {c['n_pos']} | {c['n_neg']} | {_f(c['top1'])} "
            f"| {_f(o1 and o1['threshold'])} | {_f(o1 and o1['recall'])} "
            f"| {_f(o5 and o5['threshold'])} | {_f(o5 and o5['recall'])} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="records/voice_embed_bench")
    ap.add_argument("--model", default="google/embeddinggemma-2")
    ap.add_argument("--device", default=None)
    ap.add_argument("--text-prompt", choices=["document", "query", "none"], default="document")
    ap.add_argument("--dim", type=int, default=None)
    ap.add_argument("--conditions", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--real-dir", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from scripts.voice_embed_bench.corpus import load_anchors

    root = Path(args.root)
    out = Path(args.out) if args.out else root / "results"

    rss_base = peak_rss_mb()
    t0 = time.perf_counter()
    encoder = EmbeddingGemmaEncoder(args.model, args.device, args.text_prompt, args.dim)
    load_s = time.perf_counter() - t0
    rss_after_load = peak_rss_mb()

    clips: list[dict] = []
    for name in ("clips_clean.jsonl", "clips_aug.jsonl"):
        p = root / name
        if p.exists():
            with open(p, encoding="utf-8") as f:
                clips += [json.loads(line) for line in f if line.strip()]
    if args.real_dir:
        clips += load_real(Path(args.real_dir))
    if args.conditions:
        wanted = set(args.conditions.split(","))
        clips = [c for c in clips if c["condition"] in wanted]
    if args.limit is not None:
        seen: dict[str, int] = defaultdict(int)
        kept = []
        for c in clips:
            if seen[c["condition"]] < args.limit:
                seen[c["condition"]] += 1
                kept.append(c)
        clips = kept

    results = run_bench(encoder, load_anchors(), clips, root)
    results["env"] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        **encoder.info(),
    }
    results["load_s"] = load_s
    results["rss_mb"] = {"baseline": rss_base, "after_load": rss_after_load,
                         "peak": peak_rss_mb()}

    out.mkdir(parents=True, exist_ok=True)
    predictions = results.pop("predictions")
    (out / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out / "predictions.jsonl", "w", encoding="utf-8") as f:
        for p in predictions:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    report = render_report(results)
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
