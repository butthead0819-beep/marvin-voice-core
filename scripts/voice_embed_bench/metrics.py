"""純 numpy 指標：預測、門檻掃描、operating point、延遲百分位。"""
from __future__ import annotations

import numpy as np


def predict(scores: np.ndarray, anchor_labels: list[str]) -> tuple[list[str], np.ndarray]:
    """scores (n_clips, n_anchors)：每列 argmax 的 anchor label 與最大值。"""
    scores = np.asarray(scores)
    idx = scores.argmax(axis=1)
    return [anchor_labels[i] for i in idx], scores.max(axis=1)


def sweep(labels: list[str | None], preds: list[str], best: np.ndarray,
          thresholds: np.ndarray) -> list[dict]:
    pos = np.array([lb is not None for lb in labels], dtype=bool)
    correct = np.array([lb is not None and lb == p for lb, p in zip(labels, preds)], dtype=bool)
    best = np.asarray(best)
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    rows = []
    for t in thresholds:
        hit = best >= t
        rows.append({
            "threshold": float(t),
            "recall": int((pos & hit & correct).sum()) / n_pos if n_pos else None,
            "wrong_action": int((pos & hit & ~correct).sum()) / n_pos if n_pos else None,
            "fpr": int((~pos & hit).sum()) / n_neg if n_neg else None,
        })
    return rows


def top1(labels: list[str | None], preds: list[str]) -> float | None:
    pos = [(lb, p) for lb, p in zip(labels, preds) if lb is not None]
    if not pos:
        return None
    return sum(lb == p for lb, p in pos) / len(pos)


def operating_point(sweep_rows: list[dict], max_fpr: float) -> dict | None:
    """fpr ≤ max_fpr 的列中取 recall 最高；同分取 threshold 最低。fpr 為 None 不列入。"""
    ok = [r for r in sweep_rows
          if r["fpr"] is not None and r["fpr"] <= max_fpr and r["recall"] is not None]
    if not ok:
        return None
    return max(ok, key=lambda r: (r["recall"], -r["threshold"]))


def percentile_ms(seconds: list[float], p: float) -> float | None:
    if not seconds:
        return None
    return float(np.percentile(seconds, p) * 1000)
