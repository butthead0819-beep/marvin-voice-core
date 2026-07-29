"""TDD: dj_topic_selector 的 meme_id 語義冷卻。

問題：TopicCooldownStore 用 SHA1(text) 作為 key，同一個 meme/事件
     換個說法就繞過冷卻（例如「搬家」→「最近在打包」→「新家」都是同一 meme）。

修法：
  - select_topic 接受 meme_id 覆寫 key（caller 知道語義 tag）
  - TopicCooldownStore.is_cool / mark_used 加 meme_id 參數
  - 無 meme_id → 退回舊行為（SHA1 hash）
"""
from __future__ import annotations

import tempfile
import time

import pytest

from dj_topic_selector import TopicCooldownStore, select_topic


def _fresh_store() -> TopicCooldownStore:
    return TopicCooldownStore(tempfile.mktemp(suffix=".json"))


# ── 1. 舊行為不變（SHA1 path）────────────────────────────────────────────────

def test_text_without_meme_id_still_uses_hash_cooldown():
    store = _fresh_store()
    store.mark_used("大肚在搬家")
    assert not store.is_cool("大肚在搬家")       # 同文字已冷卻
    assert store.is_cool("大肚在打包新家")        # 不同文字不受影響


# ── 2. meme_id 冷卻：同 meme_id 不管文字都算冷卻 ─────────────────────────────

def test_meme_id_cooldown_blocks_any_text_with_same_id():
    store = _fresh_store()
    store.mark_used("大肚在搬家", meme_id="搬家")
    # 不同文字但同 meme_id → 冷卻
    assert not store.is_cool("大肚在打包", meme_id="搬家")
    assert not store.is_cool("新家整理中", meme_id="搬家")


def test_meme_id_cooldown_does_not_block_other_meme_ids():
    store = _fresh_store()
    store.mark_used("大肚在搬家", meme_id="搬家")
    # 不同 meme_id → 不受影響
    assert store.is_cool("大肚要跑馬拉松", meme_id="馬拉松")


def test_meme_id_and_text_hash_are_independent_namespaces():
    """meme_id 冷卻不影響純 text hash 的冷卻（兩套 namespace 不相交）。"""
    store = _fresh_store()
    store.mark_used("大肚在搬家", meme_id="搬家")
    # 純 text key（沒有 meme_id）不受 meme 冷卻影響
    assert store.is_cool("大肚在搬家")  # 用 text hash，沒標過這個 hash key


# ── 3. select_topic 接 meme_id ───────────────────────────────────────────────

def test_select_topic_with_meme_id_cools_entire_meme():
    """select_topic 傳 meme_id → 選中後整個 meme_id 冷卻。"""
    store = _fresh_store()
    # life_cores 帶兩條同 meme 的說法
    life = [("大肚在搬家", "搬家"), ("大肚在打包", "搬家"), ("狗與露要環島", "環島")]
    topic, kind = select_topic(life, [], store)
    assert kind == "life"
    assert topic == "大肚在搬家"
    # 第二條同 meme 也被冷卻
    topic2, kind2 = select_topic([("大肚在打包", "搬家")], [], store)
    assert kind2 == "none"  # 冷卻中，選不到


def test_select_topic_plain_str_still_works():
    """舊介面（純字串，無 meme_id tuple）向後相容。"""
    store = _fresh_store()
    topic, kind = select_topic(["大肚在搬家"], [], store)
    assert kind == "life"
    assert topic == "大肚在搬家"


def test_select_topic_falls_back_to_interest_when_life_all_cooled():
    store = _fresh_store()
    store.mark_used("大肚在搬家", meme_id="搬家")
    life = [("大肚在打包", "搬家")]  # 同 meme，冷卻中
    interests = ["大肚喜歡九零年代金曲"]
    topic, kind = select_topic(life, interests, store)
    assert kind == "interest"
    assert "九零年代金曲" in topic
