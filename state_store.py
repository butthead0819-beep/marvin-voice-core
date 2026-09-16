"""
🗄️ StateStore — 共用的小型 JSON-backed key-value store 工具。

背景：專案裡每個子系統目前都手刻自己的 JSON 讀寫（music_memory.py、
dj_topic_selector.py 的 TopicCooldownStore 等），各自實作 load/save + fail-open，
其中 music_memory.json 還曾發生併發寫入互蓋的 race bug（見 MEMORY
feedback_music_memory_concurrent_write_race）。

這個模組把「atomic write + fail-open read + 可選檔案鎖」這三個重複被手刻的行為
收斂成一個共用 class，之後（Phase B，不在這次改動範圍內）再把現有幾個 JSON
store 遷移過去用。這次只新增這個獨立模組，不動任何既有檔案。

設計哲學照抄專案既有慣例：
- Atomic write：先寫暫存檔（同目錄，避免跨檔案系統 rename 失敗），
  完成後用 os.replace() 原子換檔，讀者不會讀到寫一半的檔案。
- Fail-open：檔案不存在、JSON 壞掉、或任何 IO 例外，一律回傳空/預設值，
  絕不讓壞檔炸掉呼叫端的 pipeline。
- 鎖只用標準庫 fcntl.flock（POSIX-only，這專案只跑 macOS/Linux）——
  專案沒有 filelock 這類第三方依賴，不新增。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from typing import Any, Callable


class StateStore:
    """單一 JSON 檔案的 key-value store，帶 atomic write + fail-open read。

    用法：
        store = StateStore("data/foo.json")
        data = store.load()               # 壞檔/不存在都回傳 {}（或建構時指定的 default）
        store.save({"a": 1})              # atomic write
        store.update(lambda d: d.update(x=1) or d)  # 鎖保護的 read-modify-write
    """

    def __init__(self, path: str, *, default: Any = None):
        self._path = path
        # default 若沒給，用 {}；每次回傳前 deepcopy 避免呼叫端改到同一個物件。
        self._default = {} if default is None else default

    def load(self) -> Any:
        """讀取整份資料。檔案不存在/JSON 壞掉/IO 例外 → fail-open 回傳 default。"""
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return self._clone_default()

    def save(self, data: Any) -> None:
        """把 data 整份 atomic 寫入：先寫同目錄暫存檔，完成後 os.replace() 換檔。

        fail-open：寫不進去（例如磁碟滿）不拋例外，不影響呼叫端的其他邏輯，
        下次呼叫再重試即可（跟 TopicCooldownStore._save 的哲學一致）。
        """
        try:
            dirname = os.path.dirname(self._path) or "."
            os.makedirs(dirname, exist_ok=True)
            # 暫存檔跟正式檔同目錄，確保 os.replace 是同一個檔案系統內的原子操作
            # （跨檔案系統 rename 會失敗，而不是「不保證原子」而已）。
            tmp_path = f"{self._path}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except OSError:
            pass  # fail-open：寫失敗不影響功能

    @contextlib.contextmanager
    def _locked(self):
        """開一個跟資料檔同路徑的 .lock 檔，flock 拿到才進入 critical section。

        用獨立 lock 檔（不是直接 flock 資料檔本身）是刻意的：這樣鎖的生命週期
        跟資料檔的 atomic replace 互不干擾——replace 換掉資料檔不會動到 lock 檔
        的 inode，鎖依然有效。
        """
        lock_path = f"{self._path}.lock"
        dirname = os.path.dirname(lock_path) or "."
        os.makedirs(dirname, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def update(self, fn: Callable[[Any], Any]) -> Any:
        """鎖保護下做 read-modify-write：拿鎖 → load 現有資料 → 餵給 fn → save 結果。

        這是給 music_memory.json 那種「讀-改-寫」race 場景用的一步到位介面：
        兩個 process 同時呼叫 update() 時，第二個會等第一個的 flock 釋放，
        讀到的是第一個已經寫回去的最新資料，不會互蓋。

        fn 簽名：(現有資料) -> 新資料。fn 回傳值就是要存回去的完整內容
        （不是 patch/diff），跟 dict.update 的鏈式寫法搭配時記得回傳 dict 本身。
        """
        with self._locked():
            current = self.load()
            new_data = fn(current)
            self.save(new_data)
            return new_data

    def _clone_default(self) -> Any:
        if isinstance(self._default, dict):
            return dict(self._default)
        if isinstance(self._default, list):
            return list(self._default)
        return self._default
