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


def intent_to_agent_map(manifest: dict) -> dict[str, str]:
    """manifest → {intent_name: agent_name}，只收唯一的 intent name。
    parse_tool_call 的寬鬆 fallback 用：Gemini 偶爾回沒前綴的裸 intent name
    （實測 flash-lite 會把 find_song__find_lyrics 回成 find_lyrics）。"""
    seen: dict[str, str | None] = {}
    for agent_entry in manifest.get("agents", []):
        for intent in agent_entry.get("intents", []):
            n = intent["name"]
            seen[n] = agent_entry["name"] if n not in seen else None  # 撞名 → None
    return {n: a for n, a in seen.items() if a is not None}


def parse_tool_call(function_call, intent_agents: dict[str, str] | None = None
                    ) -> tuple[str, str, dict] | None:
    """Gemini FunctionCall → (agent_name, intent_name, args dict)。

    name 有 "__" → 直接拆。沒有 "__" 但 intent_agents 給了且該裸名唯一對應一個
    agent → 用它（Gemini 掉前綴的 fallback）。都不行 → None，不炸。
    """
    name = getattr(function_call, "name", None)
    if not name:
        return None
    args = dict(getattr(function_call, "args", None) or {})
    if _SEP in name:
        agent_name, intent_name = name.split(_SEP, 1)
        if agent_name and intent_name:
            return agent_name, intent_name, args
        return None
    if intent_agents and name in intent_agents:
        return intent_agents[name], name, args
    return None


# ── 棄權 tool ────────────────────────────────────────────────────────────────
# manifest_to_function_declarations 把每個 required_slot 都標成 Gemini required，
# 一次又給 Gemini 多個誘人的 tool，Gemini 一旦選了某個就被結構逼著吐 slot 值——
# 唯一的「不動作」出口是「不呼叫任何 tool」，但那要跟一整排 tool 競爭。給它一個
# 明確的棄權 tool，Gemini 判斷「使用者只是在聊天、以上都不是」時可以直接選它，
# rescue agent 收到就 return None 走一般聊天，不硬套任何 intent。

ABSTAIN_TOOL_NAME = "just_chatting"

ABSTAIN_FUNCTION_DECLARATION = types.FunctionDeclaration(
    name=ABSTAIN_TOOL_NAME,
    description=(
        "以上工具都不符合。使用者只是在閒聊、發表意見、回應別人，或說的話"
        "不對應任何已知操作。不確定要選哪個時，選這個而不是硬套一個。"
    ),
    parameters=types.Schema(type="OBJECT", properties={}),
)


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
