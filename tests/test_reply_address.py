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
