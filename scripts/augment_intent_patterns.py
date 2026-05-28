"""LLM 擴 regex pattern 工具 — on-demand 開發者工具，非 daily ritual。

跑法：
  # 正式模式：呼叫 LLM，輸出 markdown 給人工 review
  python scripts/augment_intent_patterns.py

  # Dry run：只列要處理的 schema，不打 LLM，驗證 discovery 正確
  python scripts/augment_intent_patterns.py --dry-run

設計：
- 掃 intent_agents/*_agent.py，找 DeclarativeIntentAgent 子類
- 動態實例化（MagicMock controller）→ 抓真實 schema（f-string resolved）
- 每個 schema 丟 TieredLLMRouter.quick(json=True)，拿 paraphrases + suggested_regex
- 輸出 records/intent_augment_suggestions_<YYYY-MM-DD>.md，**不自動改 agent 程式碼**
- LLM 失敗的 schema 跳過（不污染 markdown），warning log 出來人工看

職責邊界（為什麼跟 intent_augmentation.py 分開）：
- intent_augmentation.py 純函式（無 IO、無 LLM、無 importlib）
- 這個 script 負責：importlib discovery、TieredLLMRouter 接線、CLI、檔案寫入
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import logging
import pkgutil
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

# Project root on path so we can import intent_agents.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intent_agents.base import DeclarativeIntentAgent  # noqa: E402
from intent_agents.intent_augmentation import (  # noqa: E402
    AugmentSuggestion,
    SchemaInfo,
    extract_schemas_from_class,
    format_report,
    make_augment_prompt,
    parse_augment_response,
)

logger = logging.getLogger("augment_intent_patterns")

OUTPUT_DIR = Path("records")
AGENTS_PACKAGE = "intent_agents"


def discover_agent_classes() -> list[type]:
    """Walk intent_agents/ → find DeclarativeIntentAgent subclasses (not base itself)。

    Skip 規則：
    - DeclarativeIntentAgent 本身（base class）不收
    - 模組 import 失敗 → log warning + 跳過
    - 同一個 class 在多個模組重複 import → set 去重
    """
    classes: dict[str, type] = {}
    pkg = importlib.import_module(AGENTS_PACKAGE)

    for modinfo in pkgutil.iter_modules(pkg.__path__):
        # 不用 name filter（`music_agent_v2` 不以 `_agent` 收尾）；改靠下方 issubclass
        # 過濾。代價：會多 import 一些非 agent 模組，但都是純函式/dataclass，無副作用。
        full_name = f"{AGENTS_PACKAGE}.{modinfo.name}"
        try:
            mod = importlib.import_module(full_name)
        except Exception as exc:
            logger.warning(f"[augment] import {full_name} 失敗，跳過: {exc}")
            continue
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if obj is DeclarativeIntentAgent:
                continue
            if not issubclass(obj, DeclarativeIntentAgent):
                continue
            # 只收宣告於該模組的 class（避免抓到 re-import）
            if obj.__module__ != full_name:
                continue
            classes[f"{obj.__module__}.{name}"] = obj

    return list(classes.values())


def collect_all_schemas() -> list[SchemaInfo]:
    """掃完所有 agent class → 攤平回 SchemaInfo list（不打 LLM）。"""
    all_schemas: list[SchemaInfo] = []
    for cls in discover_agent_classes():
        schemas = extract_schemas_from_class(cls, lambda: MagicMock())
        all_schemas.extend(schemas)
    return all_schemas


async def augment_one(schema: SchemaInfo, classifier) -> AugmentSuggestion | None:
    """Call cheap LLM for one schema → AugmentSuggestion；LLM 失敗 → None。"""
    raw = await classifier(make_augment_prompt(schema))
    parsed = parse_augment_response(raw)
    if parsed is None:
        return None
    return AugmentSuggestion(
        schema=schema,
        paraphrases=parsed.paraphrases,
        suggested_regex=parsed.suggested_regex,
    )


async def augment_all(schemas: list[SchemaInfo], classifier) -> list[AugmentSuggestion]:
    """Sequential（不 parallel 怕 burst TPM）。每個 schema 失敗不影響其他。"""
    out: list[AugmentSuggestion] = []
    for i, s in enumerate(schemas, 1):
        logger.info(f"[augment] {i}/{len(schemas)} {s.agent_name}::{s.intent_name}")
        try:
            sug = await augment_one(s, classifier)
        except Exception as exc:
            logger.warning(f"[augment] {s.agent_name}::{s.intent_name} 炸了: {exc}")
            continue
        if sug is not None:
            out.append(sug)
    return out


def _build_default_classifier():
    """Build a `(prompt) -> str | None` async closure using TieredLLMRouter。

    Lazy-init：避免 import 時就掉 LLM pool key 檢查；dry-run 模式不會碰到這條路徑。
    """
    from llm_pool import build_tiered_router
    router = build_tiered_router()
    if router is None:
        raise RuntimeError("no LLM provider keys configured")

    async def _classify(prompt: str) -> str | None:
        return await router.quick(
            prompt=prompt,
            caller="intent_augment",
            max_tokens=400,
            temperature=0.7,  # 高溫求多樣 paraphrase
            json=True,
        )
    return _classify


async def _amain(args) -> int:
    schemas = collect_all_schemas()
    if not schemas:
        print("no schemas discovered — check intent_agents/ has *_agent.py files", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"# Dry run: {len(schemas)} schemas discovered\n")
        for s in schemas:
            print(f"- {s.agent_name}::{s.intent_name} (conf={s.confidence})")
            for p in s.patterns:
                print(f"    {p}")
        return 0

    classifier = _build_default_classifier()
    suggestions = await augment_all(schemas, classifier)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"intent_augment_suggestions_{date.today().isoformat()}.md"
    out_path.write_text(format_report(suggestions), encoding="utf-8")

    print(f"wrote {len(suggestions)}/{len(schemas)} suggestions → {out_path}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="只列 discovered schemas，不打 LLM")
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
