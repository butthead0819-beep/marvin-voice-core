"""
EmotionMoodMixin — VoiceController 的情緒分類 / 心情貼圖 / 噪音提醒抽到獨立檔
（減肥 voice_controller.py），以 mixin 併入，self 身分不變、行為零改動。

守：mixin 在 MRO、方法搬到新模組、純函式 _classify_emotion 的韻律→情緒對映不變。
"""
from __future__ import annotations

import pytest


MOVED_METHODS = [
    "_update_emotion_from_audio",
    "_classify_marvin_self_emotion",
    "_classify_emotion",
    "_send_noise_nudge",
    "_send_mood_sticker",
]


def test_mixin_in_voice_controller_mro():
    from cogs.voice_controller import VoiceController
    from cogs.voice_controller_emotion import EmotionMoodMixin
    assert EmotionMoodMixin in VoiceController.__mro__


@pytest.mark.parametrize("name", MOVED_METHODS)
def test_method_moved_to_emotion_module(name):
    from cogs.voice_controller import VoiceController
    fn = getattr(VoiceController, name)
    assert fn.__module__ == "cogs.voice_controller_emotion", f"{name} 沒搬到 emotion 模組"


@pytest.mark.parametrize("prosody,expected", [
    (None, "neutral"),
    ({}, "neutral"),
    # 過短雜訊
    ({"wps": 9.0, "energy_variance": 99, "physical_duration": 0.5, "char_count": 10}, "neutral"),
    ({"wps": 9.0, "energy_variance": 99, "physical_duration": 2.0, "char_count": 2}, "neutral"),
    # 快 + 起伏大 = excited
    ({"wps": 7.0, "energy_variance": 60, "physical_duration": 2.0, "char_count": 10}, "excited"),
    # 快 + 平穩 = impatient
    ({"wps": 7.0, "energy_variance": 10, "physical_duration": 2.0, "char_count": 10}, "impatient"),
    # 慢 + 平穩 = depressed
    ({"wps": 1.0, "energy_variance": 10, "physical_duration": 2.0, "char_count": 10}, "depressed"),
    # 慢 + 起伏 = hesitant
    ({"wps": 1.0, "energy_variance": 60, "physical_duration": 2.0, "char_count": 10}, "hesitant"),
    # 正常速度 + 極平穩 = robotic
    ({"wps": 3.0, "energy_variance": 10, "physical_duration": 2.0, "char_count": 10}, "robotic"),
    # 其他 = neutral
    ({"wps": 3.0, "energy_variance": 40, "physical_duration": 2.0, "char_count": 10}, "neutral"),
])
def test_classify_emotion_prosody_mapping(prosody, expected):
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    assert vc._classify_emotion(prosody) == expected
