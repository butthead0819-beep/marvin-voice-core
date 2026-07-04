#!/usr/bin/env python3
"""漫畫合集週更派送器（2026-07-04；launchd com.antigravity.marvin.comicbundle 週日 21:00）。

流程：重建自含 bundle（scripts/build_comic_gallery.build_bundle）→ content_key
（漫畫檔名集合 hash，只有新漫畫出現才算新版）→ 對 consent.json consented 成員
逐一 REST 私訊檔案（bot token 直打 HTTP API，與 bot 進程無關）→ state 記
user→content_key（**每人每版只送一次**，重跑安全；無新版整趟 no-op 不擾人）。

隱私邊界：收件人 = consent 名單（漫畫內容是這群人的對話，受眾即當事人）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

STATE_PATH = REPO / "records" / "comic_bundle_state.json"
BUNDLE_PATH = REPO / "marvin_comics_bundle.html"
DM_TEXT = ("📓 本週的《馬文的厭世日記》漫畫合集更新了——"
           "用瀏覽器打開附件就能看，含從未貼出過的遺珠。"
           "看不看隨你，反正宇宙也不在乎。——馬文")


# ── pure ─────────────────────────────────────────────────────────────────────

def content_key(comic_names: list[str]) -> str:
    """漫畫檔名集合的穩定短 hash——新漫畫出現才變（重壓像素不觸發重送）。"""
    return hashlib.sha256("\n".join(sorted(comic_names)).encode()).hexdigest()[:12]


def load_state(path=STATE_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"sent": {}}


def should_send(state: dict, user_id: str, key: str) -> bool:
    return state.get("sent", {}).get(str(user_id)) != key


def mark_sent(state: dict, user_id: str, key: str, path=STATE_PATH) -> None:
    state.setdefault("sent", {})[str(user_id)] = key
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def match_member(members: list[dict], display_name: str):
    """display_name/nick/global_name/username 任一吻合 → member dict。"""
    for m in members:
        u = m.get("user", {})
        names = {m.get("nick"), u.get("global_name"), u.get("username")}
        if display_name in names:
            return m
    return None


# ── REST（bot token 直打，與 bot 進程無關） ───────────────────────────────────

def _token() -> str:
    import os
    tok = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not tok:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                tok = line.split("=", 1)[1].strip()
    if not tok:
        raise RuntimeError("no DISCORD_BOT_TOKEN")
    return tok


def _api(method: str, path: str, *, token: str, **kw):
    import requests
    r = requests.request(method, f"https://discord.com/api/v10{path}",
                         headers={"Authorization": f"Bot {token}"}, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.text else {}


def dm_file(token: str, user_id: str, filepath: Path, text: str) -> None:
    ch = _api("POST", "/users/@me/channels", token=token, json={"recipient_id": str(user_id)})
    with open(filepath, "rb") as f:
        _api("POST", f"/channels/{ch['id']}/messages", token=token,
             data={"payload_json": json.dumps({"content": text})},
             files={"files[0]": (filepath.name, f, "text/html")})


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    from scripts.build_comic_gallery import build_bundle, collect_comics

    comics = collect_comics(REPO / "records")
    if not comics:
        print("[comicbundle] 無漫畫，跳過")
        return 0
    key = content_key([e["path"].name for e in comics])
    state = load_state()

    consent = json.loads((REPO / "consent.json").read_text(encoding="utf-8"))
    names = [n for n, ok in consent.get("consented", {}).items() if ok]

    token = _token()
    guilds = _api("GET", "/users/@me/guilds", token=token)
    members: list[dict] = []
    for g in guilds:
        members += _api("GET", f"/guilds/{g['id']}/members?limit=1000", token=token)

    targets = []
    for name in names:
        m = match_member(members, name)
        if m is None:
            print(f"[comicbundle] ⚠️ 找不到成員 {name}，跳過")
            continue
        uid = m["user"]["id"]
        if should_send(state, uid, key):
            targets.append((name, uid))
        else:
            print(f"[comicbundle] {name} 已收過本版，跳過")

    if not targets:
        print(f"[comicbundle] 本版 {key} 無人待送，no-op")
        return 0

    build_bundle(records_dir=REPO / "records", out_path=BUNDLE_PATH)
    for name, uid in targets:
        try:
            dm_file(token, uid, BUNDLE_PATH, DM_TEXT)
            mark_sent(state, uid, key)
            print(f"[comicbundle] ✅ 已私訊 {name}")
            time.sleep(1.5)   # DM 節流，別像轟炸
        except Exception as e:
            print(f"[comicbundle] ❌ {name} 失敗: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
