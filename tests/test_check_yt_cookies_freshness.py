"""scripts/check_yt_cookies_freshness.py::earliest_expiry 純函式測試。"""
import datetime

from scripts.check_yt_cookies_freshness import earliest_expiry


def _write(tmp_path, lines):
    path = tmp_path / "cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n")
    return str(path)


def test_earliest_expiry_picks_soonest_non_session_cookie(tmp_path):
    soon = int((datetime.datetime.now() + datetime.timedelta(days=5)).timestamp())
    later = int((datetime.datetime.now() + datetime.timedelta(days=100)).timestamp())
    path = _write(tmp_path, [
        f".youtube.com\tTRUE\t/\tTRUE\t{later}\tNID\tval1",
        f".youtube.com\tTRUE\t/\tTRUE\t{soon}\tSID\tval2",
    ])
    out = earliest_expiry(path)
    assert abs((out - datetime.datetime.fromtimestamp(soon)).total_seconds()) < 1


def test_earliest_expiry_ignores_session_cookies(tmp_path):
    path = _write(tmp_path, [
        ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tval1",  # session cookie, expiry=0
    ])
    assert earliest_expiry(path) is None


def test_earliest_expiry_ignores_comment_and_blank_lines(tmp_path):
    soon = int((datetime.datetime.now() + datetime.timedelta(days=5)).timestamp())
    path = tmp_path / "cookies.txt"
    path.write_text(f"# comment\n\n.youtube.com\tTRUE\t/\tTRUE\t{soon}\tSID\tval\n")
    out = earliest_expiry(str(path))
    assert out is not None
