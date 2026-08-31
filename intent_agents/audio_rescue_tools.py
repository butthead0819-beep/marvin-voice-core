"""Audio Rescue v2 — build_intent_manifest() ↔ Gemini FunctionDeclaration 轉換。

不碰 I/O，純資料轉換，方便測試不用打 Gemini。

Tool function name 規則：f"{agent_name}__{intent_name}"（Gemini function name
只允許 [a-zA-Z0-9_.-]{1,64}；agent/intent name 現況都是 snake_case，"__" 分隔符
保證能唯一反查回 (agent_name, intent_name) —— 用 split("__", 1) 還原）。
"""
from __future__ import annotations

from google.genai import types

_SEP = "__"


def _tool_name(agent_name: str, intent_name: str) -> str:
    if _SEP in agent_name or _SEP in intent_name:
        raise ValueError(
            f"agent/intent name 不能含 '{_SEP}'（會破壞 parse_tool_call 反查）: "
            f"agent={agent_name!r} intent={intent_name!r}"
        )
    return f"{agent_name}{_SEP}{intent_name}"


def manifest_to_function_declarations(manifest: dict) -> list[types.FunctionDeclaration]:
    """IntentBus.build_intent_manifest() 的輸出 → Gemini FunctionDeclaration list。

    manifest 結構：{"version": str, "agents": [{"name": str, "intents": [
        {"name": str, "required_slots": list[str], "reason_template": str}, ...
    ]}]}

    所有 slot 一律視為 string 型參數（現有 regex named group 全部是 str）。

    opt-in：只有 IntentSchema 明確填了 manifest_description（→ manifest 的
    "description"）的 intent 才會曝給 Gemini。理由：(1) build_intent_manifest 的
    另一個消費者是 intent-gap classifier，它要看到「全部」intent，所以過濾放在
    audio-rescue 這層而非 manifest 產生層；(2) 沒寫 Gemini-facing 描述的 intent
    （generic 預設「X 的 Y 意圖」）Gemini 根本分不出來，曝出去只是雜訊、還會稀釋
    Gemini 的選擇；(3) 讓「這個 intent 要不要接 audio rescue」變成 agent 寫一句
    描述的明確動作——例如 MusicAgentV2 只曝專用的 rescue_play、其餘 8 個
    regex/resolver schema 不曝。

    去重：同一 tool name 只出第一筆。一個 DeclarativeIntentAgent 可能宣告多個
    同名 IntentSchema（regex 路徑合法，first-match-wins 靠不同 pattern，如
    PersonalShuffleAgent 的兩個 personal_shuffle_start）。逐 schema 產宣告會撞出
    重複 name → Gemini 整包 request 回 400「Duplicate function declaration」→
    audio rescue 全失敗（2026-08-31 prod 實錄）。
    """
    declarations: list[types.FunctionDeclaration] = []
    seen: set[str] = set()
    for agent_entry in manifest.get("agents", []):
        agent_name = agent_entry["name"]
        for intent in agent_entry.get("intents", []):
            intent_name = intent["name"]
            desc = (intent.get("description") or "").strip()
            if not desc:
                continue  # opt-in：沒寫 manifest_description → 不曝給 Gemini
            tool_name = _tool_name(agent_name, intent_name)
            if tool_name in seen:
                continue
            seen.add(tool_name)
            required_slots = list(intent.get("required_slots", []))
            properties = {
                slot: types.Schema(type="STRING")
                for slot in required_slots
            }
            declarations.append(
                types.FunctionDeclaration(
                    name=tool_name,
                    description=desc,
                    parameters=types.Schema(
                        type="OBJECT",
                        properties=properties or None,
                        required=required_slots or None,
                    ),
                )
            )
    return declarations


def parse_tool_call(function_call) -> tuple[str, str, dict] | None:
    """Gemini FunctionCall → (agent_name, intent_name, args dict)。

    name 格式不對（沒有 "__" 分隔符）回 None，不炸——上游只需要優雅降級。
    """
    name = getattr(function_call, "name", None)
    if not name or _SEP not in name:
        return None
    agent_name, intent_name = name.split(_SEP, 1)
    if not agent_name or not intent_name:
        return None
    args = dict(getattr(function_call, "args", None) or {})
    return agent_name, intent_name, args


# ── 唯讀查詢 tool（不對應任何 IntentAgent/handler，rescue agent 自行執行）──────

READONLY_TOOL_NAMES = frozenset({"get_now_playing", "get_recent_history"})

READONLY_FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="get_now_playing",
        description="查詢目前正在播放的歌曲/內容，使用者只是在問資訊而非下指令時使用",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="get_recent_history",
        description="查詢最近幾句對話歷史，使用者問「剛剛在講什麼」這類問題時使用",
        parameters=types.Schema(
            type="OBJECT",
            properties={"n_turns": types.Schema(type="INTEGER")},
        ),
    ),
]
