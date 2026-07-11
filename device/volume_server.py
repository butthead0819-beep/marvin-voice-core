#!/usr/bin/env python3
"""
volume_server.py — Marvin device 音量控制 HTTP 服務（Pi 常駐，取代實體旋鈕）。

零依賴（stdlib）。Siri 捷徑用 HTTP 就能調 DigiAMP+ 音量，不需 SSH / 旋鈕。
控制 DAC 硬體「Digital」音量（by name=IQaudIODAC，卡號會漂移故不用號碼），
每次調整後 alsactl store 持久化（重開機保持）。

iOS 最簡：直接開網址（Safari / 捷徑「取得 URL 內容」皆可），值與 token 都放網址：
  http://<pi>:8766/vol?v=40&t=<token>     → 設 40%
  http://<pi>:8766/vol?v=mute&t=<token>   → 靜音（unmute 解除）
  http://<pi>:8766/vol?t=<token>          → 讀現值
也支援 header X-Marvin-Token + body（curl 用）：
  POST /vol body="40" / "+5" / "-5" / "mute"

env：MARVIN_VOL_PORT（預設 8766）、MARVIN_VOL_TOKEN、MARVIN_VOL_CARD（預設 IQaudIODAC）、
     MARVIN_VOL_CONTROL（預設 Digital）
"""
import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CARD = os.getenv("MARVIN_VOL_CARD", "IQaudIODAC")
CONTROL = os.getenv("MARVIN_VOL_CONTROL", "Digital")
TOKEN = os.getenv("MARVIN_VOL_TOKEN", "").strip() or None
PORT = int(os.getenv("MARVIN_VOL_PORT", "8766"))
# 控制台網頁：指令送 Mac 大腦 /say（跨網域，已開 CORS）
MAC_SAY = os.getenv("MARVIN_MAC_SAY_URL", "http://100.123.68.86:8790/say")


def panel_html() -> str:
    tok = TOKEN or ""
    return PANEL_TEMPLATE.replace("__TOKEN__", tok).replace("__MAC_SAY__", MAC_SAY)


PANEL_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>馬文控制台</title>
<style>
  :root{ --bg:#0e0f13; --card:#1a1c23; --line:#2a2d38; --fg:#e8eaf0; --mut:#8b90a0;
         --accent:#6c8cff; --danger:#ff6b6b; --ok:#4ec07a; }
  *{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body{ margin:0; background:var(--bg); color:var(--fg);
        font:16px/1.4 -apple-system,"PingFang TC",system-ui,sans-serif;
        padding:16px 14px 40px; max-width:520px; margin:0 auto; }
  h1{ font-size:20px; margin:6px 2px 14px; display:flex; align-items:center; gap:8px; }
  h1 .dot{ width:9px; height:9px; border-radius:50%; background:var(--mut); }
  .card{ background:var(--card); border:1px solid var(--line); border-radius:16px;
         padding:14px; margin-bottom:14px; }
  .lbl{ font-size:13px; color:var(--mut); margin:0 2px 8px; }
  .row{ display:flex; gap:8px; }
  .grid{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  input[type=text]{ flex:1; background:#0f1117; border:1px solid var(--line);
       color:var(--fg); border-radius:12px; padding:14px; font-size:16px; }
  button{ border:none; border-radius:12px; padding:15px 10px; font-size:16px;
       font-weight:600; color:var(--fg); background:#262a35; cursor:pointer; }
  button:active{ transform:scale(.97); }
  button.accent{ background:var(--accent); color:#0b1020; }
  button.danger{ background:#3a1f24; color:var(--danger); }
  button.wide{ width:100%; }
  .vol{ display:flex; align-items:center; gap:12px; }
  .vol > button{ flex:1; }  /* ＋/－ 撐滿兩側、大觸控目標、消除右側死白 */
  .vol .pct{ font-size:34px; font-weight:700; min-width:96px; text-align:center; }
  .volbtns{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:10px; }
  #status{ font-size:13px; color:var(--mut); min-height:18px; margin:2px; text-align:center; }
</style></head><body>
<h1><span class="dot" id="dot"></span>馬文控制台</h1>

<div class="card" id="npcard">
  <div class="lbl">🎧 現正播放中</div>
  <div style="display:flex;gap:12px;align-items:center">
    <img id="npcover" alt="" style="width:64px;height:64px;border-radius:10px;
         object-fit:cover;background:#0f1117;flex-shrink:0;display:none">
    <div style="min-width:0">
      <div id="nptitle" style="font-size:18px;font-weight:600;line-height:1.3;
         display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">—</div>
      <div id="npby" style="font-size:13px;color:var(--mut);margin-top:4px"></div>
    </div>
  </div>
</div>

<div class="card">
  <div class="lbl">🎵 點歌（只打歌名，自動送「放一首…」）</div>
  <div class="row">
    <input type="text" id="song" placeholder="例：告白氣球、七里香" autocapitalize="off" autocomplete="off">
    <button class="accent" onclick="playSong()">點播</button>
  </div>
  <div class="grid" style="margin-top:10px">
    <button onclick="say('下一首')">下一首</button>
    <button onclick="say('暫停播放')">暫停</button>
    <button onclick="say('繼續播放')">繼續</button>
    <button class="danger" onclick="say('停止播放')">停</button>
  </div>
</div>

<div class="card">
  <div class="lbl" style="display:flex;justify-content:space-between;align-items:center">
    <span>音量</span><span id="soctemp" style="font-variant-numeric:tabular-nums">🌡️ --°C</span>
  </div>
  <div class="vol">
    <button onclick="vol('-10')">－</button>
    <div class="pct"><span id="pct">--</span><span style="font-size:18px">%</span></div>
    <button onclick="vol('+10')">＋</button>
  </div>
  <button class="danger wide" onclick="vol('mute')" style="margin-top:10px">靜音</button>
</div>

<div class="card">
  <div class="lbl">💬 說一句話 / 問問題</div>
  <div class="row">
    <input type="text" id="cmd" placeholder="例：現在幾點、講個笑話" autocapitalize="off" autocomplete="off">
    <button onclick="sendCmd()">送出</button>
  </div>
</div>

<div id="status">就緒</div>

<script>
const TOKEN="__TOKEN__", MAC_SAY="__MAC_SAY__", MAC_NOW=MAC_SAY.replace("/say","/now");
const $=id=>document.getElementById(id);
function stat(msg,ok){ $("status").textContent=msg; $("status").style.color=ok?"#4ec07a":"#8b90a0"; }
async function say(text){
  try{
    const r=await fetch(MAC_SAY+"?t="+encodeURIComponent(TOKEN),
      {method:"POST",headers:{"Content-Type":"text/plain"},body:text});
    if(r.ok){ stat("已送出：「"+text+"」",true); }
    else if(r.status===0||r.status>=500){ stat("大腦沒回應（是否已啟動？）",false); }
    else{ stat("送出失敗 "+r.status,false); }
  }catch(e){ stat("連不到大腦（Mac 上 main_satellite 沒跑？）",false); }
}
function sendCmd(){ const v=$("cmd").value.trim(); if(!v)return; say(v); $("cmd").value=""; }
$("cmd").addEventListener("keydown",e=>{ if(e.key==="Enter") sendCmd(); });
function playSong(){ const v=$("song").value.trim(); if(!v)return; say("放一首"+v); $("song").value=""; }
$("song").addEventListener("keydown",e=>{ if(e.key==="Enter") playSong(); });
async function vol(v){
  try{
    const r=await fetch("/vol?v="+encodeURIComponent(v)+"&t="+encodeURIComponent(TOKEN),{method:"POST"});
    const j=await r.json();
    if(j.percent!=null){ $("pct").textContent=j.percent; stat("音量 "+j.percent+"%",true); }
  }catch(e){ stat("音量服務連不到",false); }
}
async function refresh(){
  try{ const r=await fetch("/vol?t="+encodeURIComponent(TOKEN)); const j=await r.json();
       if(j.percent!=null) $("pct").textContent=j.percent; $("dot").style.background="#4ec07a";
       if(j.temp!=null && j.temp>0){
         const t=Math.round(j.temp*10)/10, el=$("soctemp");
         el.textContent="🌡️ "+t+"°C";
         el.style.color = t>=65?"#ff6b6b" : t>=57?"#e0a53a" : "#8b90a0";  // 紅=將觸發停播 黃=偏高 灰=正常
       }
  }catch(e){ $("dot").style.background="#ff6b6b"; }
}
async function nowPlaying(){
  try{
    const r=await fetch(MAC_NOW,{cache:"no-store"}); const j=await r.json();
    const cov=$("npcover");
    if(j.playing){
      $("nptitle").textContent=(j.paused?"暫停中 · ":"")+(j.title||"—");
      $("npby").textContent=j.by?("點播："+j.by):"";
      if(j.cover){ cov.src=j.cover; cov.style.display="block"; } else { cov.style.display="none"; }
    }else{
      $("nptitle").textContent="沒有播放"; $("npby").textContent=""; cov.style.display="none";
    }
  }catch(e){
    $("nptitle").textContent="大腦未啟動"; $("npby").textContent="（Mac 上 main_satellite 沒跑）";
    $("npcover").style.display="none";
  }
}
function tick(){ nowPlaying(); refresh(); }
tick();
setInterval(tick, 4000);
</script>
</body></html>"""


def _amixer(*args) -> str:
    return subprocess.run(
        ["amixer", "-c", CARD, *args],
        capture_output=True, text=True, timeout=10).stdout


def get_percent() -> int:
    out = _amixer("sget", CONTROL)
    m = re.search(r"\[(\d+)%\]", out)
    return int(m.group(1)) if m else -1


def get_temp() -> float:
    """SoC(cpu-thermal) 溫度 °C；讀不到回 -1。"""
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"([\d.]+)", out)
        return float(m.group(1)) if m else -1.0
    except Exception:
        return -1.0


def set_percent(p: int) -> int:
    p = max(0, min(100, p))
    _amixer("sset", CONTROL, f"{p}%")
    # 持久化（開機還原）；pi 免密 sudo
    subprocess.run(["sudo", "alsactl", "store"], capture_output=True, timeout=10)
    return get_percent()


def set_mute(muted: bool) -> int:
    _amixer("sset", CONTROL, "mute" if muted else "unmute")
    subprocess.run(["sudo", "alsactl", "store"], capture_output=True, timeout=10)
    return get_percent()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, q) -> bool:
        # token 可走 header 或網址 ?t=（iOS 開網址方便）
        tok = self.headers.get("X-Marvin-Token") or (q.get("t", [None])[0])
        return not TOKEN or tok == TOKEN

    def _apply(self, cmd: str) -> int:
        """cmd: '40' 絕對 / '+5' '-5' 相對 / 'mute' 'unmute'。回新的 %。"""
        cmd = cmd.strip().lower()
        if cmd in ("mute", "unmute"):
            return set_mute(cmd == "mute")
        if cmd.startswith(("+", "-")):
            return set_percent(get_percent() + int(cmd))
        return set_percent(int(re.sub(r"[^\d]", "", cmd)))

    def _serve_panel(self):
        body = panel_html().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        # 控制台網頁（免 token，Tailscale 私網；API 呼叫才驗 token）
        if self.command == "GET" and path in ("", "/panel"):
            return self._serve_panel()
        if path != "/vol":
            return self._send(404, {"error": "not_found"})
        q = parse_qs(parsed.query)
        if not self._authed(q):
            return self._send(401, {"error": "unauthorized"})
        # 值來源：網址 ?v= 優先，否則 body（POST）
        val = q.get("v", [None])[0]
        if val is None and self.command == "POST":
            n = int(self.headers.get("Content-Length", 0))
            val = self.rfile.read(n).decode() if n else ""
        val = (val or "").strip()
        if not val:  # 無值＝讀現值
            return self._send(200, {"percent": get_percent(), "temp": get_temp()})
        try:
            pct = self._apply(val)
        except (ValueError, TypeError):
            return self._send(400, {"error": "bad_value", "got": val})
        self._send(200, {"ok": True, "percent": pct, "temp": get_temp()})

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *a):  # 靜音預設 access log
        pass


if __name__ == "__main__":
    print(f"🔊 [VolumeServer] :{PORT}/vol  card={CARD} control={CONTROL} "
          f"token={'on' if TOKEN else 'off'}  現值={get_percent()}%", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
