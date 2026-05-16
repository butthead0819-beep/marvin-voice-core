"""
TDD: 語音-聊天室相關性分析
蘋蘋說了什麼 → 之後 60s 聊天室 intent 飆升多少
"""
import sqlite3
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE stream_transcript (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            text    TEXT NOT NULL,
            ts      TEXT NOT NULL
        );
        CREATE TABLE messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            channel      TEXT NOT NULL,
            username     TEXT NOT NULL,
            message      TEXT NOT NULL,
            ts           TEXT NOT NULL,
            intent_type  TEXT NOT NULL DEFAULT 'general',
            intent_score INTEGER NOT NULL DEFAULT 0,
            session_date TEXT NOT NULL DEFAULT ''
        );
    """)
    return conn


def iso(dt: datetime) -> str:
    return dt.isoformat()


BASE = datetime(2026, 5, 16, 11, 0, 0, tzinfo=timezone.utc)


class TestCorrelateVoiceToChat(unittest.TestCase):

    def setUp(self):
        from twitch_report import correlate_voice_to_chat
        self.correlate = correlate_voice_to_chat
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _add_transcript(self, text: str, offset_secs: int):
        ts = iso(BASE + timedelta(seconds=offset_secs))
        self.conn.execute(
            "INSERT INTO stream_transcript (channel, text, ts) VALUES (?,?,?)",
            ("pinpinponpon627", text, ts)
        )
        self.conn.commit()

    def _add_message(self, username: str, message: str, offset_secs: int,
                     intent_type="general", intent_score=0):
        ts = iso(BASE + timedelta(seconds=offset_secs))
        self.conn.execute(
            "INSERT INTO messages (channel, username, message, ts, intent_type, intent_score, session_date)"
            " VALUES (?,?,?,?,?,?,?)",
            ("pinpinponpon627", username, message, ts, intent_type, intent_score, "2026-05-16")
        )
        self.conn.commit()

    def test_returns_list(self):
        result = self.correlate(self.conn, "pinpinponpon627", since=iso(BASE - timedelta(hours=1)))
        self.assertIsInstance(result, list)

    def test_chat_within_window_is_counted(self):
        self._add_transcript("大家快來訂閱！", offset_secs=0)
        self._add_message("user_a", "訂閱了！", offset_secs=30,
                          intent_type="subscription_intent", intent_score=3)
        result = self.correlate(self.conn, "pinpinponpon627", since=iso(BASE - timedelta(hours=1)))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["intent_msgs"], 1)
        self.assertEqual(result[0]["intent_score_sum"], 3)

    def test_chat_before_audio_is_not_counted(self):
        self._add_transcript("大家快來訂閱！", offset_secs=100)
        self._add_message("user_a", "訂閱了！", offset_secs=50,
                          intent_type="subscription_intent", intent_score=3)
        result = self.correlate(self.conn, "pinpinponpon627", since=iso(BASE - timedelta(hours=1)))
        self.assertEqual(result[0]["intent_msgs"], 0)

    def test_chat_after_window_is_not_counted(self):
        self._add_transcript("大家快來訂閱！", offset_secs=0)
        self._add_message("user_a", "訂閱了！", offset_secs=120,  # 超過 60s window
                          intent_type="subscription_intent", intent_score=3)
        result = self.correlate(self.conn, "pinpinponpon627", since=iso(BASE - timedelta(hours=1)))
        self.assertEqual(result[0]["intent_msgs"], 0)

    def test_sorted_by_intent_score_desc(self):
        self._add_transcript("普通話題", offset_secs=0)
        self._add_transcript("訂閱週年！", offset_secs=300)
        self._add_message("u1", "sub!", offset_secs=320,
                          intent_type="subscription_intent", intent_score=5)
        self._add_message("u2", "訂閱！", offset_secs=330,
                          intent_type="subscription_intent", intent_score=3)
        result = self.correlate(self.conn, "pinpinponpon627", since=iso(BASE - timedelta(hours=1)))
        self.assertGreater(result[0]["intent_score_sum"], result[1]["intent_score_sum"])

    def test_window_secs_is_configurable(self):
        self._add_transcript("快來！", offset_secs=0)
        self._add_message("u1", "訂閱", offset_secs=90,
                          intent_type="subscription_intent", intent_score=3)
        result_60 = self.correlate(self.conn, "pinpinponpon627",
                                   since=iso(BASE - timedelta(hours=1)), window_secs=60)
        result_120 = self.correlate(self.conn, "pinpinponpon627",
                                    since=iso(BASE - timedelta(hours=1)), window_secs=120)
        self.assertEqual(result_60[0]["intent_msgs"], 0)
        self.assertEqual(result_120[0]["intent_msgs"], 1)


if __name__ == "__main__":
    unittest.main()
