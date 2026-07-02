#!/usr/bin/env python3
"""防線① 外部心跳 probe — 「該有輸出而沒輸出」的主動偵測。

由 launchd cron（com.antigravity.marvin.heartbeatprobe，每 30 分）執行，
與 bot 進程完全獨立。三個 check 對應三種真實發生過的半死型事故：

  1. heartbeat_fresh — bot event loop 凍住（2026-06-29 busy-spin：進程活著、
     launchd 不重啟、DM 發不出）。讀 records/heartbeat.json 驗 staleness。
  2. stt — Swift bin / SpeechAnalyzer 模型壞掉。直接跑 macos_stt_v2_bin
     對快取的合成 wav，驗輸出非空。
  3. tts — edge-tts 被微軟限流/斷線（「有回應沒聲音」）。直打微軟合成
     短句，驗 audio bytes。

設計鐵則：
  - probe 不進 bot 資料路徑（STT 直跑 bin、TTS 直打微軟）→ 零遙測污染
    （judge_outcomes 曾 82% 被 Alice probe 污染——不進 pipeline 就不會污染）
  - 告警走 Discord REST（bot token 直打 HTTP API）→ bot 凍住也送得到
  - 告警去重：同 failure signature 在 realert 窗內只發一次；恢復發 recovered
  - kill-switch：env MARVIN_HEARTBEAT_PROBE=0 → 直接退出
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BEACON_PATH = REPO / "records" / "heartbeat.json"
STATE_PATH = REPO / "records" / "heartbeat_probe_state.json"
PROBE_WAV = REPO / "records" / "probe_stt_fixture.wav"
OWNER_ID = int(os.environ.get("MARVIN_OWNER_ID", "876758076831723580"))
HEARTBEAT_MAX_AGE_S = 120        # 信標 30s 一寫，4 個週期沒寫＝凍住
REALERT_AFTER_S = 6 * 3600
TTS_MIN_AUDIO_BYTES = 1000       # 短句合成低於此＝沒真的出聲音


# ── checks（每個回 (ok, detail)） ─────────────────────────────────────────────

def check_heartbeat_fresh(path: Path | str = BEACON_PATH,
                          max_age_s: float = HEARTBEAT_MAX_AGE_S) -> tuple[bool, str]:
    p = Path(path)
    if not p.exists():
        return False, f"heartbeat 檔不存在（{p}）——bot 沒起來或信標未接線"
    try:
        ts = float(json.loads(p.read_text()).get("ts", 0))
    except Exception as e:
        return False, f"heartbeat 檔壞掉: {e}"
    age = time.time() - ts
    if age > max_age_s:
        return False, f"heartbeat stale {age:.0f}s（>{max_age_s:.0f}s）——event loop 可能凍住"
    return True, f"fresh ({age:.0f}s)"


def _ensure_probe_wav() -> Path:
    """合成 wav 快取（一次性 say，之後重用；被刪自動重生）。"""
    if not PROBE_WAV.exists():
        PROBE_WAV.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["say", "-v", "Meijia", "馬文測試心跳", "-o", str(PROBE_WAV),
             "--data-format=LEI16@48000"],
            check=True, timeout=30,
        )
    return PROBE_WAV


def check_stt() -> tuple[bool, str]:
    wav = _ensure_probe_wav()
    env = dict(os.environ, STT_LOCALE="zh-TW")
    r = subprocess.run(
        [str(REPO / "macos_stt_v2_bin"), str(wav)],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(REPO),
    )
    if r.returncode != 0:
        return False, f"STT bin exit={r.returncode}: {r.stderr[:200]}"
    lines = [l for l in r.stdout.splitlines() if l and not l.startswith("__META__")]
    text = lines[-1].strip() if lines else ""
    if not text:
        return False, "STT 輸出空白（模型資產或引擎壞掉）"
    return True, f"ok: {text[:30]}"


def check_tts() -> tuple[bool, str]:
    import asyncio

    import edge_tts

    async def _synth() -> int:
        comm = edge_tts.Communicate(text="心跳測試", voice="zh-TW-YunJheNeural")
        total = 0
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                total += len(chunk["data"])
        return total

    n = asyncio.run(_synth())
    if n < TTS_MIN_AUDIO_BYTES:
        return False, f"edge-tts 只回 {n} bytes（限流/No audio was received 型故障）"
    return True, f"ok ({n} bytes)"


# ── 組裝與告警去重 ────────────────────────────────────────────────────────────

def run_checks(checks: list[tuple[str, callable]]) -> list[tuple[str, str]]:
    """跑全部 check，回失敗清單 [(name, detail)]。check 自己炸＝該層 fail。"""
    failures: list[tuple[str, str]] = []
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"check 本身異常: {type(e).__name__}: {e}"
        if not ok:
            failures.append((name, detail))
    return failures


def decide_alert(state_path: Path | str, failures: list[tuple[str, str]],
                 realert_after_s: float = REALERT_AFTER_S,
                 now: float | None = None) -> str:
    """告警去重狀態機 → 'alert' | 'suppress' | 'recovered' | 'silent'。

    同 signature 在 realert 窗內只發一次；signature 變了立即再發；
    由 fail 轉全綠發 recovered；穩態全綠沉默。
    """
    now = now if now is not None else time.time()
    p = Path(state_path)
    try:
        state = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        state = {}
    prev_sig = state.get("signature", "")
    sig = "|".join(name for name, _ in failures)

    def _save(s):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    if failures:
        if sig == prev_sig and now - float(state.get("alerted_ts", 0)) < realert_after_s:
            return "suppress"
        _save({"signature": sig, "alerted_ts": now})
        return "alert"
    if prev_sig:
        _save({"signature": "", "alerted_ts": 0})
        return "recovered"
    return "silent"


# ── Discord REST DM（不依賴 bot 進程） ────────────────────────────────────────

def _read_token() -> str:
    tok = os.environ.get("DISCORD_BOT_TOKEN", "")
    if tok:
        return tok
    for line in (REPO / ".env").read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DISCORD_BOT_TOKEN not found")


def _api(path: str, payload: dict, token: str) -> dict:
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "MarvinHeartbeatProbe/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def dm_owner(text: str) -> None:
    token = _read_token()
    ch = _api("/users/@me/channels", {"recipient_id": str(OWNER_ID)}, token)
    _api(f"/channels/{ch['id']}/messages", {"content": text[:1990]}, token)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    if os.environ.get("MARVIN_HEARTBEAT_PROBE", "1") == "0":
        print("[probe] kill-switch off, exit")
        return 0
    failures = run_checks([
        ("heartbeat", check_heartbeat_fresh),
        ("stt", check_stt),
        ("tts", check_tts),
    ])
    action = decide_alert(STATE_PATH, failures)
    stamp = time.strftime("%m-%d %H:%M")
    if action == "alert":
        lines = "\n".join(f"• **{n}**: {d}" for n, d in failures)
        print(f"[probe] ALERT: {failures}")
        dm_owner(f"🚨 [HeartbeatProbe {stamp}] 語音 pipeline 檢查失敗：\n{lines}")
    elif action == "recovered":
        print("[probe] recovered")
        dm_owner(f"✅ [HeartbeatProbe {stamp}] 先前失敗的檢查已全數恢復。")
    else:
        print(f"[probe] {action}（failures={len(failures)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
