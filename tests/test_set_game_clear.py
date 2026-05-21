"""TDD: /set_game 接受清空哨兵（無 / none / 關閉）→ current_game 清為 None。

根因背景：current_game 曾卡在 'Claude code' 並持久化於 suki_dna.json，導致 5 分鐘
日記的遊戲守衛每輪 return，日記停擺數日。/set_game 原本沒有清除路徑（game_name 必填字串）。
本測試鎖定：set_game_async 命中哨兵時清空，且不觸發昂貴的黑話字典載入。
"""
import pytest
from unittest.mock import AsyncMock

from gemini_router_llm import GeminiRouterLLMMixin, is_clear_game_sentinel


class _FakeRouter(GeminiRouterLLMMixin):
    """最小 router 替身：只提供 set_game_async 需要的屬性。"""

    def __init__(self):
        self.current_game = "Claude code"
        self.dna = {"current_game": "Claude code"}
        self.game_dict_string = "舊黑話字典"
        self.saved = []
        self.dict_manager = AsyncMock()
        self.dict_manager.get_or_create_dict = AsyncMock(return_value="新黑話字典")

    def save_dna(self, dna):
        self.saved.append(dict(dna))


# ---- 純函式：哨兵判定（大小寫 / 前後空白不敏感）----

@pytest.mark.parametrize("name", ["無", "none", "None", "NONE", "關閉", " 無 ", " None "])
def test_is_clear_game_sentinel_true_for_clear_words(name):
    assert is_clear_game_sentinel(name) is True


@pytest.mark.parametrize("name", ["Valorant", "Apex Legends", "戰棋", "", "gamenone"])
def test_is_clear_game_sentinel_false_for_real_games(name):
    assert is_clear_game_sentinel(name) is False


# ---- set_game_async 清空路徑 ----

@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", ["無", "none", "關閉"])
async def test_set_game_async_clears_current_game_on_sentinel(sentinel):
    r = _FakeRouter()
    result = await r.set_game_async(sentinel)

    assert r.current_game is None
    assert r.dna["current_game"] is None
    assert r.game_dict_string == ""
    assert result == ""
    # 清空不該觸發昂貴的字典載入
    r.dict_manager.get_or_create_dict.assert_not_called()
    # DNA 有被持久化、且寫入的是 None
    assert r.saved and r.saved[-1]["current_game"] is None


# ---- set_game_async 正常設定路徑（回歸保護）----

@pytest.mark.asyncio
async def test_set_game_async_sets_real_game_unchanged():
    r = _FakeRouter()
    result = await r.set_game_async("Valorant")

    assert r.current_game == "Valorant"
    assert r.dna["current_game"] == "Valorant"
    r.dict_manager.get_or_create_dict.assert_awaited_once_with("Valorant")
    assert result == "新黑話字典"
