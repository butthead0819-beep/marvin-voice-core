from reply_address import address_prefix


def test_two_members_adds_prefix():
    assert address_prefix("showay", ["showay", "B"], "句子") == "showay，"


def test_single_member_no_prefix():
    assert address_prefix("showay", ["showay"], "句子") == ""


def test_zero_members_no_prefix():
    assert address_prefix("showay", [], "句子") == ""


def test_text_already_starts_with_speaker_case_insensitive():
    assert address_prefix("showay", ["showay", "B"], "Showay，你好") == ""


def test_empty_speaker_no_prefix():
    assert address_prefix("", ["showay", "B"], "句子") == ""


def test_name_prefix_boundary_false_positive_adds_prefix():
    """10/23 review：「Also」誤判成 "Al" 開頭，名字後接英數字不算叫過名字。"""
    assert address_prefix("Al", ["Al", "B"], "Also good") == "Al，"


def test_name_prefix_boundary_real_address_no_prefix():
    assert address_prefix("Al", ["Al", "B"], "Al, hi") == ""


def test_name_prefix_exact_whole_sentence_no_prefix():
    assert address_prefix("showay", ["showay", "B"], "showay") == ""
