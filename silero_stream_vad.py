"""串流 Silero VAD — 逐 512-sample window 跑機率，供全時收音切句器用。

模型：faster-whisper 內附的 silero_vad_v6.onnx（路徑用
faster_whisper.utils.get_assets_path()）。

ONNX 簽章（已實測）：
  inputs:  input [seq_len, 576] float32、h [1,1,128]、c [1,1,128]
  outputs: speech_probs、hn、cn

576 = 64 個 context 取樣（上一個 window 的最後 64 個）+ 512 個新取樣。
"""
from __future__ import annotations

import os

import numpy as np
import onnxruntime

import faster_whisper.utils as _fw_utils

if not hasattr(_fw_utils, "get_assets_path"):
    raise RuntimeError("faster_whisper.utils.get_assets_path 不存在，請停下來確認版本")


class SileroStreamVAD:
    WINDOW = 512
    CONTEXT = 64

    def __init__(self, model_path: str | None = None) -> None:
        if model_path is None:
            model_path = os.path.join(_fw_utils.get_assets_path(), "silero_vad_v6.onnx")

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.log_severity_level = 4

        self._session = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"], sess_options=opts
        )

        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT, dtype=np.float32)
        self._residual = np.empty(0, dtype=np.int16)

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT, dtype=np.float32)
        self._residual = np.empty(0, dtype=np.int16)

    def process(self, pcm16: np.ndarray) -> list[float]:
        buf = np.concatenate([self._residual, pcm16.astype(np.int16)])

        probs: list[float] = []
        n_windows = len(buf) // self.WINDOW
        for i in range(n_windows):
            window = buf[i * self.WINDOW:(i + 1) * self.WINDOW].astype(np.float32) / 32768.0
            model_input = np.concatenate([self._context, window]).reshape(1, 576).astype(np.float32)

            outputs = self._session.run(
                None,
                {"input": model_input, "h": self._h, "c": self._c},
            )
            out_map = dict(zip([o.name for o in self._session.get_outputs()], outputs))
            prob = float(np.asarray(out_map["speech_probs"]).reshape(-1)[0])
            self._h = out_map["hn"]
            self._c = out_map["cn"]
            self._context = window[-self.CONTEXT:]

            probs.append(prob)

        self._residual = buf[n_windows * self.WINDOW:]
        return probs
