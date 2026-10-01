"""測試同意通知內容與 PRIVACY.md §4 一致性。

涵蓋：
- consent_manager.consent_notice
- 與 PRIVACY.md 外部服務清單一致性
"""
from pathlib import Path
import pytest


def test_consent_notice_content():
    from consent_manager import consent_notice

    mention = "<@123456789>"
    notice = consent_notice(mention)

    assert mention in notice

    expected_services = [
        "Google Gemini",
        "Groq",
        "Mistral",
        "SambaNova",
        "Together AI",
        "OpenRouter",
    ]
    for svc in expected_services:
        assert svc in notice, f"缺少預期服務: {svc}"

    # 不應包含已淘汰或過時內部檔名
    assert "Cerebras" not in notice
    assert "suki_memory" not in notice


def test_consent_notice_aligns_with_privacy_md():
    from consent_manager import consent_notice

    privacy_path = Path(__file__).resolve().parent.parent / "PRIVACY.md"
    assert privacy_path.exists(), "找不到 PRIVACY.md"
    content = privacy_path.read_text(encoding="utf-8")

    start_idx = content.find("## 4.")
    end_idx = content.find("## 5.")
    assert start_idx != -1 and end_idx != -1 and start_idx < end_idx, "PRIVACY.md 缺少 ## 4. 或 ## 5. 段落"

    sec4 = content[start_idx:end_idx]

    expected_services = [
        "Google Gemini",
        "Groq",
        "Mistral",
        "SambaNova",
        "Together AI",
        "OpenRouter",
    ]
    for svc in expected_services:
        assert svc in sec4, f"PRIVACY.md §4 缺少服務: {svc}"
