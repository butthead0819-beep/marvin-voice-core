"""Audio Rescue 轉正 gate — 對一批 wav 重放 AudioRescueAgent 的「聽 → 選 tool →
反查 intent」鏈，印每筆結果 + 批次延遲，不執行任何 handler。

用途：翻 MARVIN_INTENT_RESCUE_SHADOW=0 前的人工驗收（design doc Criteria 5）。
語料 = records/rescue_wav/（sidecar，由 IntentBus 在 MODE=audio 時落地），或
自己錄的對抗集目錄。

  python scripts/replay_audio_rescue.py [wav_dir]              # 預設 records/rescue_wav
  python scripts/replay_audio_rescue.py <dir> --corpus         # 對 manifest.jsonl 打分
  python scripts/replay_audio_rescue.py <dir> --delay 7        # 每筆間隔秒（避開 RPM）
  python scripts/replay_audio_rescue.py <dir> --json

有 GEMINI_PAID_API_KEY → 自動走付費 client（free tier 只有 10 RPM，一批 26 筆會撞 429）。

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
# 付費 key 優先（free tier flash-lite 只有 10 RPM，26 筆會撞 429）
_KEY = os.getenv("GEMINI_PAID_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
_PAID = bool(os.getenv("GEMINI_PAID_API_KEY"))
if not _KEY:
    sys.exit(
        "❌ 沒有 GOOGLE_API_KEY / GEMINI_API_KEY。\n"
        "   .env 沒載到或 key 沒設 —— replay 會每筆回 None，先修這個再跑。"
    )

from google import genai  # noqa: E402

from google.genai import types  # noqa: E402

from intent_agents.audio_rescue_agent import _PROMPT  # noqa: E402
from intent_agents.audio_rescue_tools import (  # noqa: E402
    ABSTAIN_FUNCTION_DECLARATION,
    ABSTAIN_TOOL_NAME,
    READONLY_FUNCTION_DECLARATIONS,
    READONLY_TOOL_NAMES,
    intent_to_agent_map,
    manifest_to_function_declarations,
    parse_tool_call,
)
from intent_bus import IntentContext  # noqa: E402

_MODEL = "gemini-2.5-flash-lite"
_REPLAY_TIMEOUT_S = 15.0  # 量真實延遲，不套 production 的 3s cap


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


def _ctx_for(wav_bytes: bytes):
    return IntentContext(
        speaker="replay", raw_text="", query="", original_raw="",
        wake_intent=0.9, stream_active=True, game_mode=False, is_owner=False,
        now=time.time(), mode="normal", dispatch_source="llm_rescue_audio",
        audio_wav_bytes=wav_bytes,
    )


async def _replay_one(client, tools, agents, intent_agents, wav_path: Path) -> dict:
    """自己打 Gemini（不經 AudioRescueAgent），才能分清 timeout / 棄權 / 沒 call，
    也不套 production 的 3s cap。"""
    wav_bytes = wav_path.read_bytes()
    row: dict = {"wav": wav_path.name}
    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=_MODEL,
                contents=[types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
                          _PROMPT.format(query="")],
                config=types.GenerateContentConfig(
                    tools=[types.Tool(function_declarations=tools)], temperature=0.0),
            ),
            timeout=_REPLAY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        row["gemini_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        row["tool"] = f"(timeout >{_REPLAY_TIMEOUT_S:.0f}s)"
        return row
    row["gemini_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    calls = getattr(resp, "function_calls", None) or []
    names = [c.name for c in calls]
    if not calls:
        row["tool"] = "(no tool call)"
        return row
    if ABSTAIN_TOOL_NAME in names:
        row["tool"] = "just_chatting"
        return row
    action = next((c for c in calls if c.name not in READONLY_TOOL_NAMES), None)
    if action is None:
        row["tool"] = f"(readonly only: {names})"
        return row
    parsed = parse_tool_call(action, intent_agents)
    if parsed is None:
        row["tool"] = f"(malformed: {action.name})"
        return row
    agent_name, intent_name, slots = parsed
    row["tool"] = f"{agent_name}__{intent_name}"
    row["slots"] = slots
    row["slot_blank"] = [k for k, v in slots.items() if not str(v or "").strip()]

    agent = agents.get(agent_name)
    if agent is None or not hasattr(agent, "resolve_intent"):
        row["resolve"] = f"no_agent:{agent_name}"
        return row
    ctx = _ctx_for(wav_bytes)
    bid = agent.resolve_intent(intent_name, slots, ctx)
    row["resolve"] = "OK" if bid is not None else f"None ({_why_resolve_none(agent, intent_name, slots, ctx)})"
    return row


async def _main(wav_dir: Path, as_json: bool, use_corpus: bool = False,
                delay: float = 0.0) -> None:
    wavs = sorted(wav_dir.glob("*.wav"))
    if not wavs:
        sys.exit(f"❌ {wav_dir} 沒有 .wav 檔。")

    agents = _build_agents()
    manifest = _manifest(agents)
    tools = (manifest_to_function_declarations(manifest)
             + READONLY_FUNCTION_DECLARATIONS + [ABSTAIN_FUNCTION_DECLARATION])
    intent_agents = intent_to_agent_map(manifest)
    client = genai.Client(api_key=_KEY)
    if not as_json:
        print(f"  client={'paid' if _PAID else 'free (10 RPM — 建議 --delay 7)'}  "
              f"delay={delay}s  timeout={_REPLAY_TIMEOUT_S:.0f}s  n={len(wavs)}  tools={len(tools)}\n")

    # --corpus：讀 wav_dir/manifest.jsonl 的 expect，逐筆打分（synth_audio_rescue_corpus 產）
    expect_by_wav: dict[str, str] = {}
    manifest_path = wav_dir / "manifest.jsonl"
    if use_corpus and manifest_path.exists():
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            m = json.loads(line)
            expect_by_wav[m["wav"]] = m["expect"]

    rows = []
    for i, w in enumerate(wavs):
        if delay and i:
            await asyncio.sleep(delay)
        try:
            row = await _replay_one(client, tools, agents, intent_agents, w)
        except Exception as exc:  # noqa: BLE001 — dev tool，單筆炸不該中斷整批
            row = {"wav": w.name, "error": f"{type(exc).__name__}: {exc}"}
        if w.name in expect_by_wav:
            row["expect"] = expect_by_wav[w.name]
            row["hit"] = (row.get("tool", "") == row["expect"])
        rows.append(row)

    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            if "error" in r:
                print(f"  {r['wav']:<44} ERROR {r['error']}")
                continue
            slots = r.get("slots", {})
            blank = f"  ⚠️blank={r['slot_blank']}" if r.get("slot_blank") else ""
            mark = ""
            if "hit" in r:
                mark = "  ✅" if r["hit"] else f"  ❌ want {r['expect']}"
            print(f"  {r['wav']:<44} {r['gemini_ms']:>7.0f}ms  {r['tool']:<32} "
                  f"resolve={r.get('resolve','?'):<26} slots={slots}{blank}{mark}")

    ms = [r["gemini_ms"] for r in rows if "gemini_ms" in r]
    ok = sum(1 for r in rows if r.get("resolve") == "OK")
    abstain = sum(1 for r in rows if r.get("tool") == "just_chatting")
    timeout = sum(1 for r in rows if str(r.get("tool", "")).startswith("(timeout"))
    nocall = sum(1 for r in rows if r.get("tool") in ("(no tool call)",))
    print(f"\n  n={len(rows)}  resolve_OK={ok}  just_chatting={abstain}  "
          f"timeout={timeout}  no_call={nocall}  resolve_fail={len(rows) - ok - abstain - timeout - nocall}")
    scored = [r for r in rows if "hit" in r]
    if scored:
        hits = sum(1 for r in scored if r["hit"])
        print(f"  routing: {hits}/{len(scored)} = {hits / len(scored):.0%} 命中 expect"
              f"  ({'PASS ≥90%' if hits / len(scored) >= 0.9 else 'FAIL <90%'})")
    if ms:
        ms.sort()
        p95 = ms[min(len(ms) - 1, int(len(ms) * 0.95))]
        print(f"  gemini_ms: median={statistics.median(ms):.0f}  p95={p95:.0f}  max={ms[-1]:.0f}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    as_json = "--json" in argv
    use_corpus = "--corpus" in argv
    delay = 0.0
    if "--delay" in argv:
        delay = float(argv[argv.index("--delay") + 1])
    positional = [a for a in argv if not a.startswith("-") and a != str(delay)]
    target = Path(positional[0]) if positional else Path("records/rescue_wav")
    asyncio.run(_main(target, as_json, use_corpus, delay))
