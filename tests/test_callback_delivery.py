"""T3: 返場 callback 投遞的純邏輯（feature flag + 措辭模板）。

flag 預設 OFF → callback 不會發聲（dormant），merge/重啟不改變現有行為。
措辭目前走模板（安全、零 LLM latency on join hot path）；LLM 措辭潤飾留後續。
on_voice_state_update 的 async glue（peek→TTS→consume）在 voice_controller，不在此單元測。
"""
import importlib
import callback_delivery as cd


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("CALLBACK_ON_JOIN", raising=False)
    assert cd.is_join_callback_enabled() is False


def test_flag_on_values(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("CALLBACK_ON_JOIN", v)
        assert cd.is_join_callback_enabled() is True


def test_flag_off_values(monkeypatch):
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("CALLBACK_ON_JOIN", v)
        assert cd.is_join_callback_enabled() is False


def test_format_callback_line():
    line = cd.format_callback_line("戒咖啡")
    assert "戒咖啡" in line
    assert line.strip() != ""


def test_format_callback_line_empty_returns_empty():
    assert cd.format_callback_line("") == ""
    assert cd.format_callback_line("   ") == ""


# ── format_topic_callback_line (MemoryCallbackAgent v3, plan-eng-review T2) ───
# 主題關聯 callback 的措辭模板。跟 T3 的 join 模板分開：
# T3「歡迎回來」→ join 場景；v3「對了你之前說要 X」→ 對話中被主題拉出。

def test_format_topic_callback_line_normal():
    line = cd.format_topic_callback_line("試 grounded search")
    assert "試 grounded search" in line
    assert line.strip() != ""


def test_format_topic_callback_line_empty_returns_empty():
    assert cd.format_topic_callback_line("") == ""
    assert cd.format_topic_callback_line("   ") == ""
