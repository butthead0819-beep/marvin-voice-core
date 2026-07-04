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

def content_key(comic_names: list[str], fmt: str = "") -> str:
    """漫畫檔名集合的穩定短 hash——新漫畫出現才變（重壓像素不觸發重送）。

    fmt：派送格式版本（v2 起 "img1"）——格式升級時 key 跟著變，
    讓收過舊格式（HTML 附件在 Discord 不能直接開）的人重收一次新格式。
    """
    payload = fmt + "|" + "\n".join(sorted(comic_names))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def chunk(items: list, n: int) -> list[list]:
    """Discord 每則訊息最多 10 個附件 → 分批。"""
    return [items[i:i + n] for i in range(0, len(items), n)]


def compute_delta(state: dict, current_names: list[str]) -> list[str]:
    """週更只送新增的漫畫（首次=全送）。與 per-user key 去重互補：
    delta 管「送什麼」，key 管「要不要送」。"""
    seen = set(state.get("last_names", []))
    return [n for n in current_names if n not in seen]


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


def jpeg_bytes(png_path: Path, max_width: int = 1080, q: int = 85) -> bytes:
    """PNG → 縮寬 JPEG bytes（Discord 原生預覽用；單張 ~200-500KB）。"""
    import io

    from PIL import Image
    img = Image.open(png_path).convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=q, optimize=True)
    return buf.getvalue()


def dm_images(token: str, user_id: str, images: list[tuple[str, bytes]], text: str) -> None:
    """私訊圖片（Discord inline 預覽、手機直接滑）。>10 張自動分批，文字只跟第一批。"""
    ch = _api("POST", "/users/@me/channels", token=token, json={"recipient_id": str(user_id)})
    for i, batch in enumerate(chunk(images, 10)):
        files = {f"files[{j}]": (name, data, "image/jpeg")
                 for j, (name, data) in enumerate(batch)}
        _api("POST", f"/channels/{ch['id']}/messages", token=token,
             data={"payload_json": json.dumps({"content": text if i == 0 else ""})},
             files=files)
        time.sleep(1.0)


# ── main ─────────────────────────────────────────────────────────────────────

FMT = "img1"   # v2（2026-07-04）：HTML 附件在 Discord 只能下載 → 改送原生可預覽圖片
DM_TEXT_IMG = ("📓 《馬文的厭世日記》漫畫合集更新——直接點圖就能看，"
               "含從未貼出過的遺珠。看不看隨你，反正宇宙也不在乎。——馬文")


def main() -> int:
    from scripts.build_comic_gallery import build, build_bundle, collect_comics

    comics = collect_comics(REPO / "records")
    if not comics:
        print("[comicbundle] 無漫畫，跳過")
        return 0
    all_names = [e["path"].name for e in comics]
    key = content_key(all_names, fmt=FMT)
    state = load_state()

    # 本機收藏頁順手同步更新（Jack 自用；不進 DM）
    try:
        build(records_dir=REPO / "records", out_path=REPO / "marvin_comics.html")
        build_bundle(records_dir=REPO / "records", out_path=BUNDLE_PATH)
    except Exception as e:
        print(f"[comicbundle] 本機收藏頁更新失敗（不擋派送）: {e}")

    # 送什麼：新增 delta（首次/格式升級 = last_names 空或未涵蓋 → 全送）
    delta_names = compute_delta(state, all_names)
    if not delta_names:
        print("[comicbundle] 無新漫畫，no-op")
        return 0
    by_name = {e["path"].name: e for e in comics}
    images = [(n.replace(".png", ".jpg"), jpeg_bytes(by_name[n]["path"]))
              for n in sorted(delta_names)]
    total_mb = sum(len(b) for _, b in images) / 1e6
    print(f"[comicbundle] 待送 {len(images)} 張（{total_mb:.1f}MB JPEG）")

    consent = json.loads((REPO / "consent.json").read_text(encoding="utf-8"))
    names = [n for n, ok in consent.get("consented", {}).items() if ok]

    token = _token()
    guilds = _api("GET", "/users/@me/guilds", token=token)
    members: list[dict] = []
    for g in guilds:
        members += _api("GET", f"/guilds/{g['id']}/members?limit=1000", token=token)

    sent_any = False
    for name in names:
        m = match_member(members, name)
        if m is None:
            print(f"[comicbundle] ⚠️ 找不到成員 {name}，跳過")
            continue
        uid = m["user"]["id"]
        if not should_send(state, uid, key):
            print(f"[comicbundle] {name} 已收過本版，跳過")
            continue
        try:
            dm_images(token, uid, images, DM_TEXT_IMG)
            mark_sent(state, uid, key)
            sent_any = True
            print(f"[comicbundle] ✅ 已私訊 {name}（{len(images)} 張圖）")
            time.sleep(1.5)   # DM 節流，別像轟炸
        except Exception as e:
            print(f"[comicbundle] ❌ {name} 失敗: {e}")

    if sent_any:
        state["last_names"] = all_names
        Path(STATE_PATH).write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
