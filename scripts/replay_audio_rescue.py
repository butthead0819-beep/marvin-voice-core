"""Audio Rescue 轉正 gate — 對一批 wav 重放 AudioRescueAgent 的「聽 → 選 tool →
反查 intent」鏈，印每筆結果 + 批次延遲，不執行任何 handler。

用途：翻 MARVIN_INTENT_RESCUE_SHADOW=0 前的人工驗收（design doc Criteria 5）。
語料 = records/rescue_wav/（sidecar，由 IntentBus 在 MODE=audio 時落地），或
自己錄的對抗集目錄。

  python scripts/replay_audio_rescue.py [wav_dir]        # 預設 records/rescue_wav
  python scripts/replay_audio_rescue.py fixtures/adversarial --json

每筆輸出：
  tool call        — Gemini 選的 function name（含 just_chatting / 唯讀 tool）
  agent/intent     — parse_tool_call 反查
  slots            — Gemini 填的 slot（+ 是否有空值 = 幻覺風險）
  resolve          — resolve_intent 會不會回非 None bid；回 None 時卡在哪一關
  gemini_ms        — 單次 Gemini 呼叫 wall-clock

⚠️ 這支腳本會真的打付費 Gemini（每筆 ~$0.00003），走 PaidUsageGuard 記
llm_paid_usage.jsonl。一批 20-30 筆約 $0.001。
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# ── key 守衛：漏 load_dotenv / 空 key 池會讓每筆靜默回 None，你會誤判成 manifest 爛
_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
if not _KEY:
    sys.exit(
        "❌ 沒有 GOOGLE_API_KEY / GEMINI_API_KEY。\n"
        "   .env 沒載到或 key 沒設 —— replay 會每筆回 None，先修這個再跑。"
    )

from google import genai  # noqa: E402

from intent_agents.audio_rescue_agent import AudioRescueAgent  # noqa: E402
from intent_bus import IntentContext  # noqa: E402


# opt-in audio-rescue agent 清單（有 manifest_description 的那些）。用寬鬆 stub
# controller —— replay 只驗 routing，不執行 handler，也不模擬真實播放狀態。
def _build_agents():
    from intent_agents.grounded_qa_agent import GroundedQAAgent
    from intent_agents.volume_agent import VolumeAgent
    from intent_agents.playback_control_agent import PlaybackControlAgent
    from intent_agents.music_agent_v2 import MusicAgentV2
    from intent_agents.find_song_agent import FindSongAgent

    ctrl = SimpleNamespace(stream_mode=True, radio_mode=True)
    return {
        a.name: a
        for a in (
            GroundedQAAgent(ctrl), VolumeAgent(ctrl), PlaybackControlAgent(ctrl),
            MusicAgentV2(ctrl), FindSongAgent(ctrl),
        )
    }


def _manifest(agents):
    return {
        "version": "replay",
        "agents": [
            {
                "name": name,
                "intents": [
                    {"name": s.name, "required_slots": list(s.required_slots),
                     "reason_template": s.reason_template,
                     "description": s.manifest_description}
                    for s in ag.declare_intents()
                ],
            }
            for name, ag in agents.items()
        ],
    }


def _why_resolve_none(agent, intent_name, slots, ctx) -> str:
    """resolve_intent 回 None 時，逐關重跑判斷卡在哪。"""
    if ctx.mode not in agent.mode_compatible:
        return f"mode:{ctx.mode}"
    g = agent.gate(ctx)
    if g is not None:
        return f"gate:{g}"
    schema = next((s for s in agent.declare_intents() if s.name == intent_name), None)
    if schema is None:
        return "unknown_intent"
    filled = {k: (v or "") for k, v in (slots or {}).items()}
    if not agent.post_match_filter(schema, filled, ctx):
        return "post_match_filter"
    missing = [s for s in schema.required_slots if not filled.get(s, "").strip()]
    if missing:
        return f"missing_slot:{'+'.join(missing)}"
    return "unknown"


async def _replay_one(rescue_agent, agents, wav_path: Path) -> dict:
    ctx = IntentContext(
        speaker="replay", raw_text="", query="", original_raw="",
        wake_intent=0.9, stream_active=True, game_mode=False, is_owner=False,
        now=time.time(), mode="normal", audio_wav_bytes=wav_path.read_bytes(),
    )
    t0 = time.perf_counter()
    resolved = await rescue_agent.synthesize(ctx)
    gemini_ms = (time.perf_counter() - t0) * 1000

    row: dict = {"wav": wav_path.name, "gemini_ms": round(gemini_ms, 1)}
    if resolved is None:
        # synthesize 回 None：abstain / 逾時 / 例外 / 沒 function_call / 只有唯讀 tool
        row["tool"] = "(none — abstain / timeout / no-call)"
        row["resolve"] = "n/a"
        return row

    agent_name = resolved.resolved_agent
    intent_name = resolved.resolved_intent
    slots = resolved.resolved_slots or {}
    row["tool"] = f"{agent_name}__{intent_name}"
    row["slots"] = slots
    row["slot_blank"] = [k for k, v in slots.items() if not str(v or "").strip()]

    agent = agents.get(agent_name)
    if agent is None or not hasattr(agent, "resolve_intent"):
        row["resolve"] = f"no_agent:{agent_name}"
        return row
    bid = agent.resolve_intent(intent_name, slots, resolved)
    row["resolve"] = "OK" if bid is not None else f"None ({_why_resolve_none(agent, intent_name, slots, resolved)})"
    return row


async def _main(wav_dir: Path, as_json: bool) -> None:
    wavs = sorted(wav_dir.glob("*.wav"))
    if not wavs:
        sys.exit(f"❌ {wav_dir} 沒有 .wav 檔。")

    agents = _build_agents()
    manifest = _manifest(agents)
    client = genai.Client(api_key=_KEY)
    rescue_agent = AudioRescueAgent(google_client=client, manifest_provider=lambda: manifest)

    rows = []
    for w in wavs:
        try:
            rows.append(await _replay_one(rescue_agent, agents, w))
        except Exception as exc:  # noqa: BLE001 — dev tool，單筆炸不該中斷整批
            rows.append({"wav": w.name, "error": f"{type(exc).__name__}: {exc}"})

    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            if "error" in r:
                print(f"  {r['wav']:<40} ERROR {r['error']}")
                continue
            slots = r.get("slots", {})
            blank = f"  ⚠️blank={r['slot_blank']}" if r.get("slot_blank") else ""
            print(f"  {r['wav']:<40} {r['gemini_ms']:>7.0f}ms  {r['tool']:<32} "
                  f"resolve={r.get('resolve','?'):<28} slots={slots}{blank}")

    ms = [r["gemini_ms"] for r in rows if "gemini_ms" in r]
    ok = sum(1 for r in rows if r.get("resolve") == "OK")
    abstained = sum(1 for r in rows if str(r.get("tool", "")).startswith("(none"))
    print(f"\n  n={len(rows)}  resolve_OK={ok}  abstain/none={abstained}  "
          f"resolve_fail={len(rows) - ok - abstained}")
    if ms:
        ms.sort()
        p95 = ms[min(len(ms) - 1, int(len(ms) * 0.95))]
        print(f"  gemini_ms: median={statistics.median(ms):.0f}  p95={p95:.0f}  max={ms[-1]:.0f}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    as_json = "--json" in sys.argv
    target = Path(args[0]) if args else Path("records/rescue_wav")
    asyncio.run(_main(target, as_json))
