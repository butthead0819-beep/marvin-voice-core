"""Tests for the dedicated per-player callback_queue (proactive group-memory callback, T1).

Eng-review locked design: callback memories live in their OWN `callback_queue` field,
NOT news_queue — so they don't collide with news's cap-3 eviction, don't leak into the
reactive prompt via get_rich_context (fail-private stays real), and get their own render.
Delivery is idempotent: peek (stable until consumed) → deliver → consume on success.
TTL = 7 days so an undelivered callback (member never returns) doesn't live forever.
"""
import time
from suki_memory import MemoryManager, _new_player, _repair_player


def _mk(tmp_path):
    return MemoryManager(
        db_path=str(tmp_path / "cb.db"),
        json_compat_path=str(tmp_path / "cb.json"),
    )


# ── enqueue / peek ────────────────────────────────────────────────────────────

def test_enqueue_shareable_then_peek_returns_it(tmp_path):
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "你說要戒咖啡", shareable=True)
    item = mem.peek_shareable_callback("Alice")
    assert item is not None
    assert item["text"] == "你說要戒咖啡"


def test_peek_is_stable_until_consumed(tmp_path):
    """Idempotent delivery: peek must not remove — same item returned until consume."""
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "hi", shareable=True)
    first = mem.peek_shareable_callback("Alice")
    second = mem.peek_shareable_callback("Alice")
    assert first["text"] == "hi"
    assert second["text"] == "hi"


def test_peek_skips_private_items(tmp_path):
    """fail-private: a non-shareable callback is never surfaced for proactive delivery."""
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "私下的話", shareable=False)
    assert mem.peek_shareable_callback("Alice") is None


def test_peek_returns_oldest_shareable_first(tmp_path):
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "first", shareable=True)
    mem.enqueue_callback("Alice", "second", shareable=True)
    assert mem.peek_shareable_callback("Alice")["text"] == "first"


def test_peek_unknown_player_returns_none(tmp_path):
    assert _mk(tmp_path).peek_shareable_callback("Ghost") is None


# ── consume (idempotent delivery) ─────────────────────────────────────────────

def test_consume_removes_delivered_item(tmp_path):
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "deliver me", shareable=True)
    item = mem.peek_shareable_callback("Alice")
    mem.consume_callback("Alice", item)
    assert mem.peek_shareable_callback("Alice") is None


def test_consume_only_removes_the_matching_item(tmp_path):
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "keep", shareable=True)
    mem.enqueue_callback("Alice", "drop", shareable=True)
    # peek returns oldest ("keep"); consuming it leaves "drop"
    mem.consume_callback("Alice", mem.peek_shareable_callback("Alice"))
    assert mem.peek_shareable_callback("Alice")["text"] == "drop"


# ── TTL ───────────────────────────────────────────────────────────────────────

def test_ttl_expired_callback_not_surfaced(tmp_path):
    """Undelivered callback older than 7 days expires — no creepy weeks-late surfacing."""
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "stale", shareable=True)
    p = mem.get_player_memory("Alice")
    p["callback_queue"][0]["ts"] = time.time() - 8 * 86400  # 8 days old
    assert mem.peek_shareable_callback("Alice") is None


# ── isolation from news_queue (the whole point of the dedicated field) ─────────

def test_callback_does_not_touch_news_queue(tmp_path):
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "callback only", shareable=True)
    assert mem.get_player_memory("Alice")["news_queue"] == []


def test_callback_does_not_leak_into_get_rich_context(tmp_path):
    """fail-private real: callback content must NOT appear in the reactive-prompt context."""
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "私密承諾不可洩漏", shareable=False)
    mem.enqueue_callback("Alice", "可分享但仍不該進prompt", shareable=True)
    assert "私密承諾不可洩漏" not in mem.get_rich_context("Alice")
    assert "可分享但仍不該進prompt" not in mem.get_rich_context("Alice")


# ── cap / persistence / migration ──────────────────────────────────────────────

def test_cap_drops_oldest_callback(tmp_path):
    mem = _mk(tmp_path)
    for i in range(12):
        mem.enqueue_callback("Alice", f"cb{i}", shareable=True)
    q = mem.get_player_memory("Alice")["callback_queue"]
    assert len(q) == 10  # cap
    assert q[0]["text"] == "cb2"  # cb0, cb1 evicted


def test_callback_queue_persists_across_instances(tmp_path):
    db = str(tmp_path / "p.db")
    j = str(tmp_path / "p.json")
    m1 = MemoryManager(db_path=db, json_compat_path=j)
    m1.enqueue_callback("Bob", "remember", shareable=True)
    m2 = MemoryManager(db_path=db, json_compat_path=j)
    assert m2.peek_shareable_callback("Bob")["text"] == "remember"


def test_repair_player_adds_callback_queue_to_old_record():
    repaired = _repair_player({"likes": ["coffee"]})
    assert repaired["callback_queue"] == []


# ── kill-switch: per-player mute (T4) ──────────────────────────────────────────
def test_muted_player_peek_returns_none(tmp_path):
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "戒咖啡", shareable=True)
    mem.set_callbacks_muted("Alice", True)
    assert mem.peek_shareable_callback("Alice") is None


def test_unmute_restores_peek(tmp_path):
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "戒咖啡", shareable=True)
    mem.set_callbacks_muted("Alice", True)
    mem.set_callbacks_muted("Alice", False)
    assert mem.peek_shareable_callback("Alice")["text"] == "戒咖啡"


def test_default_not_muted(tmp_path):
    assert _mk(tmp_path).is_callbacks_muted("Alice") is False


def test_repair_adds_callbacks_muted():
    assert _repair_player({"likes": ["coffee"]})["callbacks_muted"] is False


def test_new_player_has_callback_queue():
    assert _new_player()["callback_queue"] == []


# ── peek_all_shareable_callbacks (MemoryCallbackAgent v3, plan-eng-review D8) ──
# 新 API：回該 player 所有 shareable 且未過期 callback 的 list。
# 沿用 peek_shareable_callback 的 TTL / mute / shareable filter 邏輯，但回 list 不是 first。

def test_peek_all_empty_queue_returns_empty_list(tmp_path):
    """未知 player 或無 callback → []，不是 None。"""
    assert _mk(tmp_path).peek_all_shareable_callbacks("Ghost") == []


def test_peek_all_expired_returns_empty_and_prunes(tmp_path):
    """全過 TTL → []，同 peek 一樣順手剪掉過期項。"""
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "stale1", shareable=True)
    mem.enqueue_callback("Alice", "stale2", shareable=True)
    p = mem.get_player_memory("Alice")
    for item in p["callback_queue"]:
        item["ts"] = time.time() - 8 * 86400  # 8 days old
    assert mem.peek_all_shareable_callbacks("Alice") == []


def test_peek_all_muted_player_returns_empty_list(tmp_path):
    """muted player → []（不是 None），跟 peek_shareable_callback 的 mute kill-switch 一致。"""
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "戒咖啡", shareable=True)
    mem.enqueue_callback("Alice", "學日文", shareable=True)
    mem.set_callbacks_muted("Alice", True)
    assert mem.peek_all_shareable_callbacks("Alice") == []


def test_peek_all_filters_private_keeps_shareable_in_order(tmp_path):
    """混 shareable/private → 只回 shareable，按 queue 順序（oldest first）。"""
    mem = _mk(tmp_path)
    mem.enqueue_callback("Alice", "private1", shareable=False)
    mem.enqueue_callback("Alice", "share1", shareable=True)
    mem.enqueue_callback("Alice", "private2", shareable=False)
    mem.enqueue_callback("Alice", "share2", shareable=True)
    items = mem.peek_all_shareable_callbacks("Alice")
    assert [it["text"] for it in items] == ["share1", "share2"]
