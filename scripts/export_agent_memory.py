"""一次性：把 Claude Code 的 per-project 記憶導出成 repo 內、agent-agnostic 的
docs/AGENT_MEMORY.md，讓換 coding agent 後新 agent 讀得到。跑完即丟。"""
import re, glob, os
from pathlib import Path

MEM = Path.home() / ".claude/projects/-Users-jackhuang-Code-Discord-voice-bot/memory"
OUT = Path("docs/AGENT_MEMORY.md")

TYPE_ORDER = ["user", "feedback", "project", "reference", "other"]
TYPE_TITLE = {
    "user": "👤 User — 使用者是誰、偏好",
    "feedback": "🧭 Feedback — 工作守則 / 修正（含 why）",
    "project": "🗂️ Project — 進行中的工作、目標、決策",
    "reference": "🔖 Reference — 外部資源 / 評估結論",
    "other": "📦 Other",
}


def parse(path: Path):
    text = path.read_text(encoding="utf-8")
    name = path.stem
    desc, typ, body = "", "other", text
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if m:
        fm, body = m.group(1), m.group(2)
        dm = re.search(r"^\s*description:\s*(.+)$", fm, re.MULTILINE)
        if dm:
            desc = dm.group(1).strip()
        nm = re.search(r"^\s*name:\s*(.+)$", fm, re.MULTILINE)
        if nm:
            name = nm.group(1).strip()
        tm = re.search(r"type:\s*(user|feedback|project|reference)", fm)
        if tm:
            typ = tm.group(1)
    body = re.sub(r"\[\[([^\]]+)\]\]", r"`\1`", body).strip()  # [[link]] → `link`
    return {"slug": path.stem, "name": name, "desc": desc, "type": typ, "body": body}


def main():
    files = sorted(f for f in glob.glob(str(MEM / "*.md")) if os.path.basename(f) != "MEMORY.md")
    mems = [parse(Path(f)) for f in files]
    groups = {t: [m for m in mems if m["type"] == t] for t in TYPE_ORDER}

    lines = [
        "# Agent Memory — Marvin Discord Voice Bot",
        "",
        "> **給接手的 coding agent**：這是從 Claude Code 的 per-project 記憶導出的累積知識"
        f"（{len(mems)} 條，截至 2026-06-06）。每條是過去 session 學到、無法從 code/git 直接看出的事實、"
        "修正、決策或踩雷。**讀 code 前先讀這份**，能省下重新踩坑的時間。",
        "> 搭配 `CLAUDE.md`（硬性工作守則）+ `AGENTS.md`（入口）一起看。",
        "> 注意：條目含日期；citation 的 file:line 可能已漂移，引用前先對現有 code 驗證。",
        "",
        "## 目錄",
        "",
    ]
    for t in TYPE_ORDER:
        if not groups[t]:
            continue
        lines.append(f"### {TYPE_TITLE[t]}")
        for m in groups[t]:
            anchor = m["slug"].lower().replace("_", "-")
            lines.append(f"- [{m['slug']}](#{anchor}) — {m['desc']}")
        lines.append("")

    for t in TYPE_ORDER:
        if not groups[t]:
            continue
        lines.append(f"\n---\n\n# {TYPE_TITLE[t]}\n")
        for m in groups[t]:
            lines.append(f"## {m['slug']}")
            if m["desc"]:
                lines.append(f"*{m['desc']}*\n")
            lines.append(m["body"])
            lines.append("")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 導出 {len(mems)} 條 → {OUT}")
    for t in TYPE_ORDER:
        if groups[t]:
            print(f"   {t}: {len(groups[t])}")
    print(f"   檔案大小: {OUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
