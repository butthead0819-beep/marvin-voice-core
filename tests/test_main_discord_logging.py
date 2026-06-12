"""main_discord import 時的 logging 副作用防護。

2026-06-12 事故：tests/test_bridge_wiring.py import main_discord →
setup_early_logging() 在 import 時就把 RotatingFileHandler(bot_main.log)
掛上 root logger 並劫持 sys.stdout/stderr → 整個 pytest 套件的 WARNING
（fake provider a/b、marvine_chat typo、exploding agent）灌進真 bot_main.log，
導致 prod 健康度誤判（假的 Tier-1 AttributeError 事故）。

約定：pytest 環境下 import main_discord 不得掛 bot_main.log handler、
不得劫持 stdout。prod（python main_discord.py）行為不變。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler


def test_import_does_not_attach_bot_log_handler():
    import main_discord  # noqa: F401

    bad = [
        h for h in logging.getLogger().handlers
        if isinstance(h, RotatingFileHandler)
        and "bot_main.log" in getattr(h, "baseFilename", "")
    ]
    assert bad == [], "pytest import main_discord 不該掛 bot_main.log handler（會污染 prod log）"


def test_import_does_not_hijack_stdout():
    import sys

    import main_discord  # noqa: F401

    assert type(sys.stdout).__name__ != "_StreamToLogger", \
        "pytest import main_discord 不該劫持 sys.stdout"
