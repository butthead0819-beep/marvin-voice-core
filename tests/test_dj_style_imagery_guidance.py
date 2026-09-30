"""9/30：風格規則原本寫「例如：上班想躺平、購物車塞滿、剛泡好的熱茶、窗外安靜的街道」，
小模型素材薄時直接照抄（近 7 天 10% 口白含這四個之一）。改成引導「畫面從這次脈絡長出來」，
A/B：氛圍 mode 萬用畫面 10/10 → 2/10。"""
from dj_prompt_builder import build_dj_interjection_prompt


def test_style_rule_guides_imagery_from_context():
    prompt = build_dj_interjection_prompt("歌曲：周杰倫 - 晴天")
    assert "畫面要從這次的脈絡裡長出來" in prompt


def test_style_rule_has_no_fixed_imagery_examples_to_copy():
    prompt = build_dj_interjection_prompt("歌曲：周杰倫 - 晴天")
    assert "剛泡好的熱茶" not in prompt
