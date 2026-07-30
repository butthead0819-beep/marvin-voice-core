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


def test_setup_early_logging_allowlists_intent_agents():
    """2026-07-30 事故：intent_agents/*.py 用 `getLogger(__name__)`（如
    playback_control_agent 的 skip/stop/pause/resume 執行結果 log）落在
    "intent_agents" 家族，沒被 cogs allowlist 涵蓋，INFO 被 root WARNING 吞掉
    ——查「法文下一首」漏執行案，IntentBus 判定 log 正常但 agent 實際執行 log
    整段歷史查無一筆，才挖出這個跟 2026-07-04 cogs 家族同型的坑。

    setup_early_logging() 會掛 root handler + 劫持 stdout，副作用不該污染
    pytest 行程本身（同檔其他兩個 test 守的就是這件事），所以在 subprocess
    裡跑，只斷言 intent_agents 家族的 effective level。
    """
    import os
    import subprocess
    import sys as _sys
    import tempfile
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp_cwd:
        out_path = Path(tmp_cwd) / "level_result.txt"
        # setup_early_logging() 會劫持 sys.stdout/stderr（見上兩個 test），print()
        # 之後就進不了 subprocess 的 stdout 管線，改用檔案 I/O 繞過劫持拿結果。
        script = (
            "import logging, main_discord; "
            "main_discord.setup_early_logging(); "
            "lg = logging.getLogger('intent_agents.playback_control_agent'); "
            f"open({str(out_path)!r}, 'w').write(logging.getLevelName(lg.getEffectiveLevel()))"
        )
        # cwd 指到 tmp dir，避免 setup_early_logging() 掛的 RotatingFileHandler
        # 把測試噪音寫進真的 prod bot_main.log/bot_stdout.log（6/12 同型事故）；
        # PYTHONPATH 補 repo_root 讓 subprocess 仍能 `import main_discord`。
        env = {**os.environ, "PYTHONPATH": str(repo_root)}
        result = subprocess.run(
            [_sys.executable, "-c", script],
            cwd=tmp_cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"subprocess 失敗: {result.stderr}"
        level = out_path.read_text().strip() if out_path.exists() else ""
    assert level == "INFO", (
        f"intent_agents.playback_control_agent effective level 應為 INFO，"
        f"實際={level!r} stderr={result.stderr[-500:]!r}"
    )
