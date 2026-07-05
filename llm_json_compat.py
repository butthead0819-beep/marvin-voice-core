"""Groq OpenAI-compat 400 防護：json_object 模式時 messages 必須含字面 'json'。

Groq 實測錯誤："messages must contain the word json in some form, to use
response_format of type json_object"。本模組提供冪等 helper，在呼叫端 is_json
為真時確保 messages 滿足此規則；非 json 路徑完全不動。
"""
from __future__ import annotations

_JSON_HINT = "（請以 JSON 格式輸出）"


def ensure_json_in_messages(messages: list) -> list:
    """確保 messages 至少一則 content 含字面 'json'（大小寫不拘）。

    冪等：若已含 'json' 則原樣返回，不修改任何 dict。
    否則附加 _JSON_HINT 到第一則 system 訊息末尾；
    若無 system 訊息，在最前方插入一則 system 訊息。
    僅在呼叫端 is_json/json_mode 為真時呼叫。
    """
    if any("json" in (m.get("content") or "").lower() for m in messages):
        return messages
    for m in messages:
        if m.get("role") == "system":
            m["content"] = (m["content"] or "") + "\n" + _JSON_HINT
            return messages
    messages.insert(0, {"role": "system", "content": _JSON_HINT})
    return messages
