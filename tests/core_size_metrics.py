"""
防胖守門共用量測（test_voice_controller_size_budget / test_music_cog_size_budget 用）。

為什麼不用行數：行數預算會被「把兩行壓成一行」繞過（48069ba 為了淨 0 行把 if 本體
併上同一行），指標守住了、可讀性變差。改用 AST 量，壓行騙不過：
  - statement_count：檔內 ast.stmt 節點數（`if x: a = b` 仍算 If + Assign 兩個）；
    import 不算——新 IntentAgent 的接線（一行 import + list 裡一個元素）因此是 0，
    不必再為「+2 接線成本」調預算
  - self_attrs：檔內任何地方被賦值的 self.X 名稱集合——共用可變狀態才是耦合真正長大的訊號
  - method_statements：每個 method 內的 statement 數（不含 def 本身，同樣不算 import）
"""
from __future__ import annotations

import ast
from pathlib import Path


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _stmts(node: ast.AST) -> int:
    return sum(
        isinstance(n, ast.stmt) and not isinstance(n, (ast.Import, ast.ImportFrom))
        for n in ast.walk(node)
    )


def statement_count(path: Path) -> int:
    return _stmts(_tree(path))


def self_attrs(path: Path) -> set[str]:
    return {
        n.attr for n in ast.walk(_tree(path))
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
        and isinstance(n.value, ast.Name) and n.value.id == "self"
    }


def method_statements(path: Path, class_name: str) -> dict[str, int]:
    cls = next(n for n in _tree(path).body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return {
        m.name: _stmts(m) - 1
        for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
