"""
scripts/prune_presence_log.py — 清理 voice_presence.jsonl 舊資料。

讀取 data/voice_presence.jsonl 與 consent.json，過濾：
- is_bot == True 的行
- 未同意者的行（user_name 不在 consent.consented 或為 False）
- event == "move" 的行
- 非 Marvin 所在的頻道（channel_id 不在 Marvin 加入過的頻道集合中）

預設 dry-run 模式：只印出統計，不修改任何檔案。
只有帶 --apply 旗標時才會執行改寫，且改寫前必然備份為：
  <presence_file>.bak_<YYYYMMDD>
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


def prune_presence(
    presence_path: Path,
    consent_path: Path,
    marvin_user_id: str,
    apply: bool = False,
) -> dict:
    if not presence_path.exists():
        print(f"❌ 找不到進出紀錄檔: {presence_path}")
        return {}

    consented_users = set()
    if consent_path.exists():
        try:
            with consent_path.open("r", encoding="utf-8") as f:
                cdata = json.load(f)
                consented_users = {
                    name for name, status in cdata.get("consented", {}).items() if status is True
                }
        except Exception as e:
            print(f"⚠️ 讀取同意紀錄失敗: {e}")

    marvin_user_id_str = str(marvin_user_id)
    marvin_channels = set()

    # 第一輪：收集 Marvin bot 進過的所有 channel_id
    with presence_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("is_bot") is True and str(rec.get("user_id")) == marvin_user_id_str:
                ch_id = rec.get("channel_id")
                if ch_id is not None:
                    marvin_channels.add(str(ch_id))

    total_lines = 0
    bot_lines = 0
    unconsented_lines = 0
    move_lines = 0
    other_channel_lines = 0
    retained_records = []

    # 第二輪：過濾與統計
    with presence_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                rec = json.loads(line)
            except Exception:
                continue

            if rec.get("is_bot") is True:
                bot_lines += 1
                continue

            user_name = rec.get("user_name", "")
            if user_name not in consented_users:
                unconsented_lines += 1
                continue

            if rec.get("event") == "move":
                move_lines += 1
                continue

            ch_id = str(rec.get("channel_id"))
            if ch_id not in marvin_channels:
                other_channel_lines += 1
                continue

            retained_records.append(line)

    retained_count = len(retained_records)

    print("=" * 50)
    print("📊 voice_presence.jsonl 清理統計報告")
    print("=" * 50)
    print(f"總行數:             {total_lines}")
    print(f"bot 行數:           {bot_lines}")
    print(f"未同意者行數:       {unconsented_lines}")
    print(f"move 行數:          {move_lines}")
    print(f"非 Marvin 頻道行數: {other_channel_lines}")
    print(f"會保留的行數:       {retained_count}")
    print("=" * 50)

    if not apply:
        print("🔍 模式: [DRY-RUN] 未更動任何檔案。若確認要執行請加上 --apply。")
    else:
        date_str = datetime.now().strftime("%Y%m%d")
        bak_path = presence_path.parent / f"{presence_path.name}.bak_{date_str}"
        shutil.copy2(presence_path, bak_path)
        print(f"💾 已建立備份: {bak_path}")

        with presence_path.open("w", encoding="utf-8") as f:
            for r in retained_records:
                f.write(r + "\n")
        print(f"✅ 已改寫檔案: {presence_path} (保留 {retained_count} 行)")

    return {
        "total": total_lines,
        "bot": bot_lines,
        "unconsented": unconsented_lines,
        "move": move_lines,
        "other_channel": other_channel_lines,
        "retained": retained_count,
    }


def main():
    parser = argparse.ArgumentParser(description="清理 voice_presence.jsonl 舊資料")
    parser.add_argument("--presence", default="data/voice_presence.jsonl", help="進出紀錄路徑")
    parser.add_argument("--consent", default="consent.json", help="同意紀錄路徑")
    parser.add_argument("--marvin-user-id", required=True, help="Marvin 的 Discord 帳號 ID")
    parser.add_argument("--apply", action="store_true", help="確認執行改寫並建立備份")

    args = parser.parse_args()
    prune_presence(
        Path(args.presence),
        Path(args.consent),
        marvin_user_id=args.marvin_user_id,
        apply=args.apply,
    )


if __name__ == "__main__":
    main()
