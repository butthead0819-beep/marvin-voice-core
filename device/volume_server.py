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

PROFILES_FILE = os.path.expanduser("~/marvin-device/sound_profiles.json")
if not os.path.exists(os.path.dirname(PROFILES_FILE)) and os.path.dirname(PROFILES_FILE):
    PROFILES_FILE = "sound_profiles.json"

DEFAULT_PROFILES = {
    "calibrated": {
        "01. 31Hz": 50, "02. 63Hz": 50, "03. 125Hz": 50, "04. 250Hz": 50, "05. 500Hz": 50,
        "06. 1kHz": 50, "07. 2kHz": 50, "08. 4kHz": 50, "09. 8kHz": 50, "10. 16kHz": 50
    },
    "pop": {
        "01. 31Hz": 62, "02. 63Hz": 60, "03. 125Hz": 56, "04. 250Hz": 52, "05. 500Hz": 50,
        "06. 1kHz": 48, "07. 2kHz": 52, "08. 4kHz": 56, "09. 8kHz": 60, "10. 16kHz": 62
    },
    "podcast": {
        "01. 31Hz": 30, "02. 63Hz": 35, "03. 125Hz": 48, "04. 250Hz": 58, "05. 500Hz": 62,
        "06. 1kHz": 66, "07. 2kHz": 64, "08. 4kHz": 58, "09. 8kHz": 48, "10. 16kHz": 42
    },
    "spatial": {
        "01. 31Hz": 50, "02. 63Hz": 50, "03. 125Hz": 50, "04. 250Hz": 50, "05. 500Hz": 50,
        "06. 1kHz": 50, "07. 2kHz": 50, "08. 4kHz": 50, "09. 8kHz": 50, "10. 16kHz": 50
    }
}

CURRENT_PROFILE = "calibrated"
FIR_DIR = "/etc/marvin-device"

def apply_airplay_fir(profile_name: str) -> bool:
    """將對應 profile 的 FIR WAV 檔案路徑透過 D-Bus 設為作用中，實現無縫切換。"""
    src = os.path.join(FIR_DIR, f"eq_fir_{profile_name}.wav")
    print(f"🎬 [apply_airplay_fir] Target profile: {profile_name}")
    print(f"🎬 [apply_airplay_fir] Src: {src} (exists={os.path.exists(src)})")
    if not os.path.exists(src):
        print(f"⚠️ [apply_airplay_fir] Src file does not exist!")
        return False
    try:
        # 直接使用 D-Bus 動態變更 shairport-sync 的路徑屬性（不需重啟，無縫切換！）
        subprocess.run([
            "dbus-send", "--system", "--print-reply",
            "--dest=org.gnome.ShairportSync",
            "/org/gnome/ShairportSync",
            "org.freedesktop.DBus.Properties.Set",
            "string:org.gnome.ShairportSync",
            "string:ConvolutionImpulseResponseFile",
            f"variant:string:{src}"
        ], capture_output=True, text=True, timeout=5, check=True)
        print(f"✅ [apply_airplay_fir] Dynamic path updated to {src} via D-Bus")
        return True
    except Exception as e:
        print(f"❌ [apply_airplay_fir] Error updating D-Bus property: {e}")
        return False

def load_profiles() -> dict:
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {k: v.copy() for k, v in DEFAULT_PROFILES.items()}

def save_profiles(profiles: dict):
    try:
        if os.path.dirname(PROFILES_FILE):
            os.makedirs(os.path.dirname(PROFILES_FILE), exist_ok=True)
        with open(PROFILES_FILE, "w") as f:
            json.dump(profiles, f, indent=2)
    except Exception:
        pass


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
  button.active { border: 1.5px solid var(--accent) !important; color: var(--accent) !important; font-weight: 700 !important; }
</style></head><body>
<h1><span class="dot" id="dot"></span>馬文控制台</h1>

<div class="card" id="npcard">
  <div class="lbl">🎧 現正播放中</div>
  <img id="npcover" alt="" style="width:100%;aspect-ratio:1/1;border-radius:12px;
       object-fit:cover;background:#0f1117;display:none;margin-bottom:12px">
  <div id="nptitle" style="font-size:18px;font-weight:600;line-height:1.3;
       display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">—</div>
  <div id="npby" style="font-size:13px;color:var(--mut);margin-top:4px"></div>
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
  <div class="lbl" style="margin-top:14px">🔊 聲音風格 (Sound Profile)</div>
  <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:6px" id="prof-btns">
    <button id="btn-calibrated" onclick="setProfile('calibrated')">校正</button>
    <button id="btn-pop" onclick="setProfile('pop')">流行</button>
    <button id="btn-podcast" onclick="setProfile('podcast')">播客</button>
    <button id="btn-spatial" onclick="setProfile('spatial')">空間</button>
  </div>
</div>

<div class="card" style="text-align:center">
  <div class="lbl" style="text-align:left;margin-bottom:8px">🎙️ PTT 語音對話 (Push to Talk)</div>
  <button id="btn-ptt" onclick="togglePTT()" style="background:#1f283a; color:#85b3ff; font-size:18px; font-weight:bold; padding:15px; width:100%; border:1px solid #334466; border-radius:8px; cursor:pointer; transition:all 0.2s">🎙️ 開始對話</button>
</div>

<div class="card">
  <div class="lbl">🏠 在家 / 離家（麥克風＋DigiAMP）</div>
  <div class="grid">
    <button class="danger" onclick="presence('off')">🔇 離家</button>
    <button onclick="presence('on')">🏠 到家</button>
  </div>
  <div id="presence-state" style="font-size:13px;color:var(--mut);text-align:center;margin-top:8px">—</div>
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
       if(j.profile) updateProfileUI(j.profile);
  }catch(e){ $("dot").style.background="#ff6b6b"; }
}
async function nowPlaying(){
  try{
    const r=await fetch(MAC_NOW+"?t="+encodeURIComponent(TOKEN),{cache:"no-store"}); const j=await r.json();
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
async function presence(state){
  try{
    const r=await fetch("/presence?t="+encodeURIComponent(TOKEN),
      {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({state:state})});
    const j=await r.json();
    if(j.ok){ stat(state==="off"?"已離家：麥克風＋DigiAMP 關閉":"已到家：麥克風＋DigiAMP 開啟",true); presenceState(); }
    else stat("切換失敗",false);
  }catch(e){ stat("音量服務連不到",false); }
}
async function presenceState(){
  try{ const r=await fetch("/presence?t="+encodeURIComponent(TOKEN)); const j=await r.json();
       if(j.status) $("presence-state").textContent=j.status;
  }catch(e){}
}
function tick(){ nowPlaying(); refresh(); presenceState(); }
tick();
setInterval(tick, 4000);

function updateProfileUI(name) {
  const btns = ["calibrated", "pop", "podcast", "spatial"];
  btns.forEach(b => {
    const el = $("btn-" + b);
    if(el) {
      if(b === name) el.classList.add("active");
      else el.classList.remove("active");
    }
  });
}

async function setProfile(name){
  try{
    const r=await fetch("/profile?t="+encodeURIComponent(TOKEN),{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:name})
    });
    if(r.ok){ 
      stat("已套用風格：" + name, true);
      updateProfileUI(name);
    }
    else { stat("套用風格失敗", false); }
  }catch(e){ stat("風格服務連不到", false); }
}

let pttRecording = false;
async function togglePTT() {
  const el = $("btn-ptt");
  if (!pttRecording) {
    try {
      const r = await fetch("/ptt?t=" + encodeURIComponent(TOKEN), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({state: "start"})
      });
      if (r.ok) {
        pttRecording = true;
        el.textContent = "🔴 錄音中 (點擊結束)";
        el.style.background = "#ff6b6b";
        el.style.color = "#0e0f13";
        el.style.borderColor = "#ff6b6b";
        stat("🎙️ 錄音中...請對著音箱說話", true);
      } else {
        stat("啟動 PTT 失敗", false);
      }
    } catch(e) {
      stat("連不到音量服務", false);
    }
  } else {
    try {
      const r = await fetch("/ptt?t=" + encodeURIComponent(TOKEN), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({state: "stop"})
      });
      if (r.ok) {
        pttRecording = false;
        el.textContent = "🎙️ 開始對話";
        el.style.background = "#1f283a";
        el.style.color = "#85b3ff";
        el.style.borderColor = "#334466";
        stat("⌛ 傳送語音中...", true);
      } else {
        stat("結束 PTT 失敗", false);
      }
    } catch(e) {
      stat("連不到音量服務", false);
    }
  }
}
</script>
</body></html>"""


def _amixer(*args) -> str:
    return subprocess.run(
        ["amixer", "-c", CARD, *args],
        capture_output=True, text=True, timeout=10).stdout


def get_percent() -> int:
    out = _amixer("sget", CONTROL)
    m = re.search(r"Playback (\d+)\s+\[", out)
    if not m:
        m_pct = re.search(r"\[(\d+)%\]", out)
        return int(m_pct.group(1)) if m_pct else -1
    raw_val = int(m.group(1))
    if raw_val <= 0:
        return 0
    if raw_val <= 107:
        return 1
    pct = int((raw_val - 107) / 100.0 * 99.0 + 1)
    return max(0, min(100, pct))


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
    if p == 0:
        _amixer("sset", CONTROL, "0")
    else:
        # 將 1-100% 線性映射至暫存器 107-207 (對應 -50dB 至 0dB 的線性聽感曲線)
        raw_val = int(107 + ((p - 1) / 99.0) * 100.0)
        raw_val = max(0, min(207, raw_val))
        _amixer("sset", CONTROL, str(raw_val))
    # 持久化（開機還原）；pi 免密 sudo
    subprocess.run(["sudo", "alsactl", "store"], capture_output=True, timeout=10)
    return get_percent()


def set_mute(muted: bool) -> int:
    _amixer("sset", CONTROL, "mute" if muted else "unmute")
    subprocess.run(["sudo", "alsactl", "store"], capture_output=True, timeout=10)
    return get_percent()


def get_eq() -> dict:
    try:
        out = subprocess.run(
            ["amixer", "-D", "equal", "sget", "all"],
            capture_output=True, text=True, timeout=5
        ).stdout
        if not out.strip():
            out = subprocess.run(
                ["amixer", "-D", "equal"],
                capture_output=True, text=True, timeout=5
            ).stdout
    except Exception:
        return {}

    res = {}
    controls = re.split(r"Simple mixer control", out)
    for c in controls:
        m_name = re.search(r"'([^']+)'", c)
        if not m_name:
            continue
        name = m_name.group(1)
        m_val = re.search(r"\[(\d+)%\]", c)
        if m_val:
            res[name] = int(m_val.group(1))
    return res


def set_eq(band: str, val: int) -> bool:
    current = get_eq()
    target_key = None
    # 移除前導數字與點，如 "01. 31Hz" -> "31Hz"，並移除空格
    band_core = re.sub(r"^\d+\.\s*", "", band)
    clean_band = re.sub(r"\s+", "", band_core.lower())
    for k in current.keys():
        # 同樣對實機控制鍵進行清洗，如 "00. 31 Hz" -> "31 Hz"
        k_core = re.sub(r"^\d+\.\s*", "", k)
        clean_k = re.sub(r"\s+", "", k_core.lower())
        if clean_band == clean_k or clean_band in clean_k:
            target_key = k
            break
    if not target_key:
        return False
    val = max(0, min(100, val))
    try:
        subprocess.run(
            ["amixer", "-D", "equal", "sset", target_key, f"{val}%"],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        return False


def get_balance() -> dict:
    out = _amixer("sget", CONTROL)
    left = -1
    right = -1
    m_left = re.search(r"Front Left:.*\[(\d+)%\]", out)
    m_right = re.search(r"Front Right:.*\[(\d+)%\]", out)
    if m_left:
        left = int(m_left.group(1))
    if m_right:
        right = int(m_right.group(1))
    return {"left": left, "right": right}


def set_balance(left: int, right: int) -> dict:
    left = max(0, min(100, left))
    right = max(0, min(100, right))
    _amixer("sset", CONTROL, f"{left}%,{right}%")
    subprocess.run(["sudo", "alsactl", "store"], capture_output=True, timeout=10)
    return get_balance()



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
        global CURRENT_PROFILE
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        # 控制台網頁（免 token，Tailscale 私網；API 呼叫才驗 token）
        if self.command == "GET" and path in ("", "/panel"):
            return self._serve_panel()
        if path not in ("/vol", "/eq", "/balance", "/profile", "/ptt", "/presence"):
            return self._send(404, {"error": "not_found"})
        q = parse_qs(parsed.query)
        if not self._authed(q):
            return self._send(401, {"error": "unauthorized"})

        if path == "/vol":
            # 值來源：網址 ?v= 優先，否則 body（POST）
            val = q.get("v", [None])[0]
            if val is None and self.command == "POST":
                n = int(self.headers.get("Content-Length", 0))
                val = self.rfile.read(n).decode() if n else ""
            val = (val or "").strip()
            if not val:  # 無值＝讀現值
                return self._send(200, {"percent": get_percent(), "temp": get_temp(), "profile": CURRENT_PROFILE})
            try:
                pct = self._apply(val)
            except (ValueError, TypeError):
                return self._send(400, {"error": "bad_value", "got": val})
            return self._send(200, {"ok": True, "percent": pct, "temp": get_temp(), "profile": CURRENT_PROFILE})

        elif path == "/eq":
            if self.command == "POST":
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode() if n else "{}"
                try:
                    data = json.loads(body)
                    applied = {}
                    for band, val in data.items():
                        ok = set_eq(band, int(val))
                        applied[band] = ok
                    
                    # 校正完後自動存入 calibrated Profile 記憶體
                    CURRENT_PROFILE = "calibrated"
                    current_eq = get_eq()
                    if current_eq:
                        profiles = load_profiles()
                        profiles["calibrated"] = current_eq
                        save_profiles(profiles)
                        
                    return self._send(200, {"ok": True, "applied": applied, "eq": current_eq})
                except Exception as e:
                    return self._send(400, {"error": "bad_request", "message": str(e)})
            else:
                return self._send(200, {"eq": get_eq()})

        elif path == "/balance":
            if self.command == "POST":
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode() if n else "{}"
                try:
                    data = json.loads(body)
                    left = data.get("left")
                    right = data.get("right")
                    if left is None or right is None:
                        return self._send(400, {"error": "missing_parameters", "require": ["left", "right"]})
                    bal = set_balance(int(left), int(right))
                    return self._send(200, {"ok": True, "balance": bal})
                except Exception as e:
                    return self._send(400, {"error": "bad_request", "message": str(e)})
            else:
                return self._send(200, get_balance())

        elif path == "/profile":
            if self.command == "POST":
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode() if n else "{}"
                print(f"📡 [VolumeServer] POST /profile body={body}")
                try:
                    data = json.loads(body)
                    name = data.get("name", "").strip().lower()
                    print(f"📡 [VolumeServer] POST /profile requesting name={name}")
                    profiles = load_profiles()
                    if name not in profiles:
                        print(f"⚠️ [VolumeServer] POST /profile unknown name={name}")
                        return self._send(400, {"error": "unknown_profile", "available": list(profiles.keys())})
                    
                    # 套用該風格的所有等化器頻段值
                    for band, val in profiles[name].items():
                        set_eq(band, val)
                        
                    CURRENT_PROFILE = name
                    # 同步更換 AirPlay FIR 並重載 shairport-sync
                    fir_ok = apply_airplay_fir(name)
                    print(f"📡 [VolumeServer] POST /profile success name={name} fir_ok={fir_ok}")
                    return self._send(200, {"ok": True, "profile": name, "eq": get_eq(), "airplay_fir": fir_ok})
                except Exception as e:
                    return self._send(400, {"error": "bad_request", "message": str(e)})
            else:
                profiles = load_profiles()
                return self._send(200, {"profiles": list(profiles.keys()), "current_profiles": profiles, "active": CURRENT_PROFILE})

        elif path == "/presence":
            # 離家/到家一鍵開關：跑 marvin-mic on|off（麥克風 + DigiAMP 一起）。
            # 位置自動化的手動備援。GET=讀狀態。
            if self.command == "POST":
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode() if n else "{}"
                try:
                    state = json.loads(body).get("state", "").strip().lower()
                    if state not in ("on", "off"):
                        return self._send(400, {"error": "bad_state", "want": ["on", "off"]})
                    r = subprocess.run(["/usr/local/bin/marvin-mic", state],
                                       capture_output=True, text=True, timeout=10)
                    print(f"📡 [VolumeServer] POST /presence state={state} rc={r.returncode} out={r.stdout.strip()}")
                    return self._send(200, {"ok": r.returncode == 0, "state": state, "detail": r.stdout.strip()})
                except Exception as e:
                    return self._send(400, {"error": "bad_request", "message": str(e)})
            else:
                r = subprocess.run(["/usr/local/bin/marvin-mic", "status"],
                                   capture_output=True, text=True, timeout=10)
                return self._send(200, {"status": r.stdout.strip()})

        elif path == "/ptt":
            if self.command == "POST":
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode() if n else "{}"
                print(f"📡 [VolumeServer] POST /ptt body={body}")
                try:
                    data = json.loads(body)
                    state = data.get("state", "").strip().lower()
                    
                    if state == "start":
                        # 1. 啟用麥克風輸入
                        subprocess.run(["amixer", "-c", "Array", "sset", "Headset", "cap"], check=True)
                        print("🎤 [PTT] 麥克風已開啟 (Headset cap)")
                        
                        # 2. 通知 Mac 大腦進行音樂壓低 (Ducking)
                        import urllib.request
                        mac_host = MAC_SAY.split("/say")[0]
                        wake_url = f"{mac_host}/wake"
                        if TOKEN:
                            wake_url += f"?t={TOKEN}"
                        print(f"📡 [VolumeServer] 呼叫 Mac wake: {wake_url}")
                        
                        req = urllib.request.Request(
                            wake_url,
                            data=b"{}",
                            headers={"Content-Type": "application/json"},
                            method="POST"
                        )
                        try:
                            with urllib.request.urlopen(req, timeout=2) as response:
                                print(f"📡 [VolumeServer] Mac wake 回應: {response.status}")
                        except Exception as e:
                            print(f"⚠️ [VolumeServer] 呼叫 Mac wake 失敗: {e}")
                            
                        return self._send(200, {"ok": True, "state": "recording"})
                        
                    elif state == "stop":
                        # 1. 關閉麥克風輸入
                        subprocess.run(["amixer", "-c", "Array", "sset", "Headset", "nocap"], check=True)
                        print("🔇 [PTT] 麥克風已關閉 (Headset nocap)")
                        
                        # 2. 通知 Mac 大腦強行斷句 (Flush)
                        import urllib.request
                        mac_host = MAC_SAY.split("/say")[0]
                        flush_url = f"{mac_host}/flush"
                        if TOKEN:
                            flush_url += f"?t={TOKEN}"
                        print(f"📡 [VolumeServer] 呼叫 Mac flush: {flush_url}")
                        
                        req = urllib.request.Request(
                            flush_url,
                            data=b"{}",
                            headers={"Content-Type": "application/json"},
                            method="POST"
                        )
                        try:
                            with urllib.request.urlopen(req, timeout=2) as response:
                                print(f"📡 [VolumeServer] Mac flush 回應: {response.status}")
                        except Exception as e:
                            print(f"⚠️ [VolumeServer] 呼叫 Mac flush 失敗: {e}")
                            
                        return self._send(200, {"ok": True, "state": "idle"})
                    else:
                        return self._send(400, {"error": "invalid_state", "got": state})
                except Exception as e:
                    return self._send(400, {"error": "bad_request", "message": str(e)})

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *a):  # 靜音預設 access log
        pass


if __name__ == "__main__":
    print(f"🔊 [VolumeServer] :{PORT}/vol  card={CARD} control={CONTROL} "
          f"token={'on' if TOKEN else 'off'}  現值={get_percent()}%", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
