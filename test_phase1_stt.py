"""
Phase 1 Fused Intent Scorer — test suite
Run: python test_phase1_stt.py
"""
import asyncio
import json
import os
import sys
import types
import logging
from pathlib import Path

# Load .env from project root before anything else
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# ── setup logging ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# ── add project root to path ─────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from stt_cleaner import GeminiRouterSTTMixin, WAKE_THRESHOLD

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []


def check(label: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append((status, label))
    print(f"  {status}  {label}" + (f" — {detail}" if detail else ""))
    return condition


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1: Logic unit tests (mock Groq client)
# ═══════════════════════════════════════════════════════════════════════════════

def make_mock_instance(llm_response: str):
    """Create a minimal GeminiRouterSTTMixin instance with a mock Groq client."""

    class MockChoice:
        class message:
            content = llm_response

    class MockResponse:
        choices = [MockChoice()]
        usage = types.SimpleNamespace(total_tokens=50)

    class MockGroqClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    return MockResponse()

    class MockPromptManager:
        def get_instruction(self, layer, **kwargs):
            # Return the actual stt_cleaner prompt
            import marvin_prompts
            pm = marvin_prompts.PromptManager.__new__(marvin_prompts.PromptManager)
            pm.__init__()
            return pm.get_instruction(layer, **kwargs)

    inst = GeminiRouterSTTMixin.__new__(GeminiRouterSTTMixin)
    inst.groq_dedicated_client = MockGroqClient()
    inst.groq_cleaner_usage = []
    inst.groq_simple_model = None
    inst.prompt_manager = MockPromptManager()
    return inst


async def run_logic_tests():
    print("\n" + "═" * 60)
    print("PART 1: Logic unit tests")
    print("═" * 60)

    # ── 1. Valid JSON, clear wake (intent=1.0, calling=True) ─────────────────
    inst = make_mock_instance('{"cleaned": "馬文，今天天氣怎樣？", "intent": 1.0, "calling": true}')
    res = await inst.clean_stt_text("媽問今天天氣怎樣？", speaker="Jack")
    check("Valid JSON wake (intent=1.0)", res["is_wake"] is True, f"is_wake={res['is_wake']} intent={res['wake_intent']}")
    check("Cleaned text extracted", res["text"] == "馬文，今天天氣怎樣？", f"text='{res['text']}'")
    check("wake_intent returned", res["wake_intent"] == 1.0)
    check("wake_threshold returned", res["wake_threshold"] == WAKE_THRESHOLD)

    # ── 2. Valid JSON, non-wake (intent=0.0, calling=False) ──────────────────
    inst = make_mock_instance('{"cleaned": "我昨天看了馬文的影片", "intent": 0.3, "calling": false}')
    res = await inst.clean_stt_text("我昨天看了馬文的影片", speaker="Jack")
    check("Valid JSON non-wake (intent=0.3)", res["is_wake"] is False, f"is_wake={res['is_wake']} intent={res['wake_intent']}")

    # ── 3. Borderline intent=0.7, calling=True → wake ────────────────────────
    inst = make_mock_instance('{"cleaned": "馬文你在嗎", "intent": 0.7, "calling": true}')
    res = await inst.clean_stt_text("馬文你在嗎", speaker="Jack")
    check("Borderline intent=0.7 + calling=True → wake", res["is_wake"] is True, f"is_wake={res['is_wake']}")

    # ── 4. Borderline intent=0.7, calling=False → no wake ────────────────────
    inst = make_mock_instance('{"cleaned": "馬文你在嗎", "intent": 0.7, "calling": false}')
    res = await inst.clean_stt_text("馬文你在嗎", speaker="Jack")
    check("Borderline intent=0.7 + calling=False → no wake", res["is_wake"] is False, f"is_wake={res['is_wake']}")

    # ── 5. High intent=0.75, unconditional wake ───────────────────────────────
    inst = make_mock_instance('{"cleaned": "馬文幫我查一下", "intent": 0.75, "calling": false}')
    res = await inst.clean_stt_text("馬文幫我查一下", speaker="Jack")
    check("intent=0.75 → unconditional wake (no calling needed)", res["is_wake"] is True)

    # ── 6. JSON parse failure → regex fallback on LLM output (NOT intent=0.0) ─
    # LLM returns garbage text with no wake word: regex on that output → no wake
    # (Track A would have caught "馬文" first anyway)
    inst = make_mock_instance('這不是 JSON 的輸出')
    res = await inst.clean_stt_text("馬文，你好嗎？", speaker="Jack")
    check("JSON parse failure → wake_intent=None (not 0.0)", res["wake_intent"] is None, f"wake_intent={res['wake_intent']}")
    check("JSON failure → regex on LLM output, no wake word → is_wake=False", res["is_wake"] is False, f"is_wake={res['is_wake']}")

    # ── 6b. JSON failure + LLM output contains wake word → regex catches it ──
    inst = make_mock_instance('馬文，你好嗎？')  # LLM echoed the cleaned text as plain text
    res = await inst.clean_stt_text("媽問你好嗎", speaker="Jack")
    check("JSON failure + LLM output has wake word → regex wakes", res["is_wake"] is True, f"is_wake={res['is_wake']}")

    # ── 7. JSON missing 'cleaned' field → original returned ──────────────────
    inst = make_mock_instance('{"intent": 0.9, "calling": true}')
    res = await inst.clean_stt_text("媽問今天天氣好嗎", speaker="Jack")
    check("JSON missing cleaned → original text preserved", res["text"] == "媽問今天天氣好嗎", f"text='{res['text']}'")
    check("JSON missing cleaned → wake_intent=None", res["wake_intent"] is None)

    # ── 8. Wake Injection Guard ───────────────────────────────────────────────
    # LLM claims calling=True but original has no wake word → reject
    inst = make_mock_instance('{"cleaned": "你好啊馬文", "intent": 1.0, "calling": true}')
    res = await inst.clean_stt_text("你好啊", speaker="Jack")  # original has no wake word
    check("Wake Injection Guard blocks LLM confabulation", res["is_wake"] is False, f"is_wake={res['is_wake']}")
    check("Wake Injection Guard returns original text", res["text"] == "你好啊", f"text='{res['text']}'")

    # ── 9. Short text bypasses LLM ───────────────────────────────────────────
    inst = make_mock_instance('{"cleaned": "嗯", "intent": 1.0, "calling": true}')
    res = await inst.clean_stt_text("嗯", speaker="Jack")
    check("Text < 3 chars bypasses LLM (no API call)", res["wake_intent"] is None)

    # ── 10. Repeated chars bypass LLM ────────────────────────────────────────
    inst = make_mock_instance('{"cleaned": "哈哈哈", "intent": 1.0, "calling": true}')
    res = await inst.clean_stt_text("哈哈哈", speaker="Jack")
    check("All-same-char bypasses LLM", res["wake_intent"] is None)

    # ── 11. Plain text response (non-JSON) with newline → rejected ────────────
    inst = make_mock_instance("第一行\n第二行（吐出Background）")
    res = await inst.clean_stt_text("你好嗎", speaker="Jack")
    check("Plain text with newline → rejected, original returned", res["text"] == "你好嗎")

    # ── 12. intent clamped to [0.0, 1.0] ─────────────────────────────────────
    inst = make_mock_instance('{"cleaned": "馬文你好", "intent": 1.5, "calling": true}')
    res = await inst.clean_stt_text("馬文你好", speaker="Jack")
    check("intent=1.5 clamped to 1.0", res["wake_intent"] == 1.0, f"wake_intent={res['wake_intent']}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2: Live Groq API probe — JSON format & intent quality
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_UTTERANCES = [
    # (raw_text, expected_wake: bool, description)
    ("馬文，今天天氣怎樣？", True,  "Clear wake + question"),
    ("馬文你在嗎", True,           "Short wake call"),
    ("媽問，幫我查一下",  True,     "STT error 媽問 → 馬文"),
    ("艾瑪文，你覺得呢", True,      "STT error 艾瑪文 → 馬文"),
    ("我昨天看了馬文的電影", False, "Mentioning, not calling"),
    ("他說馬文很厲害",    False,    "Third-party mention mid-sentence"),
    ("對啊我覺得也是",    False,    "Normal conversation, no wake word"),
    ("哈哈你說得對",      False,    "Laughter agreement, no wake"),
    ("幫我訂一個披薩",    False,    "Command but no wake word"),
    ("馬文馬文馬文",      True,     "Repeated wake word"),
]

GROQ_PROMPT = None  # loaded lazily


async def run_live_tests():
    global GROQ_PROMPT
    print("\n" + "═" * 60)
    print("PART 2: Live Groq API probe")
    print("═" * 60)

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("  ⚠️  GROQ_API_KEY not set — skipping live tests")
        return

    try:
        from groq import AsyncGroq
    except ImportError:
        print("  ⚠️  groq package not installed — skipping live tests")
        return

    # Load actual prompt from marvin_prompts
    import marvin_prompts
    pm = marvin_prompts.PromptManager()
    GROQ_PROMPT = pm.get_instruction("stt_cleaner", vision_enabled=False)

    client = AsyncGroq(api_key=api_key)
    model = os.getenv("GROQ_CLEANER_MODEL", "llama-3.1-8b-instant")
    print(f"  Model: {model}\n")

    json_ok = 0
    intent_correct = 0
    rows = []

    for raw, expected_wake, desc in SAMPLE_UTTERANCES:
        user_msg = f"<Target>{raw}</Target>"
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": GROQ_PROMPT},
                    {"role": "user", "content": user_msg}
                ],
                temperature=0.0,
                max_tokens=200,
                response_format={"type": "json_object"}
            )
            raw_out = resp.choices[0].message.content.strip()
            data = json.loads(raw_out)
            intent = float(data.get("intent", -1))
            calling = bool(data.get("calling", False))
            cleaned = data.get("cleaned", "")

            # Derive is_wake same logic as _build_res
            if intent >= 0.75:
                is_wake = True
            elif intent >= 0.65:
                is_wake = calling
            else:
                is_wake = False

            match = (is_wake == expected_wake)
            json_ok += 1
            if match:
                intent_correct += 1

            rows.append({
                "raw": raw, "cleaned": cleaned, "intent": intent,
                "calling": calling, "is_wake": is_wake,
                "expected": expected_wake, "match": match, "desc": desc
            })
        except (json.JSONDecodeError, Exception) as e:
            rows.append({
                "raw": raw, "intent": None, "is_wake": None,
                "expected": expected_wake, "match": False,
                "desc": f"ERROR: {e}", "cleaned": "", "calling": None
            })

    # Print table
    print(f"  {'Raw':<25} {'Cleaned':<25} {'Intent':>6} {'Calling':>7} {'Decision':>8} {'Expected':>8}  Result")
    print(f"  {'-'*25} {'-'*25} {'-'*6} {'-'*7} {'-'*8} {'-'*8}  ------")
    for r in rows:
        intent_str = f"{r['intent']:.2f}" if r['intent'] is not None else "ERR"
        calling_str = str(r['calling']) if r['calling'] is not None else "ERR"
        wake_str = "WAKE" if r['is_wake'] else "PASS"
        exp_str = "WAKE" if r['expected'] else "PASS"
        ok = "✅" if r['match'] else "❌"
        print(f"  {r['raw']:<25} {r['cleaned'][:24]:<25} {intent_str:>6} {calling_str:>7} {wake_str:>8} {exp_str:>8}  {ok} {r['desc']}")

    total = len(SAMPLE_UTTERANCES)
    print(f"\n  JSON parse: {json_ok}/{total}  |  Wake decision: {intent_correct}/{total}")
    check(f"JSON parse rate ≥ 95%", json_ok / total >= 0.95, f"{json_ok}/{total}")
    check(f"Wake decision accuracy ≥ 80%", intent_correct / total >= 0.80, f"{intent_correct}/{total}")


# ═══════════════════════════════════════════════════════════════════════════════
async def main():
    await run_logic_tests()
    await run_live_tests()

    print("\n" + "═" * 60)
    passed = sum(1 for s, _ in results if s == PASS)
    failed = sum(1 for s, _ in results if s == FAIL)
    print(f"TOTAL: {passed} passed, {failed} failed")
    print("═" * 60)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
