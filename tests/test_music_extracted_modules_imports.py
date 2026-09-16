"""
回歸守門：抽出的 music_cog_*.py mixin 模組（music_cog.py 拆解計畫）不可有三種常見
抽離事故：漏帶 import、裸用被組合的 class 名（該用 self/cls）、class-body 層級引用
「同模組裡沒先定義」的常數。

背景：這三種檢查對齊 tests/test_extracted_modules_imports.py 對 voice_controller_*.py
的做法（同一套 incident 教訓：2026-06-20 BufferedF32MusicSource 漏帶 import 造成
/summon NameError），並補上該檔案結構性抓不到的兩種：
  (a) 抽出模組裡有 @staticmethod 硬寫 `MusicCog.X` 而非 `self.X`/`cls.X`——一旦搬出
      music_cog.py 就是循環 import（music_cog_dj_lyrics.py 的 _autopilot_dj_phrase
      地雷，見 music_cog 拆解計畫）。
  (b) 抽出模組的 class-body 層級（不在任何 method 內）用到「同模組 class-body 裡沒有
      先定義」的名字——這類常數是靠 Python 循序 class-body namespace 解析、不是
      MRO，跟本檔案物理位置留在哪個檔案高度相關（_DJ_TEMPLATES 衍生常數地雷）。
"""
from __future__ import annotations

import ast
import builtins
import glob
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSED_CLASS_NAME = "MusicCog"


def _mc_imported_names() -> set[str]:
    tree = ast.parse(open(os.path.join(ROOT, "cogs", "music_cog.py")).read())
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


def _bare_composed_class_refs(tree) -> list[str]:
    """(a) 裸用 MusicCog（Load context）——該用 self/cls 的地方寫死了 class 名。"""
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == COMPOSED_CLASS_NAME and isinstance(n.ctx, ast.Load):
            hits.append(f"line {n.lineno}")
    return hits


def _module_level_bound_names(tree) -> set[str]:
    """該模組自己的 import + 模組層級 def/class 名（class-body 常數檢查的合法來源之一）。"""
    names = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                names.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
    return names


def _class_body_forward_refs(tree, module_bound: set[str]) -> list[str]:
    """(b) class-body 層級（不在任何 method 內）引用同模組 class-body 裡沒先定義的名字。

    只查 Assign/AnnAssign 的 RHS 直接 Name（涵蓋目前已知的地雷形狀：
    `_X = _DJ_TEMPLATES.get(...)` 這種衍生常數宣告），不下鑽巢狀 FunctionDef/Lambda
    （那些有自己的延遲解析語意，不是這個檢查要抓的形狀）。
    """
    builtin_names = set(dir(builtins))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bound_so_far: set[str] = set()
        for stmt in node.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                rhs = stmt.value
                if rhs is not None:
                    for sub in ast.walk(rhs):
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                            break  # 巢狀函式/lambda 有自己的延遲解析語意，不查
                        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                            name = sub.id
                            if (name not in bound_so_far and name not in builtin_names
                                    and name not in module_bound):
                                hits.append(f"{name} @ line {sub.lineno}")
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for t in targets:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                            bound_so_far.add(sub.id)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound_so_far.add(stmt.name)
    return hits


MODULES = sorted(glob.glob(os.path.join(ROOT, "cogs", "music_cog_*.py")))


@pytest.mark.parametrize("path", MODULES, ids=lambda p: os.path.basename(p))
def test_extracted_module_has_no_missing_import(path):
    mc_imports = _mc_imported_names()
    bound, loaded = _bound_and_loaded(ast.parse(open(path).read()))
    builtin_names = set(dir(builtins))
    missing = sorted(
        name for name in loaded
        if name not in bound and name not in builtin_names and name in mc_imports
    )
    assert not missing, (
        f"{os.path.basename(path)} 用到但沒帶過來的 import（抽離漏帶）：{missing}。"
        f"請在該模組補上對應 import（來源見 music_cog.py）。"
    )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: os.path.basename(p))
def test_extracted_module_has_no_bare_composed_class_ref(path):
    tree = ast.parse(open(path).read())
    hits = _bare_composed_class_refs(tree)
    assert not hits, (
        f"{os.path.basename(path)} 裸用 {COMPOSED_CLASS_NAME}（{hits}）——搬出主檔後這會是"
        f"循環 import。該處應改用 self/cls。"
    )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: os.path.basename(p))
def test_extracted_module_has_no_class_body_forward_ref(path):
    tree = ast.parse(open(path).read())
    module_bound = _module_level_bound_names(tree)
    hits = _class_body_forward_refs(tree, module_bound)
    assert not hits, (
        f"{os.path.basename(path)} 的 class-body 層級常數引用了同模組裡沒先定義的名字"
        f"（{hits}）——這類常數靠循序 class-body namespace 解析、不是 MRO，來源常數"
        f"（如 _DJ_TEMPLATES）沒有跟著一起搬過來就會在 import 時炸 NameError。"
    )
