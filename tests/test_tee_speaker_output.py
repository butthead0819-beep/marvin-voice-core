"""TDD：TeeSpeakerOutput——write(frame)/close() 扇出給多個下游 output。

驗：
(a) write() 轉呼叫每個下游的 write()，帶同一個 frame
(b) close() 轉呼叫每個下游的 close()
(c) 其中一個下游 write() 丟例外，不擋其他下游收到、不往外冒
(d) 其中一個下游 close() 丟例外，不擋其他下游收到、不往外冒
"""
from __future__ import annotations

from unittest.mock import MagicMock

from marvin_voice_core.tee_speaker_output import TeeSpeakerOutput


def test_write_forwards_to_every_output():
    a, b = MagicMock(), MagicMock()
    tee = TeeSpeakerOutput([a, b])

    tee.write(b"\x01\x02")

    a.write.assert_called_once_with(b"\x01\x02")
    b.write.assert_called_once_with(b"\x01\x02")


def test_close_forwards_to_every_output():
    a, b = MagicMock(), MagicMock()
    tee = TeeSpeakerOutput([a, b])

    tee.close()

    a.close.assert_called_once()
    b.close.assert_called_once()


def test_write_error_in_one_output_does_not_block_others():
    dead = MagicMock()
    dead.write.side_effect = RuntimeError("boom")
    alive = MagicMock()
    tee = TeeSpeakerOutput([dead, alive])

    tee.write(b"\x01\x02")   # 不該往外冒例外

    alive.write.assert_called_once_with(b"\x01\x02")


def test_close_error_in_one_output_does_not_block_others():
    dead = MagicMock()
    dead.close.side_effect = RuntimeError("boom")
    alive = MagicMock()
    tee = TeeSpeakerOutput([dead, alive])

    tee.close()   # 不該往外冒例外

    alive.close.assert_called_once()
