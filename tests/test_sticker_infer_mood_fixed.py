"""infer_mood() 拔掉 toxicity 參數後行為固定為厭世基調，驗證四條規則。"""
from sticker_manager import infer_mood


def test_two_or_more_question_marks_returns_thinking():
    assert infer_mood("這是什麼？為什麼會這樣？", "neutral") == "thinking"


def test_frustrated_or_angry_user_returns_contempt():
    assert infer_mood("隨便啦", "frustrated") == "contempt"
    assert infer_mood("隨便啦", "angry") == "contempt"


def test_excited_user_returns_contempt():
    assert infer_mood("好耶", "excited") == "contempt"


def test_attack_word_returns_angry():
    assert infer_mood("滾啦", "neutral") == "angry"


def test_other_cases_return_contempt():
    assert infer_mood("今天天氣不錯", "neutral") == "contempt"
