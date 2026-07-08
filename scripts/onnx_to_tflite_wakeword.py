#!/usr/bin/env python3
"""openWakeWord wake-head onnx → tflite（正確 [1,N,96] 輸入 layout）。

實體音箱 S1：Colab 防彈 notebook 只出 onnx，但 Pi 的 wyoming-openwakeword 慣吃 tflite。
⚠️ 直接用 onnx2tf 會把 [1,N,96] 轉置成 [1,96,N]（NCHW→NHWC 優化）→ openWakeWord 餵
[1,N,96] 會對不上、Pi 上失效。故不用 onnx2tf，改「讀 onnx 權重→Keras 以正確輸入重建
→驗證數值等同→匯出 tflite」，layout 由我們控制、保證正確。

openWakeWord wake head 架構（本腳本假設，openWakeWord 標準）：
    input [1, N_frames, 96] → Flatten → Dense(hidden) → LayerNorm → ReLU → Dense(1) → Sigmoid
N_frames 隨喚醒詞長度變（hey_mycroft/hey_marvin=16、weather=22、timer=34），由權重自動推斷。

用法（在有 tensorflow 的 venv，如 /tmp/tflenv）：
    python scripts/onnx_to_tflite_wakeword.py models/wakeword/hey_marvin.onnx
    # → 寫出同目錄 hey_marvin.tflite，並印 onnx↔tflite 數值誤差（應 <1e-4）

deps：onnx onnxruntime tensorflow numpy
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import tensorflow as tf
from onnx import numpy_helper


def convert(onnx_path: str) -> str:
    m = onnx.load(onnx_path)
    W = {i.name: numpy_helper.to_array(i) for i in m.graph.initializer}

    # LayerNorm eps（layernorm 的 Add 常數）
    eps = 1e-5
    for n in m.graph.node:
        if n.op_type == "Constant" and n.output and "Constant_1" in n.output[0]:
            eps = float(numpy_helper.to_array(n.attribute[0].t).reshape(-1)[0])

    l1w, l1b = W["base.layer1.weight"], W["base.layer1.bias"]         # (hidden, N*96)
    lnw, lnb = W["base.layernorm1.weight"], W["base.layernorm1.bias"]
    l2w, l2b = W["base.layer2.weight"], W["base.layer2.bias"]         # (1, hidden)

    hidden = l1w.shape[0]
    n_frames = l1w.shape[1] // 96
    assert l1w.shape[1] == n_frames * 96, f"非預期 layer1 形狀 {l1w.shape}"

    inp = tf.keras.Input(shape=(n_frames, 96))
    x = tf.keras.layers.Flatten()(inp)
    d1 = tf.keras.layers.Dense(hidden); x = d1(x)
    ln = tf.keras.layers.LayerNormalization(axis=-1, epsilon=eps); x = ln(x)
    x = tf.keras.layers.ReLU()(x)
    d2 = tf.keras.layers.Dense(1); x = d2(x)
    out = tf.keras.layers.Activation("sigmoid")(x)
    model = tf.keras.Model(inp, out)
    # Gemm transB=1 → Keras kernel = W.T
    d1.set_weights([l1w.T, l1b]); ln.set_weights([lnw, lnb]); d2.set_weights([l2w.T, l2b])

    # 匯出
    tfl = tf.lite.TFLiteConverter.from_keras_model(model).convert()
    out_path = str(Path(onnx_path).with_suffix(".tflite"))
    Path(out_path).write_bytes(tfl)

    # 驗證數值等同（onnx vs tflite）
    s = ort.InferenceSession(onnx_path); iname = s.get_inputs()[0].name
    rng = np.random.default_rng(0)
    X = rng.standard_normal((16, n_frames, 96)).astype(np.float32)
    onnx_o = np.array([s.run(None, {iname: x[None]})[0][0, 0] for x in X])
    it = tf.lite.Interpreter(model_content=tfl); it.allocate_tensors()
    ind, outd = it.get_input_details()[0], it.get_output_details()[0]
    tfl_o = []
    for x in X:
        it.set_tensor(ind["index"], x[None].astype(np.float32)); it.invoke()
        tfl_o.append(it.get_tensor(outd["index"])[0, 0])
    max_err = float(np.max(np.abs(onnx_o - np.array(tfl_o))))

    print(f"✅ {out_path}")
    print(f"   input shape: {list(ind['shape'])}  (要 [1, {n_frames}, 96])")
    print(f"   onnx↔tflite 最大誤差: {max_err:.2e}  ->", "PASS" if max_err < 1e-4 else "FAIL ⚠️")
    if max_err >= 1e-4:
        raise SystemExit("數值不匹配，別用這顆 tflite")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("用法: python scripts/onnx_to_tflite_wakeword.py <path/to/model.onnx>")
    convert(sys.argv[1])
