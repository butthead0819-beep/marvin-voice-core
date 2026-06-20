"""
回歸守門：抽出的 voice_controller_*.py mixin 模組不可有「漏帶 import」的 undefined name。

背景：2026-06-20 strangler-fig 抽離期間，handle_summon 搬進 ConnectionMixin 卻沒把
BufferedF32MusicSource / S16ToF32MusicSource（local_mixing_source 的 class）一起帶過去，
造成 /summon 時 NameError（incident 2026-06-20-144734）。當初的缺名檢查有 `not isupper()`
過濾，把大寫 class 名整批跳過 → 盲點。

這個 meta-test 把那個檢查補成正式回歸：對每個抽出模組，凡是「它載入(load)用到、本地沒綁定、
不是 builtin、但 voice_controller.py 有 import」的名字 → 代表抽離時漏帶 import，fail。
只比對「voice_controller 有 import 的名」可避免誤報 runtime 注入的屬性。
"""
from __future__ import annotations

import ast
import builtins
import glob
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _vc_imported_names() -> set[str]:
    tree = ast.parse(open(os.path.join(ROOT, "cogs", "voice_controller.py")).read())
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                names.add(a.asname or a.name)
    return names


def _bound_and_loaded(tree):
    bound, loaded = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                bound.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Store):
                bound.add(n.id)
            elif isinstance(n.ctx, ast.Load):
                loaded.add(n.id)
        elif isinstance(n, (ast.ExceptHandler,)) and n.name:
            bound.add(n.name)
    return bound, loaded


MODULES = sorted(glob.glob(os.path.join(ROOT, "cogs", "voice_controller_*.py")))


@pytest.mark.parametrize("path", MODULES, ids=lambda p: os.path.basename(p))
def test_extracted_module_has_no_missing_import(path):
    vc_imports = _vc_imported_names()
    bound, loaded = _bound_and_loaded(ast.parse(open(path).read()))
    builtin_names = set(dir(builtins))
    missing = sorted(
        name for name in loaded
        if name not in bound and name not in builtin_names and name in vc_imports
    )
    assert not missing, (
        f"{os.path.basename(path)} 用到但沒帶過來的 import（抽離漏帶）：{missing}。"
        f"請在該模組補上對應 import（來源見 voice_controller.py）。"
    )
