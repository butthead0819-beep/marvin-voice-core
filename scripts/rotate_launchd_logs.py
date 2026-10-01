"""
scripts/rotate_launchd_logs.py — launchd 日誌 copytruncate + gzip 輪替。

針對被 launchd 持有 fd 的 log（bot_stdout.log, satellite_stdout.log）：
1. 複製成 <name>.<YYYYMMDD>（同名則 -2, -3）
2. 原檔以 truncate 截斷成 0 bytes（絕不 unlink 或 rename）
3. 壓縮複本為 .gz，刪除未壓縮複本
4. 保留最新 14 份 .gz，刪除多餘舊檔

預設為 dry-run 模式，加上 --apply 才執行。
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TAIPEI_TZ = timezone(timedelta(hours=8))
DEFAULT_LOGS = [
    Path.home() / "Library/Logs/Marvin/bot_stdout.log",
    Path.home() / "Library/Logs/Marvin/satellite_stdout.log",
]
DEFAULT_RETENTION = 14


def rotate_single_log(path: Path, retention: int = DEFAULT_RETENTION, apply: bool = False) -> dict:
    if not path.exists():
        print(f"⏩ [Skip] 檔案不存在: {path}")
        return {"path": str(path), "status": "not_found"}

    size = path.stat().st_size
    if size == 0:
        print(f"⏩ [Skip] 檔案大小為 0 bytes: {path}")
        return {"path": str(path), "status": "empty"}

    today_str = datetime.now(TAIPEI_TZ).strftime("%Y%m%d")
    parent = path.parent
    base_name = path.name

    # 決定複製目標路徑
    candidate = parent / f"{base_name}.{today_str}"
    idx = 2
    while candidate.exists() or (parent / f"{candidate.name}.gz").exists():
        candidate = parent / f"{base_name}.{today_str}-{idx}"
        idx += 1

    gz_target = parent / f"{candidate.name}.gz"

    # 搜尋現存的所有舊 .gz 檔
    existing_gzs = sorted(parent.glob(f"{base_name}.*.gz"))

    if not apply:
        print(f"🔍 [DRY-RUN] 會輪替: {path} ({size} bytes)")
        print(f"   -> 複製至 {candidate} 並壓縮為 {gz_target.name}")
        print(f"   -> 原檔 truncate 成 0 bytes")
        if len(existing_gzs) + 1 > retention:
            to_remove = len(existing_gzs) + 1 - retention
            print(f"   -> 會刪除最舊的 {to_remove} 份 .gz 檔")
        return {"path": str(path), "status": "dry_run", "size": size}

    # 1. 複製
    shutil.copy2(path, candidate)

    # 2. 原檔 truncate 0
    with path.open("r+") as f:
        f.truncate(0)

    # 3. gzip 壓縮並刪除未壓縮複本
    with candidate.open("rb") as f_in:
        with gzip.open(gz_target, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    candidate.unlink()

    # 4. 清理超過 retention 份的舊檔
    all_gzs = sorted(parent.glob(f"{base_name}.*.gz"))
    deleted_count = 0
    if len(all_gzs) > retention:
        for old_gz in all_gzs[:-retention]:
            try:
                old_gz.unlink()
                deleted_count += 1
            except OSError:
                pass

    print(f"✅ [Rotated] {path} -> {gz_target.name} (原檔已截斷為 0，清理了 {deleted_count} 份舊 .gz)")
    return {
        "path": str(path),
        "status": "rotated",
        "gz": str(gz_target),
        "pruned_gz_count": deleted_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="launchd stdout log 輪替工具")
    parser.add_argument(
        "--log",
        action="append",
        dest="logs",
        help="指定要輪替的 log 檔案（可重複指定，預設 bot_stdout.log 與 satellite_stdout.log）",
    )
    parser.add_argument("--retention", type=int, default=DEFAULT_RETENTION, help="保留份數（預設 14）")
    parser.add_argument("--apply", action="store_true", help="確認執行輪替（預設 dry-run）")
    args = parser.parse_args(argv)

    targets = [Path(p) for p in args.logs] if args.logs else DEFAULT_LOGS

    for target in targets:
        rotate_single_log(target, retention=args.retention, apply=args.apply)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
