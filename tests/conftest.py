"""Shared test isolation.

Redirect stt_cleaner 的所有寫檔路徑到 tmp，讓任何測試都不會污染 prod records/
（feedback_stt_test_isolation：cleaner 測試曾寫到真 records/）。autouse → 每個測試生效。
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_stt_cleaner_writes(tmp_path, monkeypatch):
    try:
        import stt_cleaner  # noqa: F401
    except Exception:
        return
    for attr, fn in (("_CORRECTIONS_LOG", "corr.jsonl"),
                     ("_LOCAL_CORRECTIONS_PATH", "corr.json"),
                     ("_GATE_DROP_LOG", "gate_drops.jsonl")):
        if hasattr(stt_cleaner, attr):
            monkeypatch.setattr(f"stt_cleaner.{attr}", tmp_path / fn, raising=False)
    yield
