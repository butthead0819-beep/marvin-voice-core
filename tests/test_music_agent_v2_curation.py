"""TDD 失敗測試骨架（5/22 prep，等 Jack 審）— MusicAgentV2 三檔分流。

對應 memory/project_vector_intent_5_21.md「Step 1：MusicAgentV2 三檔 schema split」。
這是 5/21 被 prod 火災延後的 vector intent prod wiring 的第一步。

分流目標（play keyword 命中後，看 target 結構）：
  ├─ 含「的」且後段 ≥2 字          → SPECIFIC      conf=?, missing=[]
  ├─ 純 artist/genre token（≤4 字）→ CURATION      conf=0.85, missing=["song_choice"]
  └─ 含 directional modifier        → DIRECTIONAL   conf=0.50, missing=["directional_resolution"]
     （符合.*的 / 像.*那種 / 適合.*的）

實作（schema split）**尚未做** → 本檔預期全紅。確認設計後再寫最小實作轉綠。

────────────────────────────────────────────────────────────────────────────
⚠️⚠️ SPEC 矛盾，待 Jack 裁決（影響 SPECIFIC_CONF）：

  project_vector_intent_5_21.md 自己有兩處對「播放陶喆的天天」給不同 confidence：
    (a) 頂部設計表 (L17-25)：「含『的』+後段≥2字 → SPECIFIC conf=0.95」
    (b) 驗收表 Test 1 (L52)：「播放陶喆的天天」→ conf=0.95
    (c) Step 1 schema 表 (L126-131)：weak_play_with_marker **維持 0.80**，沒新增 0.95 specific schema

  現有 v2 對「播放陶喆的天天」回 0.80（weak_play_with_marker，因「播放」是 WEAK_PLAY_KW + 「的」是 marker）。
  本檔依驗收表寫成 0.95（= 新增 SPECIFIC schema，priority 高於 with_marker）。
  若你決定維持 0.80（不為「artist的song」升到 strong 等級），把下面 SPECIFIC_CONF 改 0.80 即可。
────────────────────────────────────────────────────────────────────────────

⚠️ 排序陷阱（實作時要注意，已用測試鎖住）：
  「播放周杰倫符合我年紀的歌」同時含「的」(像 SPECIFIC) 與 artist (像 CURATION)，
  但必須判 DIRECTIONAL。所以 directional schema 在 declare_intents() 的 order 必須
  **早於** specific / with_marker / artist_only（first-match-wins 與 confidence 解耦）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from intent_agents.music_agent_v2 import MusicAgentV2
from intent_bus import IntentContext

# 三檔的預期 confidence — 集中成常數方便 Jack review 後一次調整
SPECIFIC_CONF = 0.95      # ⚠️ 見頂部 SPEC 矛盾；若維持現狀改 0.80
CURATION_CONF = 0.85
DIRECTIONAL_CONF = 0.50


def _agent() -> MusicAgentV2:
    # bid() 只讀 module-level constants + ctx；ctrl 只在 make_handler 用到（本檔不 await handler）
    return MusicAgentV2(MagicMock())


def _ctx(query: str, wake_intent: float = 0.9) -> IntentContext:
    return IntentContext(
        speaker="大肚", raw_text=query, query=query, original_raw=query,
        wake_intent=wake_intent, stream_active=False, game_mode=False,
        is_owner=False, now=0.0,
    )


# ── SPECIFIC：含「的」+後段≥2字 → 完整曲目，不需 resolver ────────────────────

def test_specific_song_bids_high_no_missing_slots():
    """「播放陶喆的天天」→ SPECIFIC，missing_slots=[]（直接可播）。"""
    bid = _agent().bid(_ctx("播放陶喆的天天"))
    assert bid.confidence == SPECIFIC_CONF
    assert bid.missing_slots == []


def test_specific_song_another_artist():
    """「我想聽五月天的倔強」→ SPECIFIC（換 play keyword + artist 仍成立）。"""
    bid = _agent().bid(_ctx("我想聽五月天的倔強"))
    assert bid.confidence == SPECIFIC_CONF
    assert bid.missing_slots == []


# ── CURATION：純 artist token → 把選擇權交給 Marvin ─────────────────────────

def test_artist_only_bids_085_with_song_choice_slot():
    """「播放周杰倫」→ CURATION，conf=0.85，missing=["song_choice"]。

    這是 vector intent 最 user-facing 的 slice：沒給歌名 = 主動把選擇權交給 Marvin。
    conf 保持高（0.85）讓它在 bus 仍 winning，但 missing_slots 讓 dispatch 不直接呼 handler，
    而是先過 semantic_resolver 補完（Step 2 bus 路由）。
    """
    bid = _agent().bid(_ctx("播放周杰倫"))
    assert bid.confidence == CURATION_CONF
    assert bid.missing_slots == ["song_choice"]


def test_genre_only_also_curation():
    """「我想聽五月天」→ 同樣 CURATION（artist token，無歌名、無修飾）。"""
    bid = _agent().bid(_ctx("我想聽五月天"))
    assert bid.confidence == CURATION_CONF
    assert bid.missing_slots == ["song_choice"]


# ── Gap 1：「artist 的{類別詞}」是 curation，不是 SPECIFIC（2026-05-21 prod 實測）──

def test_artist_de_genre_word_is_curation_not_specific():
    """「播放陶喆的歌曲」→ CURATION（「歌曲」是類別詞非曲名），不該 SPECIFIC 直送 yt-dlp。"""
    bid = _agent().bid(_ctx("播放陶喆的歌曲"))
    assert bid.confidence == CURATION_CONF
    assert bid.missing_slots == ["song_choice"]


def test_artist_de_short_genre_also_curation():
    """「播放陶喆的歌」（單字類別詞）→ 同樣 CURATION。"""
    bid = _agent().bid(_ctx("播放陶喆的歌"))
    assert bid.confidence == CURATION_CONF
    assert bid.missing_slots == ["song_choice"]


def test_real_song_still_specific_not_genre_curation():
    """「播放陶喆的蘇珊說」→ 仍 SPECIFIC（蘇珊說是真曲名，非類別詞），不被 genre 規則搶走。"""
    bid = _agent().bid(_ctx("播放陶喆的蘇珊說"))
    assert bid.confidence == SPECIFIC_CONF
    assert bid.missing_slots == []


def test_directional_still_wins_over_genre():
    """「播放周杰倫符合我年紀的歌」結尾雖是「的歌」，directional 仍須先攔（0.50）。"""
    bid = _agent().bid(_ctx("播放周杰倫符合我年紀的歌"))
    assert bid.confidence == DIRECTIONAL_CONF
    assert bid.missing_slots == ["directional_resolution"]


# ── DIRECTIONAL：含抽象修飾 → 需 resolver 解出年代/情緒 ──────────────────────

def test_directional_bids_050_with_directional_slot():
    """「播放周杰倫符合我年紀的歌」→ DIRECTIONAL，conf=0.50，missing=["directional_resolution"]。

    含「的」卻不能判 SPECIFIC——directional modifier（符合.*的）必須先攔下。
    """
    bid = _agent().bid(_ctx("播放周杰倫符合我年紀的歌"))
    assert bid.confidence == DIRECTIONAL_CONF
    assert bid.missing_slots == ["directional_resolution"]


def test_directional_modifier_without_artist():
    """「放點適合深夜的」→ DIRECTIONAL（無 artist，純氛圍修飾）。"""
    bid = _agent().bid(_ctx("放點適合深夜的"))
    assert bid.confidence == DIRECTIONAL_CONF
    assert bid.missing_slots == ["directional_resolution"]


# ── 不可回歸：強訊號 / 控制詞維持 0.95，不被新分流搶走 ───────────────────────

def test_strong_play_still_095():
    """「放音樂」仍是 strong_play 0.95，三檔分流不影響強訊號。"""
    bid = _agent().bid(_ctx("放音樂"))
    assert bid.confidence == 0.95
    assert bid.missing_slots == []


def test_control_skip_still_095():
    """「換一首」仍是 control 0.95（控制詞優先序最高，不被 directional 干擾）。"""
    bid = _agent().bid(_ctx("換一首"))
    assert bid.confidence == 0.95
    assert bid.missing_slots == []
