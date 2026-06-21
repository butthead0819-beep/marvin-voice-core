"""B 骨架 — 出圖時現生馬文吐槽（page punchline）測試。

6 月日記已無【碎念】，改成拼版時把整頁核心丟 LLM 現生一句馬文毒舌。
LLM 用注入式：測試注入假的，production 注入真的（gemini/groq）。
"""
from diary_comic.punchline import build_prompt, generate_page_punchline

CORES = [
    "陳進文和狗與露討論燈光和裝潢。",
    "討論露營餐點準備與數位擴大機挑選。",
    "PS4 遊戲體驗與主機運作狀態。",
]


def test_build_prompt_includes_every_core():
    system, user = build_prompt(CORES)
    assert system  # 有馬文人設
    for c in CORES:
        assert c in user


def test_generate_page_punchline_returns_injected_llm_output():
    fake = lambda system, user: "  人類連天花板都能吵一小時，宇宙想關機。  "
    line = generate_page_punchline(CORES, generate_fn=fake)
    assert line == "人類連天花板都能吵一小時，宇宙想關機。"  # 有 strip


def test_generate_page_punchline_passes_cores_into_prompt():
    seen = {}

    def spy(system, user):
        seen["user"] = user
        return "嘆。"

    generate_page_punchline(CORES, generate_fn=spy)
    assert "PS4" in seen["user"]


def test_generate_page_punchline_empty_without_llm():
    # 沒注入 LLM → 留白（demo/production 才注入真的），不硬掰
    assert generate_page_punchline(CORES, generate_fn=None) == ""


def test_generate_page_punchline_empty_cores_returns_empty():
    fake = lambda system, user: "不該被呼叫"
    assert generate_page_punchline([], generate_fn=fake) == ""


def test_generate_page_punchline_swallows_llm_failure():
    def boom(system, user):
        raise RuntimeError("LLM down")

    # I/O 失敗要降級成留白，不能炸掉整條拼版（CLAUDE.md fallback 鐵則）
    assert generate_page_punchline(CORES, generate_fn=boom) == ""
