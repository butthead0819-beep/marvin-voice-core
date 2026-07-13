"""memory_sandbox.py — ephemeral 記憶沙盒：satellite 唯讀繼承正本、寫入 no-op、斷線丟棄。

滿足「satellite/discord 模式共存不搶寫正本」（見 design_ephemeral_sandbox_memory）。
啟用後（activate() 或 env MARVIN_MEMORY_SANDBOX=1）：
  - connect() 開唯讀連線（mode=ro）＝**物理牆**，正本 marvin.db 寫不進（漏網寫路徑會拋錯）
  - 各 store 的寫入方法用 active() 早退＝**graceful no-op**（不撞唯讀牆、pipeline 不崩）
  - 整檔 JSON 覆寫（music `_save` / suki `_export_json` 等）同樣 no-op

雙層＝主要靠 no-op（優雅）、read-only 當防線（漏網也物理寫不進）。ephemeral 語意＝
變更只留 RAM（各 store 的 cache/session 狀態）、進程結束即忘、正本一個 byte 沒被碰。

⚠️ 只在 satellite 進程 activate；24/7 Discord bot 進程絕不 activate（它是正本唯一寫者）。
"""
import os
import sqlite3

_active = False

_ENV_FLAG = "MARVIN_MEMORY_SANDBOX"
_TRUE = ("1", "true", "yes", "on")


def activate() -> None:
    """啟用沙盒（同時設 env，讓晚於 import 才建構的 store 也看得到）。"""
    global _active
    _active = True
    os.environ[_ENV_FLAG] = "1"


def deactivate() -> None:
    """關閉沙盒（測試用）。"""
    global _active
    _active = False
    os.environ.pop(_ENV_FLAG, None)


def active() -> bool:
    """沙盒是否啟用。env 與 process flag 任一為真即真（跨 import 時序安全）。"""
    if _active:
        return True
    return os.environ.get(_ENV_FLAG, "").strip().lower() in _TRUE


def connect(db_path: str, **kwargs) -> sqlite3.Connection:
    """取得 sqlite 連線：沙盒啟用時開唯讀（物理牆），否則正常讀寫。

    `:memory:` 永遠正常開（純 RAM 暫存/測試 DB，非正本、無並行風險）。
    唯讀讀 WAL 正本：只要另一進程（Discord bot）持有 -shm 即可讀到最新（實測成立）。
    """
    if active() and db_path != ":memory:":
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, **kwargs)
    return sqlite3.connect(db_path, **kwargs)
