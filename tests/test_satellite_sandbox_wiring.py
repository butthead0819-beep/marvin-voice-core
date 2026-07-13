"""satellite 啟動時啟用 ephemeral 記憶沙盒的 wiring 測試。

預設 ON（沙盒化＝可與 24/7 Discord bot 並存不搶寫）；env MARVIN_MEMORY_SANDBOX=0
＝escape hatch（舊工作流：停掉 Discord bot、讓 satellite 直接寫正本）。
"""
import os

import pytest

import memory_sandbox
from main_satellite import maybe_activate_memory_sandbox


@pytest.fixture(autouse=True)
def _clean():
    memory_sandbox.deactivate()
    os.environ.pop("MARVIN_MEMORY_SANDBOX", None)
    yield
    memory_sandbox.deactivate()
    os.environ.pop("MARVIN_MEMORY_SANDBOX", None)


def test_default_activates_sandbox():
    assert maybe_activate_memory_sandbox({}) is True
    assert memory_sandbox.active() is True


def test_env_zero_is_escape_hatch():
    assert maybe_activate_memory_sandbox({"MARVIN_MEMORY_SANDBOX": "0"}) is False
    assert memory_sandbox.active() is False


def test_env_one_activates():
    assert maybe_activate_memory_sandbox({"MARVIN_MEMORY_SANDBOX": "1"}) is True
    assert memory_sandbox.active() is True
