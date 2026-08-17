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

try:
    from puck_mixer import PuckMixer
except ImportError:
    PuckMixer = None

try:
    from puck_mic_aec import PuckMicAecLoop, make_fifo_writer
except ImportError:
    PuckMicAecLoop = None
    make_fifo_writer = None

try:
    from avrcp_media_player import AvrcpMediaPlayer
except ImportError:
    AvrcpMediaPlayer = None

CARD = os.getenv("MARVIN_VOL_CARD", "IQaudIODAC")
CONTROL = os.getenv("MARVIN_VOL_CONTROL", "Digital")
TOKEN = os.getenv("MARVIN_VOL_TOKEN", "").strip() or None
PORT = int(os.getenv("MARVIN_VOL_PORT", "8766"))


def _parse_connected_macs(bluetoothctl_output: str) -> set:
    """解析 `bluetoothctl devices Connected` 的輸出，每行長這樣
    `Device AA:BB:CC:DD:EE:FF BMW 04900`，抓第二個欄位的 MAC（正規化成大寫）。"""
    macs = set()
    for line in bluetoothctl_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            macs.add(parts[1].upper())
    return macs


def _list_connected_bt_macs(timeout: float = 5.0) -> set:
    """跑 `bluetoothctl devices Connected` 查目前有連線的 BT MAC。抓不到（指令不在/
    逾時）就回傳空集合——當成「沒有連線資訊可用」處理，不是硬錯誤。"""
    try:
        out = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
    except Exception:
        return set()
    return _parse_connected_macs(out)


def pick_bt_mac(candidates: list, connected: set = None):
    """candidates 依優先權排序（第一個優先權最高，實務上＝BMW車機，使用者拍板
    「BMW跟其他喇叭不會同時連線，真的都連著時BMW優先」）。回傳目前有連線、優先權
    最高的候選；查不到任何連線資訊（bluetoothctl 沒回應/都沒連）就照樣回傳優先權
    最高的那個——交給 PuckMixer 既有的 `_write_with_reconnect` 重試邏輯把連線談
    起來，不要因為偵測失敗就整條不開機。candidates 為空回傳 None。"""
    if not candidates:
        return None
    if connected is None:
        connected = _list_connected_bt_macs()
    for mac in candidates:
        if mac.upper() in connected:
            return mac
    return candidates[0]


def _bt_mac_candidates() -> list:
    """依優先權排序的候選 MAC 清單：MARVIN_PUCK_BT_MAC 是主要（通常＝BMW，優先權
    最高），MARVIN_PUCK_BT_MAC_FALLBACK 逗號分隔次要候選（例如 Soundcore，對照
    測試/備援用）。只設 MARVIN_PUCK_BT_MAC 時行為跟改動前的固定 MAC 完全一致。"""
    primary = os.getenv("MARVIN_PUCK_BT_MAC", "").strip()
    fallback = [m.strip() for m in os.getenv("MARVIN_PUCK_BT_MAC_FALLBACK", "").split(",") if m.strip()]
    return ([primary] if primary else []) + fallback


PUCK_BT_MAC = pick_bt_mac(_bt_mac_candidates())
# BMW 30s 規律斷線疑似跟裸串流沒回應 AVRCP metadata 查詢有關（見
# device/avrcp_media_player.py 開頭說明，2026-08-17 Soundcore 對照測試沒斷）。
# 出狀況想快速排除變因就設這個 env=0，不用重新部署程式碼。
AVRCP_ENABLE = os.getenv("MARVIN_AVRCP_ENABLE", "1").strip() != "0"
_avrcp_player = AvrcpMediaPlayer() if (AvrcpMediaPlayer and PUCK_BT_MAC and AVRCP_ENABLE) else None
_puck_mixer = (
    PuckMixer(PUCK_BT_MAC, on_track_change=(_avrcp_player.set_track if _avrcp_player else None))
    if (PuckMixer and PUCK_BT_MAC) else None
)
# INMP441 收音 + 即時 AEC（PoC，見 device/puck_mic_aec.py）。
# 只有兩個 env 都設，且真的跑在有 puck_mixer 的車 puck 上才啟動。
PUCK_MIC_DEVICE = os.getenv("MARVIN_PUCK_MIC_DEVICE", "").strip() or None
PUCK_MIC_AEC_OUT = os.getenv("MARVIN_PUCK_MIC_AEC_OUT", "").strip() or "/tmp/marvin_puck_mic_clean.pcm"
_puck_mic_aec_loop = None
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
<title>車 Puck 控制台</title>
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
  .grid3{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
  #status{ font-size:13px; color:var(--mut); min-height:18px; margin:2px; text-align:center; }

  /* ── 黑膠「現正播放」（搬自 HUD mountVinyl，見 main_satellite.py HUD_HTML）── */
  .vinyl-card{ position:relative; aspect-ratio:1/1; overflow:hidden; padding:0; }
  .vinyl-card .vwrap{ position:absolute; top:50%; left:50%; width:82%; aspect-ratio:1/1; transform:translate(-50%,-50%); }
  .vinyl-card .vdisc{ position:absolute; inset:0; width:100%; height:100%; border-radius:50%;
       animation:vspin 12s linear infinite; will-change:transform; }
  @keyframes vspin{ to{ transform:rotate(360deg); } }
  @media (prefers-reduced-motion:reduce){ .vinyl-card .vdisc{ animation:none; } }
  .vinyl-card .vmeta{ position:absolute; left:14px; right:14px; bottom:12px; z-index:2;
       text-shadow:0 2px 10px rgba(0,0,0,.7); }
  .vinyl-card #nptitle{ font-size:17px; font-weight:700; color:#fff; white-space:nowrap;
       overflow:hidden; text-overflow:ellipsis; }
  .vinyl-card #npby{ font-size:12px; color:rgba(255,255,255,.72); margin-top:2px; }

  /* ── Marvin 頭（搬自 HUD mountHead，簡化成只有 idle 待機態，無疊加表演）── */
  .head-card{ aspect-ratio:1/1; overflow:hidden; padding:0; position:relative;
       background:radial-gradient(120% 100% at 50% -10%,#1b2230 0%,#0e0f13 70%); }
  .head-card canvas{ width:100%; height:100%; display:block; }
</style></head><body>
<h1><span class="dot" id="dot"></span>車 Puck 控制台</h1>

<div class="card vinyl-card" id="npcard">
  <div class="vwrap"><canvas class="vdisc"></canvas></div>
  <div class="vmeta">
    <div id="nptitle">—</div>
    <div id="npby"></div>
  </div>
</div>

<div class="card head-card">
  <canvas id="mvhead"></canvas>
</div>

<div class="card">
  <div class="lbl">🎛️ 控制項</div>
  <div class="grid3">
    <button class="accent" onclick="say('繼續播放')">▶️ 播放</button>
    <button class="danger" onclick="say('停止播放')">⏹ 停止</button>
    <button onclick="say('下一首')">⏭ 下一首</button>
  </div>
  <div class="grid" style="margin-top:8px">
    <button onclick="say('小聲一點')">🔉 小聲</button>
    <button onclick="say('大聲一點')">🔊 大聲</button>
  </div>
</div>

<div class="card">
  <div class="lbl">🔍 搜尋＝播放（打歌名，自動送「放一首…」）</div>
  <div class="row">
    <input type="text" id="song" placeholder="例：告白氣球、七里香" autocapitalize="off" autocomplete="off">
    <button class="accent" onclick="playSong()">播放</button>
  </div>
</div>

<div id="status">就緒</div>

<script>
const TOKEN="__TOKEN__", MAC_SAY="__MAC_SAY__",
      MAC_CAR_NOW=MAC_SAY.replace("/say","/car_now");
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
function playSong(){ const v=$("song").value.trim(); if(!v)return; say("放一首"+v); $("song").value=""; }
$("song").addEventListener("keydown",e=>{ if(e.key==="Enter") playSong(); });

// ── 黑膠「現正播放」：搬自 HUD mountVinyl（main_satellite.py 的 HUD_HTML），
// 只吃 {title, pal, cover} 畫封面唱片，跟 HUD 那份是同一套繪圖邏輯 ───────────
const FALLBACK_PAL=['#9BE04B','#4C9DFF','#2A1A44','#080B11'];
function padPal(pal){
  const out=(Array.isArray(pal)?pal:[]).filter(Boolean).slice(0,4);
  while(out.length<4) out.push(FALLBACK_PAL[out.length]);
  return out;
}
function rng(seed){ return ()=>{ seed=(seed*1664525+1013904223)>>>0; return seed/4294967296; }; }
function shade(hex, amt){
  const n=parseInt(String(hex).replace('#',''),16), rr=(n>>16)&255, gg=(n>>8)&255, bb=n&255;
  const mix=c=> amt<0 ? Math.round(c*(1+amt)) : Math.round(c+(255-c)*amt);
  return `rgb(${mix(rr)},${mix(gg)},${mix(bb)})`;
}
function drawLabelArt(ctx,cx,cy,LR,tk){
  const [a,b,c,d]=tk.pal, PI2=Math.PI*2;
  ctx.save(); ctx.beginPath(); ctx.arc(cx,cy,LR,0,PI2); ctx.clip();
  const g=ctx.createLinearGradient(cx-LR,cy-LR,cx+LR,cy+LR); g.addColorStop(0,c); g.addColorStop(1,d);
  ctx.fillStyle=g; ctx.fillRect(cx-LR,cy-LR,LR*2,LR*2);
  [[a,-0.4,-0.3,1.1],[b,0.5,-0.1,1.0],[a,0.2,0.6,0.9]].forEach(([col,px,py,rad])=>{
    const x=cx+LR*px,y=cy+LR*py,R=LR*rad; const bg=ctx.createRadialGradient(x,y,0,x,y,R);
    bg.addColorStop(0,col+'DD'); bg.addColorStop(0.5,col+'55'); bg.addColorStop(1,col+'00');
    ctx.fillStyle=bg; ctx.beginPath(); ctx.arc(x,y,R,0,PI2); ctx.fill(); });
  ctx.globalCompositeOperation='soft-light'; ctx.fillStyle='#fff';
  ctx.globalAlpha=.45;
  for(let i=0;i<9;i++){ ctx.save(); ctx.translate(cx,cy); ctx.rotate(i/9*PI2);
    ctx.beginPath(); ctx.ellipse(0,LR*0.45,LR*0.12,LR*0.4,0,0,PI2); ctx.fill(); ctx.restore(); }
  ctx.globalCompositeOperation='source-over'; ctx.globalAlpha=1;
  ctx.fillStyle='rgba(255,255,255,.96)'; ctx.textAlign='center'; ctx.textBaseline='middle';
  ctx.font='700 '+(LR*0.34)+'px Futura,"Avenir Next",sans-serif';
  ctx.shadowColor='rgba(0,0,0,.35)'; ctx.shadowBlur=LR*0.08;
  ctx.fillText(tk.title,cx,cy-LR*0.02);
  ctx.shadowBlur=0; ctx.textAlign='left'; ctx.textBaseline='alphabetic'; ctx.restore();
}
const imgCache=new Map();
function getCoverImage(url, onReady){
  if(!url) return null;
  const cached=imgCache.get(url);
  if(cached==='error') return null;
  if(cached instanceof Image) return (cached.complete && cached.naturalWidth) ? cached : null;
  const img=new Image();
  imgCache.set(url, img);
  img.onload=onReady;
  img.onerror=()=>imgCache.set(url,'error');
  img.src=url;
  return null;
}
let vinyl=null;
function mountVinyl(card, cover){
  if(vinyl){ vinyl.ro.disconnect(); vinyl=null; }
  if(!card||!cover) return;
  const disc=card.querySelector('.vdisc');
  const dctx=disc.getContext('2d'); let DPR=1;
  function drawDisc(){
    const W=disc.width,H=disc.height,S=Math.min(W,H),cx=W/2,cy=H/2,Rdisc=S*0.49,LR=S*0.205,PI2=Math.PI*2;
    const pal=cover.pal, r=rng(cover.title.length*131+7);
    dctx.clearRect(0,0,W,H);
    const baseCol=pal[0]||'#1b1620';
    const body=dctx.createRadialGradient(cx-Rdisc*0.25,cy-Rdisc*0.3,Rdisc*0.1,cx,cy,Rdisc);
    body.addColorStop(0,shade(baseCol,0.32)); body.addColorStop(0.6,shade(baseCol,-0.15)); body.addColorStop(1,shade(baseCol,-0.55));
    dctx.fillStyle=body; dctx.beginPath(); dctx.arc(cx,cy,Rdisc,0,PI2); dctx.fill();
    dctx.save(); dctx.beginPath(); dctx.arc(cx,cy,Rdisc,0,PI2); dctx.arc(cx,cy,LR*0.98,0,PI2,true); dctx.clip();
    const mixRay=0.55+r()*0.7, mixSplash=0.45+r()*0.9, mixDot=0.45+r()*0.9;
    const rays=Math.floor((90+r()*70)*mixRay);
    for(let i=0;i<rays;i++){
      const ang=r()*PI2;
      const reach=Math.pow(r(),2.4);
      const endR=LR+reach*(Rdisc-LR)*1.02;
      const segs=3+Math.floor(reach*9);
      const col=pal[Math.floor(r()*pal.length)];
      for(let s=0;s<segs;s++){
        const t=s/Math.max(1,segs-1);
        const rr=LR+t*(endR-LR)+(r()-0.5)*S*0.006;
        const ja=ang+(r()-0.5)*0.05;
        const w=S*(0.005*(1-t*0.75)+r()*0.0025);
        dctx.globalAlpha=(0.85-t*0.45)*(0.6+r()*0.4);
        dctx.fillStyle=col;
        dctx.beginPath();
        dctx.arc(cx+Math.cos(ja)*rr, cy+Math.sin(ja)*rr, w, 0, PI2);
        dctx.fill();
      }
    }
    const splashes=Math.floor((8+r()*8)*mixSplash);
    for(let i=0;i<splashes;i++){
      const ang=r()*PI2, reach=Math.pow(r(),1.6), rr=LR+reach*(Rdisc-LR)*0.85;
      const bx=cx+Math.cos(ang)*rr, by=cy+Math.sin(ang)*rr;
      const blobR=S*(0.012+r()*0.022), col=pal[Math.floor(r()*pal.length)];
      const lumps=3+Math.floor(r()*4);
      dctx.fillStyle=col;
      for(let k=0;k<lumps;k++){
        const lx=bx+(r()-0.5)*blobR*1.6, ly=by+(r()-0.5)*blobR*1.6, lr=blobR*(0.4+r()*0.7);
        dctx.globalAlpha=0.55+r()*0.35;
        dctx.beginPath(); dctx.arc(lx,ly,lr,0,PI2); dctx.fill();
      }
    }
    for(let i=0;i<Math.floor(rays*0.25*mixDot);i++){
      const ang=r()*PI2, rr=LR+Math.pow(r(),0.35)*(Rdisc-LR);
      dctx.globalAlpha=0.5+r()*0.4; dctx.fillStyle=pal[Math.floor(r()*pal.length)];
      dctx.beginPath(); dctx.arc(cx+Math.cos(ang)*rr, cy+Math.sin(ang)*rr, S*(0.0015+r()*0.003), 0, PI2); dctx.fill();
    }
    dctx.globalAlpha=1;
    dctx.lineWidth=Math.max(1,DPR*0.6);
    for(let R=LR*1.15; R<Rdisc*0.98; R+=S*0.008){
      dctx.strokeStyle='rgba(255,255,255,0.16)'; dctx.beginPath(); dctx.arc(cx,cy,R,0,PI2); dctx.stroke();
      dctx.strokeStyle='rgba(0,0,0,0.12)'; dctx.beginPath(); dctx.arc(cx,cy,R+DPR*0.7,0,PI2); dctx.stroke();
    }
    dctx.restore();
    const gl=dctx.createRadialGradient(cx-Rdisc*0.4,cy-Rdisc*0.5,0,cx-Rdisc*0.4,cy-Rdisc*0.5,Rdisc*1.1);
    gl.addColorStop(0,'rgba(255,255,255,0.12)'); gl.addColorStop(0.4,'rgba(255,255,255,0)');
    dctx.globalCompositeOperation='screen'; dctx.fillStyle=gl; dctx.beginPath(); dctx.arc(cx,cy,Rdisc,0,PI2); dctx.fill();
    dctx.globalCompositeOperation='source-over';
    dctx.strokeStyle='rgba(255,255,255,0.10)'; dctx.lineWidth=DPR; dctx.beginPath(); dctx.arc(cx,cy,Rdisc,0,PI2); dctx.stroke();
    const coverImg=cover.cover ? getCoverImage(cover.cover, ()=>drawDisc()) : null;
    if(coverImg){
      dctx.save(); dctx.beginPath(); dctx.arc(cx,cy,LR+DPR,0,PI2); dctx.clip();
      const iw=coverImg.naturalWidth, ih=coverImg.naturalHeight, s=Math.max((LR*2)/iw,(LR*2)/ih);
      const dw=iw*s, dh=ih*s;
      dctx.drawImage(coverImg, cx-dw/2, cy-dh/2, dw, dh);
      dctx.restore();
    } else {
      drawLabelArt(dctx,cx,cy,LR,cover);
      dctx.strokeStyle='rgba(0,0,0,.4)'; dctx.lineWidth=DPR*1.5; dctx.beginPath(); dctx.arc(cx,cy,LR,0,PI2); dctx.stroke();
    }
  }
  function size(){ DPR=Math.min(2,window.devicePixelRatio||1);
    const dr=disc.getBoundingClientRect(); disc.width=Math.max(1,dr.width*DPR); disc.height=Math.max(1,dr.height*DPR);
    drawDisc(); }
  const ro=new ResizeObserver(size); ro.observe(card); size();
  vinyl={ro};
}

// ── Marvin 頭：搬自 HUD mountHead，砍掉疊加表演/情緒系統（沒有 speak/wake 這種
// 即時語音事件可餵，車面板只會用到 idle 待機態，那一整套判斷永遠不會走到）──
const MOOD_IDLE={ col:[104,158,58], blink:{min:2,max:6,dur:0.16},
  gaze:t=>[Math.sin(t*0.33)*0.18, Math.sin(t*0.23+1.1)*0.12] };
let head=null;
function mountHead(canvas){
  if(head){ cancelAnimationFrame(head.raf); head.ro.disconnect(); head=null; }
  if(!canvas) return;
  const ctx=canvas.getContext('2d');
  const st={t:0,gphi:0,glam:0,vphi:0,vlam:0,blink:1,blinkT:1.2,blinkStart:-1,sacT:0,sacX:0,sacY:0,ec:[104,158,58].slice()};
  let W=0,Hh=0,DPR=1;
  const reduce=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const FRAME_MS=1000/24; let lastRenderTs=0;
  function size(){ const r=canvas.getBoundingClientRect(); DPR=Math.min(2,window.devicePixelRatio||1);
    W=canvas.width=Math.max(1,r.width*DPR); Hh=canvas.height=Math.max(1,r.height*DPR); }
  const ro=new ResizeObserver(size); ro.observe(canvas); size();
  const P2=Math.PI*2;
  function frame(ts){
    if(!lastRenderTs) lastRenderTs=ts;
    const sinceRender=ts-lastRenderTs;
    if(sinceRender<FRAME_MS){ if(!reduce) head.raf=requestAnimationFrame(frame); return; }
    const renderDt=sinceRender/1000; lastRenderTs=ts;
    st.t+=renderDt*1.8;
    ctx.clearRect(0,0,W,Hh);
    const cfg=MOOD_IDLE, headBase=Hh;
    const cx=W/2+Math.sin(st.t*0.4)*W*0.02;
    const floatY=Math.sin(st.t*0.28)*headBase*0.045;
    const cy=Hh*0.45+floatY;
    const R=headBase*0.40*(1+Math.sin(st.t*0.9)*0.006);
    const floatNorm=(floatY/(headBase*0.045)+1)/2;
    const shadowGap=headBase*0.10+floatNorm*headBase*0.05;
    const shadowScale=1-floatNorm*0.22, shadowAlpha=0.40-floatNorm*0.16;
    ctx.save(); ctx.translate(cx,cy+R+shadowGap); ctx.scale(shadowScale,0.22*shadowScale);
    const cs=ctx.createRadialGradient(0,0,0,0,0,R*0.85);
    cs.addColorStop(0,`rgba(0,0,0,${shadowAlpha})`); cs.addColorStop(0.7,`rgba(0,0,0,${shadowAlpha*0.4})`); cs.addColorStop(1,'rgba(0,0,0,0)');
    ctx.fillStyle=cs; ctx.beginPath(); ctx.arc(0,0,R*0.85,0,P2); ctx.fill();
    const glowAlpha=0.16-floatNorm*0.09;
    const gl=ctx.createRadialGradient(0,0,0,0,0,R*0.75);
    gl.addColorStop(0,`rgba(140,214,90,${glowAlpha})`); gl.addColorStop(1,'rgba(140,214,90,0)');
    ctx.globalCompositeOperation='screen'; ctx.fillStyle=gl; ctx.beginPath(); ctx.arc(0,0,R*0.75,0,P2); ctx.fill();
    ctx.globalCompositeOperation='source-over';
    ctx.restore();
    const sph=ctx.createRadialGradient(cx-R*0.34,cy-R*0.42,R*0.05,cx,cy,R*1.07);
    sph.addColorStop(0,'#ffffff');sph.addColorStop(0.3,'#eef2f4');sph.addColorStop(0.66,'#cfd6dc');sph.addColorStop(0.9,'#b6c0c8');sph.addColorStop(1,'#8b959d');
    ctx.fillStyle=sph;ctx.beginPath();ctx.arc(cx,cy,R,0,P2);ctx.fill();
    ctx.save();ctx.beginPath();ctx.arc(cx,cy,R,0,P2);ctx.clip();
    const hot=ctx.createRadialGradient(cx-R*0.33,cy-R*0.42,0,cx-R*0.33,cy-R*0.42,R*0.5);
    hot.addColorStop(0,'rgba(255,255,255,0.9)');hot.addColorStop(1,'rgba(255,255,255,0)');
    ctx.fillStyle=hot;ctx.fillRect(cx-R,cy-R,2*R,2*R);
    ctx.restore();
    ctx.strokeStyle='rgba(255,255,255,.4)';ctx.lineWidth=DPR;ctx.beginPath();ctx.arc(cx,cy,R,0,P2);ctx.stroke();
    const tc=cfg.col; st.ec=st.ec.map((v,i)=>v+(tc[i]-v)*0.06);
    const gr=st.ec[0], gg=st.ec[1], gb=st.ec[2], alpha=0.98;
    const dark=k=>`rgba(${gr*k|0},${gg*k|0},${gb*k|0},${alpha})`;
    const bright=`rgba(${Math.min(255,gr+80)|0},${Math.min(255,gg+70)|0},${Math.min(255,gb+70)|0},${alpha})`;
    let [tphi,tlam]=cfg.gaze(st.t);
    if(st.t>st.sacT){ st.sacT=st.t+0.4+Math.random()*1.7; st.sacX=(Math.random()-0.5)*0.1; st.sacY=(Math.random()-0.5)*0.06; }
    tphi+=st.sacX; tlam+=st.sacY;
    st.vphi+=(tphi-st.gphi)*0.018-st.vphi*0.14; st.gphi+=st.vphi;
    st.vlam+=(tlam-st.glam)*0.018-st.vlam*0.14; st.glam+=st.vlam;
    const bc=cfg.blink;
    if(st.blinkStart<0&&st.t>st.blinkT){ st.blinkStart=st.t; st.blinkT=st.t+bc.min+Math.random()*(bc.max-bc.min); }
    st.blink=1;
    if(st.blinkStart>=0){ const pr=(st.t-st.blinkStart)/bc.dur; if(pr>=1) st.blinkStart=-1; else st.blink=1-0.92*Math.sin(pr*Math.PI); }
    const phiC=0.72,dw=0.27,lamC=0.15,dhA=0.26, proj=(phi,lam)=>[cx+R*Math.cos(lam)*Math.sin(phi),cy+R*Math.sin(lam)];
    function eye(sign){
      const p0=sign*phiC+st.gphi, lam0=lamC+st.glam;
      const P=[proj(p0+sign*dw,lam0+0.05*st.blink),proj(p0-sign*dw,lam0),proj(p0,lam0+dhA*st.blink)];
      const path=()=>{ctx.beginPath();ctx.moveTo(P[0][0],P[0][1]);ctx.lineTo(P[1][0],P[1][1]);ctx.lineTo(P[2][0],P[2][1]);ctx.closePath();};
      path();ctx.fillStyle=dark(0.45);ctx.fill();
      ctx.save();path();ctx.clip();
      const sx=(P[1][0]+P[2][0])/2+st.gphi*R*0.9, sy=(P[1][1]+P[2][1])/2;
      const g=ctx.createRadialGradient(sx,sy,0,sx,sy,R*0.46);
      g.addColorStop(0,bright);g.addColorStop(0.4,dark(1));g.addColorStop(1,dark(0.42));ctx.fillStyle=g;ctx.fill();
      const topY=Math.min(P[0][1],P[1][1]);
      const sh=ctx.createLinearGradient(0,topY-R*0.01,0,topY+R*0.16);sh.addColorStop(0,'rgba(0,0,0,.5)');sh.addColorStop(1,'rgba(0,0,0,0)');
      ctx.fillStyle=sh;ctx.fill();ctx.restore();
      ctx.lineJoin='round';ctx.lineCap='round';ctx.lineWidth=Math.max(2,R*0.035);ctx.strokeStyle='rgba(8,10,9,.96)';
      ctx.beginPath();ctx.moveTo(P[0][0],P[0][1]);ctx.lineTo(P[2][0],P[2][1]);ctx.lineTo(P[1][0],P[1][1]);ctx.stroke();
    }
    eye(-1); eye(1);
    ctx.save();ctx.strokeStyle='rgba(16,19,17,.92)';ctx.lineWidth=Math.max(1.5,R*0.02);ctx.lineCap='round';ctx.lineJoin='round';
    const phiEnd=phiC+dw+0.26,curve=0.035;ctx.beginPath();
    for(let i=0;i<=24;i++){ const s=-1+i/12, ph=st.gphi+s*phiEnd, lm=lamC+st.glam+curve*s*s, q=proj(ph,lm); i?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); }
    ctx.stroke();ctx.restore();
    if(!reduce) head.raf=requestAnimationFrame(frame);
  }
  head={raf:requestAnimationFrame(frame),ro};
}
mountHead($("mvhead"));

// ── 輪詢 /car_now：puck 實際在放的歌（見 main_satellite.py::handle_car_now）──
let lastNowKey='';
async function carNowPlaying(){
  try{
    const r=await fetch(MAC_CAR_NOW+"?t="+encodeURIComponent(TOKEN),{cache:"no-store"}); const j=await r.json();
    $("dot").style.background = j.playing ? "#4ec07a" : "#8b90a0";
    if(j.playing){
      $("nptitle").textContent=j.title||'—';
      $("npby").textContent=j.by?("點播："+j.by):"";
      const key=j.title+'|'+j.by+'|'+(j.palette||[]).join(',')+'|'+j.cover;
      if(key!==lastNowKey){ lastNowKey=key;
        mountVinyl($("npcard"), {title:j.title||'', pal:padPal(j.palette), cover:j.cover||''});
      }
    }else{
      $("nptitle").textContent="沒有播放"; $("npby").textContent="";
      if(lastNowKey!==''){ lastNowKey=''; mountVinyl($("npcard"), null); }
    }
  }catch(e){
    $("dot").style.background="#ff6b6b";
    $("nptitle").textContent="大腦未啟動"; $("npby").textContent="（Mac 上 main_satellite 沒跑）";
  }
}
carNowPlaying();
setInterval(carNowPlaying, 4000);
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
        if path not in ("/vol", "/eq", "/balance", "/profile", "/ptt", "/presence", "/hud",
                         "/puck/play", "/puck/queue_next", "/puck/crossfade", "/puck/stop", "/puck/status"):
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

        elif path == "/hud":
            # HUD HDMI kiosk 開關：跑 marvin-hud on|off|status（cage+chromium systemd service）。
            # GET=讀狀態。
            if self.command == "POST":
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode() if n else "{}"
                try:
                    state = json.loads(body).get("state", "").strip().lower()
                    if state not in ("on", "off"):
                        return self._send(400, {"error": "bad_state", "want": ["on", "off"]})
                    r = subprocess.run(["/usr/local/bin/marvin-hud", state],
                                       capture_output=True, text=True, timeout=10)
                    print(f"📡 [VolumeServer] POST /hud state={state} rc={r.returncode} out={r.stdout.strip()}")
                    return self._send(200, {"ok": r.returncode == 0, "state": state, "detail": r.stdout.strip()})
                except Exception as e:
                    return self._send(400, {"error": "bad_request", "message": str(e)})
            else:
                r = subprocess.run(["/usr/local/bin/marvin-hud", "status"],
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

        elif path.startswith("/puck/"):
            # 車puck mk2 BT crossfade 混音：Mac 決策時機、Pi 純執行。
            if _puck_mixer is None:
                return self._send(503, {"error": "puck_mixer_unavailable"})
            if path == "/puck/status":
                return self._send(200, _puck_mixer.status())
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode() if n else "{}"
            try:
                data = json.loads(body) if body else {}
            except Exception:
                return self._send(400, {"error": "bad_json"})
            try:
                if path == "/puck/play":
                    url = data.get("url", "").strip()
                    if not url:
                        return self._send(400, {"error": "missing_url"})
                    _puck_mixer.play(url, title=(data.get("title") or "").strip() or None)
                    return self._send(200, {"ok": True})
                elif path == "/puck/queue_next":
                    url = data.get("url", "").strip()
                    if not url:
                        return self._send(400, {"error": "missing_url"})
                    _puck_mixer.queue_next(url, title=(data.get("title") or "").strip() or None)
                    return self._send(200, {"ok": True})
                elif path == "/puck/crossfade":
                    duration_s = float(data.get("duration_s", 4.0))
                    _puck_mixer.crossfade(duration_s)
                    return self._send(200, {"ok": True})
                elif path == "/puck/stop":
                    _puck_mixer.stop()
                    return self._send(200, {"ok": True})
                else:
                    return self._send(404, {"error": "not_found"})
            except Exception as e:
                return self._send(400, {"error": "bad_request", "message": str(e)})

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *a):  # 靜音預設 access log
        pass


if __name__ == "__main__":
    print(f"🔊 [VolumeServer] :{PORT}/vol  card={CARD} control={CONTROL} "
          f"token={'on' if TOKEN else 'off'}  現值={get_percent()}%", flush=True)
    if _puck_mixer and PUCK_MIC_DEVICE and PuckMicAecLoop:
        _puck_mic_aec_loop = PuckMicAecLoop(
            mixer=_puck_mixer,
            mic_device=PUCK_MIC_DEVICE,
            on_clean_chunk=make_fifo_writer(PUCK_MIC_AEC_OUT),
        )
        _puck_mic_aec_loop.start()
        print(f"🎙️ [PuckMicAec] 收音 device={PUCK_MIC_DEVICE} → 消回音後輸出 {PUCK_MIC_AEC_OUT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
