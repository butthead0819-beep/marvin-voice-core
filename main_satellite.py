"""
main_satellite.py — 衛星模式 standalone 啟動入口（實體音箱 S4；不登入 Discord）

腦跑在 Mac，麥/喇叭在 Pi（wyoming-satellite）。與 main_local.py 唯一差別＝輸入/輸出
transport 從「Mac 本機 mic/speaker」換成「TCP 連 Pi 衛星」。

Live 執行步驟：
  1. 先在 Pi 起 wyoming-openwakeword + wyoming-satellite（見 docs/device/S3_pi_setup.md）
  2. 從**主 checkout** 跑（非獨立 worktree）＝讀寫**正本記憶**（marvin.db/music_memory.json/
     records/）＋用主 .env 的 GUILD_ID＝跟 Discord 同一個 per-person 記憶分區（同一個靈魂）。
     入口會自動 chdir 到 repo 根目錄，從哪啟動都錨到正本。
     - .env 需有 GUILD_ID（與 Discord 相同，已設）
     - 設 MARVIN_SATELLITE_SPEAKER=狗與露（身分映射→(GUILD_ID, 狗與露) 同分區＝記憶延續）
  3. /Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python main_satellite.py
  4. 對 Pi 麥喊喚醒詞「馬文」，再說話；從 Pi 書架喇叭聽回應

注意事項：
  - 不登入 Discord——不與線上 24/7 bot 的同 token 衝突
  - 🧊 預設啟用 ephemeral 記憶沙盒＝唯讀繼承正本、寫入全 no-op、斷線丟棄
    ∴ **可與 24/7 Discord bot 並存、不必停一啟一**（見 design_ephemeral_sandbox_memory）。
    代價：satellite 講的話/點的歌不進正本（session 內 RAM cache 連貫、進程結束即忘）。
    escape hatch：設 MARVIN_MEMORY_SANDBOX=0 關沙盒直接寫正本，但須先停 Discord bot。
  - 衛星斷線會自動 5s 重連（不炸腦）；驗收天梯見 docs/device/S4_integration.md
  - 按 Ctrl-C 乾淨結束
"""
import asyncio
import logging
import os
import tempfile
import time

from dotenv import load_dotenv

import memory_sandbox

logger = logging.getLogger(__name__)


def maybe_activate_memory_sandbox(env) -> bool:
    """satellite 預設啟用 ephemeral 記憶沙盒＝唯讀繼承正本、寫入 no-op、斷線丟棄。

    這是「satellite/discord 模式共存不搶寫正本」的關鍵：沙盒下 satellite 進程
    絕不寫 marvin.db / music_memory.json / 等正本，∴ 24/7 Discord bot 可同時活著、
    不必停一啟一（見 design_ephemeral_sandbox_memory）。

    env `MARVIN_MEMORY_SANDBOX=0` ＝escape hatch：關沙盒、讓 satellite 直接寫正本
    （舊工作流，需先停掉 Discord bot 避免 lost-update）。回傳是否啟用。
    """
    if env.get("MARVIN_MEMORY_SANDBOX", "1").strip() == "0":
        logger.warning(
            "⚠️ [Satellite] 記憶沙盒關閉（MARVIN_MEMORY_SANDBOX=0）＝直接寫正本；"
            "務必先停掉 24/7 Discord bot，否則並行寫會 lost-update")
        return False
    memory_sandbox.activate()
    logger.info(
        "🧊 [Satellite] ephemeral 記憶沙盒啟用：唯讀繼承正本、寫入全 no-op、斷線丟棄"
        "（可與 24/7 Discord bot 並存不搶寫）")
    return True


def repo_root() -> str:
    """含 main_satellite.py 的 repo 根目錄＝正本記憶/assets/models/.env 所在。"""
    return os.path.dirname(os.path.abspath(__file__))


def check_identity_alignment(env) -> list:
    """回傳記憶對齊警告清單（空＝對齊 OK）。純函式，好測。

    device 是「同一個靈魂的另一具身體」：per-person 記憶按 (GUILD_ID, speaker) 分區，
    兩者都要跟 Discord 一致，才讀得到同一份人格記憶。
    """
    warnings = []
    gid = env.get("GUILD_ID")
    if not gid or gid == "0":
        warnings.append(
            "GUILD_ID 未設或=0 → per-person 記憶會落在分區 0、讀不到 Discord 的人格記憶；"
            "請在 .env 設與 Discord 相同的 GUILD_ID"
        )
    if not env.get("MARVIN_SATELLITE_SPEAKER"):
        warnings.append(
            "MARVIN_SATELLITE_SPEAKER 未設 → 衛星講者不映射到既有身分、記憶不延續；"
            "建議設為 OWNER_SPEAKER（如 狗與露）"
        )
    return warnings


def build_local_bot():
    """構建 MarvinBot 腦（不登入 Discord）。VISION_ENABLED 強制 false，避免螢幕擷取依賴。"""
    os.environ["VISION_ENABLED"] = "false"
    from main_discord import MarvinBot
    return MarvinBot()


async def setup_satellite(bot) -> object:
    """載入必要 cog 並啟動衛星聆聽（可測試的 wiring 層）。

    順序對齊 setup_hook：music_cog 必須先於 voice_controller。
    """
    await bot.load_extension("cogs.music_cog")
    await bot.load_extension("cogs.voice_controller")
    bot.engine.start()
    vc = bot.cogs.get("VoiceController")
    if vc is None:
        raise RuntimeError("VoiceController cog 未載入，無法啟動衛星聆聽")
    vc.start_satellite_listening()
    return vc


async def setup_browser_satellite(bot):
    """純軟體 satellite wiring：載 cog + 綁輸出（不連 Pi）。

    一般模式 → BrowserSpeakerOutput（靜音切段快取，GET /reply 給瀏覽器）。
    MARVIN_CAR_MODE=1 → 改用 StreamSpeakerOutput（逐 frame 即時轉送，GET /audio_stream
    給 ESP32 puck；不緩衝整段，音樂/歌單長度不受 PSRAM 限制）+ persistent=True（比照
    Pi 常駐喇叭連續泵，見 [[project_marvin_physical_speaker]]/mk2）。

    回 (vc, browser_out, stream_out)：非車載模式 stream_out=None；車載模式 browser_out=None
    （/reply 停用，全走 /audio_stream）。
    """
    await bot.load_extension("cogs.music_cog")
    await bot.load_extension("cogs.voice_controller")
    bot.engine.start()
    vc = bot.cogs.get("VoiceController")
    if vc is None:
        raise RuntimeError("VoiceController cog 未載入，無法啟動純軟體 satellite")

    if os.getenv("MARVIN_CAR_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
        from marvin_voice_core.stream_speaker_output import StreamSpeakerOutput
        stream_out = StreamSpeakerOutput(bot.loop)
        vc.start_browser_satellite_listening(stream_out, persistent=True)
        return vc, None, stream_out

    from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput
    browser_out = BrowserSpeakerOutput()
    vc.start_browser_satellite_listening(browser_out)
    return vc, browser_out, None


async def inject_text(vc, speaker: str, text: str) -> bool:
    """把一段文字當成「已轉錄結果」注入 Marvin pipeline（stdin / HTTP 共用）。

    跳過 STT、虛擬空 wav_bytes、bypass_etd（文字輸入無語音、不需語意終止檢測）。
    回傳 True＝已送出、False＝空字串略過。
    """
    text = (text or "").strip()
    if not text:
        return False
    logger.info(f"📝 [TextInput] 收到文字（{speaker}）: {text}")
    # 用牆鐘 time.time()：下游 Stale Drop 檢查是 time.time()-timestamp，
    # 傳單調時鐘（loop.time()）會被誤判成排隊上億秒而丟棄。
    timestamp = time.time()
    await vc.handle_stt_result(
        speaker=speaker,
        raw_text=text,
        timestamp=timestamp,
        wav_bytes=b"",  # 文字模式無音訊
        prosody_data=None,
        is_wake_check=False,
        bypass_etd=True,  # 文字輸入跳過語意終止檢測
        is_text_input=True,  # 跳過 Echo Guard（播音樂時仍能下文字指令）+ 不等後續語音
    )
    return True


# 純軟體 iOS satellite 網頁（Mac :8790 自服務；Pi 完全不參與）。
# 瀏覽器用 WebAudio 擷取 PCM 自行編 WAV（跨 iOS Safari 穩、免伺服器 ffmpeg），
# 一次 POST 整句 → Mac STT → pipeline。__TOKEN__ 由伺服器填入。
SATELLITE_HTML = """<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>馬文 Satellite</title>
<style>
  :root{ --bg:#0e0f13; --card:#1a1c23; --line:#2a2d38; --fg:#e8eaf0; --mut:#8b90a0;
         --accent:#6c8cff; --danger:#ff6b6b; --ok:#4ec07a; }
  *{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body{ margin:0; background:var(--bg); color:var(--fg);
        font:16px/1.4 -apple-system,"PingFang TC",system-ui,sans-serif;
        padding:16px 14px 40px; max-width:520px; margin:0 auto; }
  h1{ font-size:20px; margin:6px 2px 14px; display:flex; align-items:center; gap:8px; }
  .card{ background:var(--card); border:1px solid var(--line); border-radius:16px;
         padding:18px 16px; margin-bottom:14px; }
  .lbl{ font-size:13px; color:var(--mut); margin:0 2px 10px; }
  #ptt{ width:100%; border:none; border-radius:16px; padding:34px 10px; font-size:22px;
        font-weight:700; color:#0b1020; background:var(--accent); cursor:pointer;
        transition:transform .12s, background .2s; }
  #ptt:active{ transform:scale(.98); }
  #ptt.rec{ background:var(--danger); color:#0e0f13; }
  #you{ font-size:17px; font-weight:600; line-height:1.4; min-height:24px; }
  #status{ font-size:13px; color:var(--mut); min-height:18px; margin:10px 2px 0; text-align:center; }
</style></head><body>
<h1>🛰️ 馬文 Satellite</h1>

<div class="card">
  <button id="ptt">🎙️ 按住講話</button>
</div>

<div class="card">
  <div class="lbl">📝 狀態</div>
  <div id="you">—</div>
</div>

<div class="card">
  <div class="lbl">🔊 馬文</div>
  <div id="marvin" style="font-size:15px;color:var(--mut)">—</div>
</div>

<audio id="player" playsinline></audio>
<div id="status">就緒（本機瀏覽器收音，不經 Pi）</div>

<script>
const TOKEN="__TOKEN__";
// 無聲 clip：在 PTT 手勢內播一次以「解鎖」<audio> 元素（走 media 類別，不受 iOS 靜音鍵影響）。
const SILENT="data:audio/wav;base64,UklGRsQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const $=id=>document.getElementById(id);
function stat(m,ok){ $("status").textContent=m; $("status").style.color=ok?"#4ec07a":"#8b90a0"; }

// 馬文回覆：輪詢 /reply，新段（seq 遞增）就播。用 <audio>（media 類別，靜音鍵不消音），
// 於 PTT 手勢內先播無聲 clip 解鎖，之後從輪詢回呼 play() 才不被 iOS 自動播放政策擋。
let replySeq=0, unlocked=false;
const player=$("player");
function unlockPlayer(){
  if(unlocked) return;
  try{
    player.src=SILENT;
    const p=player.play();
    if(p) p.then(()=>{ player.pause(); player.currentTime=0; unlocked=true; })
          .catch(e=>{ stat("音訊解鎖失敗："+e.name, false); });
  }catch(e){}
}
async function pollReply(){
  try{
    const r=await fetch("/reply?t="+encodeURIComponent(TOKEN)+"&since="+replySeq, {cache:"no-store"});
    if(r.status!==200) return;
    replySeq=parseInt(r.headers.get("X-Reply-Seq")||replySeq);
    const blob=await r.blob();
    player.src=URL.createObjectURL(blob);
    const p=player.play();
    if(p) p.catch(e=>{ $("marvin").textContent="播放失敗（"+e.name+"）——檢查手機靜音鍵/音量"; });
    $("marvin").textContent="🔊 播放中…"; $("marvin").style.color="#e8eaf0";
  }catch(e){ $("marvin").textContent="播放失敗（"+e+"）"; }
}
setInterval(pollReply, 1200);

let ctx, stream, node, src, chunks=[], sampleRate=48000, recording=false, busy=false;

async function startRec(){
  if(recording||busy) return;
  unlockPlayer();                       // 手勢中解鎖 audio（iOS 自動播放限制）
  try{
    stream = await navigator.mediaDevices.getUserMedia({audio:{
      echoCancellation:true, noiseSuppression:true, autoGainControl:true }});
  }catch(e){ stat("拿不到麥克風權限", false); return; }
  ctx = new (window.AudioContext||window.webkitAudioContext)();
  sampleRate = ctx.sampleRate;
  src = ctx.createMediaStreamSource(stream);
  node = ctx.createScriptProcessor(4096, 1, 1);   // 廣泛支援（含 iOS Safari）
  chunks = [];
  node.onaudioprocess = e => {
    const d = e.inputBuffer.getChannelData(0);
    chunks.push(new Float32Array(d));
  };
  src.connect(node); node.connect(ctx.destination);
  recording = true;
  $("ptt").classList.add("rec"); $("ptt").textContent="🔴 放開結束";
  stat("錄音中…請說話", true);
}

async function stopRec(){
  if(!recording) return;
  recording=false; busy=true;
  $("ptt").classList.remove("rec"); $("ptt").textContent="⌛ 傳送中…";
  try{ node.disconnect(); src.disconnect(); stream.getTracks().forEach(t=>t.stop()); await ctx.close(); }catch(e){}
  const wav = encodeWAV(chunks, sampleRate);
  chunks=[];
  try{
    const r = await fetch("/audio?t="+encodeURIComponent(TOKEN),
      {method:"POST", headers:{"Content-Type":"audio/wav"}, body:wav});
    const j = await r.json();
    if(j.ok){ $("you").textContent="✓ 已聽到，馬文思考中…"; stat("已送進馬文大腦", true); }
    else if(r.status===401){ stat("token 錯誤", false); }
    else{ $("you").textContent="（沒聽清楚）"; stat("沒聽到有效語音", false); }
  }catch(e){ stat("連不到大腦（Mac 上 main_satellite 沒跑？）", false); }
  busy=false; $("ptt").textContent="🎙️ 按住講話";
}

// 按住＝錄音；放開＝送出（滑鼠 + 觸控都綁）
const b=$("ptt");
b.addEventListener("mousedown", startRec);
b.addEventListener("mouseup", stopRec);
b.addEventListener("mouseleave", ()=>{ if(recording) stopRec(); });
b.addEventListener("touchstart", e=>{ e.preventDefault(); startRec(); }, {passive:false});
b.addEventListener("touchend", e=>{ e.preventDefault(); stopRec(); }, {passive:false});

// Float32 chunks → 16-bit PCM mono WAV bytes
function encodeWAV(buffers, rate){
  let len=0; buffers.forEach(b=>len+=b.length);
  const pcm=new Float32Array(len); let off=0;
  buffers.forEach(b=>{ pcm.set(b,off); off+=b.length; });
  const buf=new ArrayBuffer(44+pcm.length*2), view=new DataView(buf);
  const ws=(o,s)=>{ for(let i=0;i<s.length;i++) view.setUint8(o+i, s.charCodeAt(i)); };
  ws(0,"RIFF"); view.setUint32(4, 36+pcm.length*2, true); ws(8,"WAVE");
  ws(12,"fmt "); view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
  view.setUint32(24,rate,true); view.setUint32(28,rate*2,true); view.setUint16(32,2,true); view.setUint16(34,16,true);
  ws(36,"data"); view.setUint32(40, pcm.length*2, true);
  let p=44; for(let i=0;i<pcm.length;i++){ let s=Math.max(-1,Math.min(1,pcm[i])); view.setInt16(p, s<0?s*0x8000:s*0x7FFF, true); p+=2; }
  return new Blob([view], {type:"audio/wav"});
}
</script>
</body></html>"""


# Marvin HUD v12（設計稿 → 接上真實 /now 現正播放資料）。1920×480 寬屏顯示框架，
# 重要性階梯卡片 + 會動 Marvin 頭 + 旋轉黑膠（封面調色盤 splatter）。
# 場景/通知中心示範資料仍是靜態 demo；「現正播放」卡輪詢 /now，playing=true 時
# 用真實 title/by/palette 蓋掉 demo 黑膠，沒歌在播就維持 demo 樣子。__TOKEN__ 由伺服器填入。
HUD_HTML = """<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Marvin HUD v12 — 寬屏顯示框架</title>
<style>
  :root{
    color-scheme: dark;
    --ink:#080B11; --ink2:#0C1119; --surf:rgba(255,255,255,.04);
    --text:#EEF2F6; --muted:#93A0AE; --dim:#5C6774; --line:rgba(160,180,200,.12);
    --ok:52,224,190; --info:76,157,255; --warn:245,178,62; --urgent:255,107,94; --marvin:155,224,75;
    --display: "Futura","Avenir Next",-apple-system,system-ui,sans-serif;
    --font: "Avenir Next","Avenir",-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
    --mono: ui-monospace,"SF Mono","JetBrains Mono",Menlo,monospace;
  }
  *{ box-sizing:border-box; }
  html,body{ margin:0; }
  body{
    background:radial-gradient(120% 100% at 50% -20%,#111826 0%,var(--ink) 60%,#04060A 100%);
    color:var(--text); font-family:var(--font); min-height:100vh;
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    gap:clamp(18px,3.4vh,36px); padding:clamp(20px,4vh,52px) 18px; overflow-x:hidden;
  }
  .brand{ text-align:center; display:flex; flex-direction:column; gap:8px; align-items:center; }
  .brand h1{ margin:0; font-family:var(--display); font-size:clamp(19px,2.7vw,28px); font-weight:600; letter-spacing:.18em; text-transform:uppercase;
    background:linear-gradient(180deg,#fff,#B7C4D0); -webkit-background-clip:text; background-clip:text; color:transparent; }
  .brand p{ margin:0; font-family:var(--mono); font-size:clamp(10px,1.3vw,12px); color:var(--muted); letter-spacing:.04em; }

  .device{ width:min(1180px,95vw); filter:drop-shadow(0 40px 80px rgba(0,0,0,.6)); }
  .bezel{ background:linear-gradient(180deg,#1b212b,#0b0f16); border:1px solid #2a323d; border-radius:24px; padding:12px; }
  .screen{
    position:relative; width:100%; aspect-ratio:1920/480; border-radius:14px; overflow:hidden;
    background:var(--ink); container-type:size; box-shadow:inset 0 0 0 1px #000, inset 0 0 60px rgba(0,0,0,.7);
    display:flex; flex-direction:column;
  }

  /* ---- stage: <=3 importance-weighted cards ---- */
  .stage{ flex:1; display:flex; gap:2.2cqh; padding:3cqh 3cqh 1.6cqh; min-height:0; }
  .card{
    --c:var(--ok);
    position:relative; min-width:0; border-radius:3cqh; padding:3.2cqh 3.4cqh;
    display:flex; flex-direction:column; justify-content:space-between; overflow:hidden;
    background:radial-gradient(135% 150% at 16% -12%, rgba(var(--c),.22), transparent 60%), var(--surf);
    border:1px solid rgba(var(--c),.30);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 0 42px rgba(var(--c),.07);
    animation:rise .5s cubic-bezier(.2,.7,.2,1) both;
  }
  .card.hero{ box-shadow: inset 0 1px 0 rgba(255,255,255,.06), 0 0 60px rgba(var(--c),.14); border-color:rgba(var(--c),.45); }
  .card .top{ display:flex; align-items:center; gap:1.6cqh; }
  .card .label{ font-family:var(--mono); font-size:2.9cqh; letter-spacing:.12em; text-transform:uppercase; color:rgba(var(--c),.95); }
  .card .dot{ width:1.7cqh; height:1.7cqh; border-radius:50%; background:rgb(var(--c)); box-shadow:0 0 8px rgba(var(--c),.8); margin-left:auto; }
  .card .title{ font-family:var(--display); font-size:8.5cqh; font-weight:600; line-height:1.04; letter-spacing:.005em; text-wrap:balance; }
  .card.hero .title{ font-size:12cqh; }
  .card .sub{ font-size:3.8cqh; color:var(--muted); font-weight:500; margin-top:.6cqh; }
  .card .acts{ display:flex; gap:1.4cqh; margin-top:2cqh; }
  .chip{ font-family:var(--font); font-size:3.2cqh; font-weight:650; padding:1.3cqh 2.6cqh; border-radius:2cqh; cursor:pointer;
    border:1px solid rgba(var(--c),.4); background:rgba(var(--c),.14); color:#fff; transition:.15s; }
  .chip.primary{ background:rgb(var(--c)); color:#0b1204; border-color:transparent; }
  .chip:hover{ filter:brightness(1.12); }
  .card .glyph{ position:absolute; right:2.6cqh; bottom:2.4cqh; width:11cqh; height:11cqh; color:rgba(var(--c),.5); opacity:.5; }
  .card .glyph.face{ opacity:.95; width:13cqh; height:13cqh; }
  .card .glyph svg{ width:100%; height:100%; }
  .card .qlist{ list-style:none; margin:1.4cqh 0 0; padding:0; display:flex; flex-direction:column; gap:1.2cqh; }
  .card .qlist li{ display:flex; align-items:baseline; gap:1.4cqh; min-width:0; }
  .card .qlist .qi{ font-family:var(--mono); font-size:3cqh; color:rgba(var(--c),.85); flex:none; }
  .card .qlist .qt{ font-size:3.6cqh; font-weight:600; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .card .qlist .qb{ margin-left:auto; flex:none; font-size:2.8cqh; color:var(--dim); }
  .card .qlist .qempty{ font-size:3.2cqh; color:var(--dim); }
  .mcard .mrow{ flex:1; display:flex; align-items:center; gap:2.4cqh; min-height:0; }
  .mcard .mvhead{ width:42%; flex:none; aspect-ratio:1/1; height:auto; }
  .mcard .mtext{ min-width:0; }
  .mcard .mtext .title{ font-size:7.5cqh; }
  .mcard .mtext .sub{ font-size:3.6cqh; }
  .card{ transition:flex-grow .45s cubic-bezier(.2,.7,.2,1), transform .3s, box-shadow .3s, border-color .3s; }
  .mcard{ cursor:pointer;
    /* 底色已經是接近黑的頁面，卡片外圍的陰影疊上去會直接隱形——深度感不能靠外陰影，
       要靠「卡片自己的填色」由上往下漸亮到暗，加上底部一圈跟著圓角走的 inset 陰影
       （inset 陰影是畫在卡片自己的填色上，不是疊到頁面背景，才不會被吃掉），
       讓底部像往內凹進去的一層「地板」，撐出立體感。 */
    background:
      linear-gradient(180deg, rgba(255,255,255,.16) 0%, rgba(255,255,255,0) 30%, rgba(0,0,0,0) 55%, rgba(0,0,0,.55) 100%),
      radial-gradient(135% 150% at 16% -12%, rgba(var(--c),.22), transparent 60%), var(--surf);
    box-shadow: inset 0 1.5px 0 rgba(255,255,255,.28),
                inset 0 -3.4cqh 4.2cqh -1.6cqh rgba(0,0,0,.85),
                0 0 42px rgba(var(--marvin),.07);
  }
  .mcard:hover{ border-color:rgba(var(--marvin),.55); }
  .card.explain{ transform:scale(1.015); z-index:4; box-shadow:0 0 64px rgba(var(--c),.26); border-color:rgba(var(--c),.62); }
  .card.explain .title{ font-size:13cqh; }
  .exhint{ position:absolute; left:3cqh; bottom:1.2cqh; font-family:var(--mono); font-size:3cqh; color:rgba(var(--marvin),.85); z-index:5; }
  .vinyl-card{ position:relative; overflow:hidden; }
  .vinyl-card .vwrap{ position:absolute; top:44%; left:41%; height:140%; aspect-ratio:1/1; transform:translate(-50%,-50%); z-index:0; }
  .vinyl-card .vdisc{ position:absolute; inset:0; width:100%; height:100%; border-radius:50%; animation:spin 12s linear infinite; will-change:transform; }
  .vinyl-card::after{ content:""; position:absolute; inset:0; z-index:1; pointer-events:none;
    background:linear-gradient(180deg, transparent 52%, rgba(6,10,16,.78) 100%); }
  .vinyl-card .top, .vinyl-card .vmeta{ position:relative; z-index:3; }
  .vinyl-card .vmeta{ margin-top:auto; font-family:var(--display); font-size:4cqh; font-weight:600; color:#F1F5F8; text-shadow:0 2px 12px rgba(0,0,0,.75); }
  @keyframes spin{ to{ transform:rotate(360deg); } }
  @media (prefers-reduced-motion:reduce){ .vinyl-card .vdisc{ animation:none; } }
  .mcard:focus-visible{ outline:2px solid rgb(var(--marvin)); outline-offset:-2px; }
  .card .done{ margin-top:2cqh; font-size:3.6cqh; font-weight:650; color:rgb(var(--c)); font-family:var(--mono); }
  @keyframes rise{ from{ opacity:0; transform:translateY(2cqh) scale(.985); } to{ opacity:1; transform:none; } }

  /* dock 收起時整個變矮（不只 icon 橫向收），讓 .stage（flex:1）自動吃到多出來的高度；
     展開時 dock 變高、卡片自動讓出空間——flexbox column 天生會算，不用 JS 量高度。 */
  .dock{ height:16.5cqh; display:flex; align-items:center; gap:2cqh; padding:0 3cqh 1.4cqh; border-top:1px solid var(--line);
    overflow:hidden; transition:height .35s cubic-bezier(.2,.7,.2,1); }
  .dock.icons-collapsed{ height:9cqh; }
  .mshort{ position:relative; display:flex; align-items:center; gap:1.6cqh; padding:1.4cqh 2.6cqh 1.4cqh 1.6cqh; border-radius:3cqh;
    background:radial-gradient(120% 160% at 20% 0%, rgba(var(--marvin),.20), transparent 60%), var(--surf);
    border:1px solid rgba(var(--marvin),.34); cursor:pointer;
    transition:border-color .18s, transform .18s, padding .35s, gap .35s; }
  .mshort:hover{ border-color:rgba(var(--marvin),.65); transform:translateY(-0.4cqh); }
  .dock.icons-collapsed .mshort{ padding:0.7cqh 1.8cqh 0.7cqh 1cqh; gap:1cqh; }
  .mface-wrap{ position:relative; display:inline-flex; flex:none; }
  .mface{ width:9.5cqh; height:9.5cqh; flex:none; transition:width .35s, height .35s; }
  .dock.icons-collapsed .mface{ width:6cqh; height:6cqh; }
  /* 資訊量降到最低：平常只有這顆小紅點，有事才提醒，其餘都收起來（點 Marvin 才滑出來）。
     掛在 .mface-wrap 而非 .mface 本身——mface 的 innerHTML 會被 SVG 覆蓋掉，紅點放裡面會被吃掉。 */
  .mdot{ position:absolute; top:-0.3cqh; right:-0.3cqh; width:2.6cqh; height:2.6cqh; border-radius:50%;
    background:rgb(var(--urgent)); box-shadow:0 0 0 2px var(--ink), 0 0 6px rgba(var(--urgent),.7);
    display:none; }
  .mdot.show{ display:block; }
  .mshort .mlabel{ display:flex; flex-direction:column; line-height:1.12; }
  .mshort .mlabel b{ font-size:3.3cqh; font-weight:650; transition:font-size .35s; }
  .mshort .mlabel span{ font-family:var(--mono); font-size:2.4cqh; color:rgba(var(--marvin),.92); letter-spacing:.05em; transition:font-size .35s; }
  .dock.icons-collapsed .mshort .mlabel b{ font-size:2.8cqh; }
  .dock.icons-collapsed .mshort .mlabel span{ font-size:2cqh; }
  .vdiv{ width:1px; align-self:stretch; margin:2.6cqh 0.6cqh; background:var(--line); transition:opacity .3s; }
  .icons{ display:flex; gap:1.5cqh; max-width:100cqh; opacity:1; overflow:hidden;
    transition:max-width .35s cubic-bezier(.2,.7,.2,1), opacity .25s, gap .35s; }
  .icons.collapsed{ max-width:0; gap:0; opacity:0; pointer-events:none; }
  .dock.icons-collapsed .vdiv{ opacity:0; }
  .ibtn{ --ic:150,180,200; position:relative; width:11cqh; height:11cqh; border-radius:2.8cqh;
    background:radial-gradient(120% 150% at 30% 0%, rgba(var(--ic),.34), rgba(var(--ic),.10) 70%), rgba(255,255,255,.05);
    border:1px solid rgba(var(--ic),.55); color:rgb(var(--ic)); display:grid; place-items:center; cursor:pointer; transition:.16s;
    box-shadow:0 0 16px rgba(var(--ic),.28), inset 0 1px 0 rgba(255,255,255,.12); }
  .ibtn:hover{ transform:translateY(-0.5cqh); box-shadow:0 0 24px rgba(var(--ic),.5), inset 0 1px 0 rgba(255,255,255,.15); }
  .ibtn svg{ width:6cqh; height:6cqh; filter:drop-shadow(0 0 5px rgba(var(--ic),.85)); }
  .ibtn .badge{ position:absolute; top:-0.9cqh; right:-0.9cqh; min-width:3.6cqh; height:3.6cqh; padding:0 1cqh;
    border-radius:2cqh; background:rgb(var(--bc)); color:#0a0a0a; font-family:var(--mono); font-size:2.5cqh; font-weight:700;
    display:grid; place-items:center; box-shadow:0 0 0 2px var(--ink); }
  .clock{ margin-left:auto; text-align:right; font-family:var(--mono); }
  .clock b{ font-size:4.6cqh; font-weight:600; font-variant-numeric:tabular-nums; transition:font-size .35s; }
  .clock span{ display:block; font-size:2.5cqh; color:var(--dim); transition:font-size .35s; }
  .dock.icons-collapsed .clock b{ font-size:3.6cqh; }
  .dock.icons-collapsed .clock span{ font-size:2cqh; }

  .nc{ position:absolute; left:0; right:0; top:0; bottom:16.5cqh; z-index:20;
    background:rgba(8,11,17,.74); backdrop-filter:blur(22px) saturate(1.2); -webkit-backdrop-filter:blur(22px) saturate(1.2);
    transform:translateY(calc(100% + 17cqh)); transition:transform .34s cubic-bezier(.2,.7,.2,1); padding:3cqh; display:flex; flex-direction:column; gap:2cqh; }
  .nc.open{ transform:translateY(0); }
  .nc .head{ display:flex; align-items:center; gap:1.6cqh; }
  .nc .head .t{ font-size:5cqh; font-weight:650; }
  .nc .head .t small{ font-family:var(--mono); font-weight:400; color:var(--muted); font-size:2.8cqh; margin-left:1.2cqh; letter-spacing:.06em; }
  .nc .close{ margin-left:auto; width:8cqh; height:8cqh; border-radius:50%; border:1px solid var(--line); background:var(--surf);
    color:var(--muted); font-size:4cqh; cursor:pointer; display:grid; place-items:center; transition:.16s; }
  .nc .close:hover{ color:var(--text); border-color:rgba(160,180,200,.3); }
  .nc .list{ flex:1; display:grid; grid-template-columns:1fr 1fr; grid-auto-rows:min-content; gap:1.6cqh; overflow:auto; align-content:start; }
  .note{ --c:var(--info); display:flex; gap:1.8cqh; padding:2.2cqh 2.4cqh; border-radius:2.6cqh;
    background:radial-gradient(120% 160% at 12% 0%, rgba(var(--c),.14), transparent 62%), rgba(255,255,255,.045);
    border:1px solid rgba(var(--c),.22); animation:rise .4s both; }
  .note .ni{ width:7cqh; height:7cqh; border-radius:2cqh; background:rgba(var(--c),.18); color:rgb(var(--c)); display:grid; place-items:center; flex:none; }
  .note .ni svg{ width:4.2cqh; height:4.2cqh; }
  .note .nb{ min-width:0; display:flex; flex-direction:column; gap:.4cqh; }
  .note .nb .nt{ font-size:3.4cqh; font-weight:600; display:flex; gap:1cqh; align-items:baseline; }
  .note .nb .nt time{ margin-left:auto; font-family:var(--mono); font-size:2.5cqh; color:var(--dim); flex:none; }
  .note .nb .nm{ font-size:3cqh; color:var(--muted); line-height:1.3; }
  .qa{ display:grid; grid-template-columns:repeat(4,1fr); gap:1.6cqh; grid-column:1/-1; }
  .qbtn{ padding:2.4cqh 2cqh; border-radius:2.6cqh; border:1px solid rgba(var(--marvin),.3);
    background:radial-gradient(120% 150% at 20% 0%, rgba(var(--marvin),.14), transparent 62%), rgba(255,255,255,.04);
    color:var(--text); font-size:3.2cqh; font-weight:600; cursor:pointer; text-align:left; transition:.16s; display:flex; flex-direction:column; gap:1cqh; }
  .qbtn:hover{ border-color:rgba(var(--marvin),.6); }
  .qbtn svg{ width:5cqh; height:5cqh; color:rgb(var(--marvin)); }

  .dock2{ display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; justify-content:center; }
  .seg{ display:flex; background:var(--ink2); border:1px solid var(--line); border-radius:12px; padding:4px; gap:4px; }
  .seg button{ font-family:var(--mono); font-size:12px; color:var(--muted); background:transparent; border:0; cursor:pointer; padding:9px 14px; border-radius:9px; transition:.18s; }
  .seg button:hover{ color:var(--text); }
  .seg button[aria-pressed="true"]{ background:rgba(var(--marvin),.9); color:#0c1406; font-weight:600; }
  .ghost{ font-family:var(--mono); font-size:12px; color:var(--muted); background:transparent; border:1px solid var(--line); border-radius:11px; padding:10px 15px; cursor:pointer; }
  .ghost[data-on="true"]{ border-color:rgba(var(--marvin),.6); color:rgb(var(--marvin)); }
  .cap{ max-width:700px; text-align:center; color:var(--dim); font-size:13px; line-height:1.6; }
  .cap b{ color:var(--muted); font-weight:500; }
  button:focus-visible,.ibtn:focus-visible,.mshort:focus-visible{ outline:2px solid rgb(var(--marvin)); outline-offset:2px; }
  /* kiosk 模式（?kiosk=1）：拿掉簡報用外殼，screen 直接滿版貼齊實體螢幕，不留展示留白。 */
  body.kiosk{ padding:0; gap:0; }
  body.kiosk .brand, body.kiosk .cap, body.kiosk .dock2{ display:none; }
  body.kiosk .device{ width:100vw; filter:none; }
  body.kiosk .bezel{ background:none; border:none; border-radius:0; padding:0; }
  body.kiosk .screen{ border-radius:0; aspect-ratio:auto; width:100vw; height:100vh; }
</style>
</head>
<body class="__BODY_CLASS__">

<div class="brand">
  <h1>Marvin HUD</h1>
  <p>寬屏顯示框架 · 1920&times;480 · 與 macOS 的常駐互動夥伴</p>
</div>

<div class="device"><div class="bezel">
  <div class="screen">
    <div class="stage" id="stage"></div>
    <div class="nc" id="nc">
      <div class="head"><span class="t" id="nc-title"></span><button class="close" id="nc-close" aria-label="關閉">&#10005;</button></div>
      <div class="list" id="nc-list"></div>
    </div>
    <div class="dock icons-collapsed" id="dock">
      <div class="mshort" id="mshort" role="button" tabindex="0" aria-expanded="false" aria-label="Marvin，點擊展開通知列">
        <span class="mface-wrap"><span class="mface" id="mface"></span><span class="mdot" id="mdot"></span></span>
        <span class="mlabel"><b>Marvin</b><span id="mstatus">待命中</span></span>
      </div>
      <div class="vdiv"></div>
      <div class="icons collapsed" id="icons"></div>
      <div class="clock"><b id="clk">10:48</b><span id="clkd">週日 7/20</span></div>
    </div>
  </div>
</div></div>

<div class="dock2">
  <div class="seg" id="scene" role="group" aria-label="場景">
    <button data-i="0" aria-pressed="true">平靜</button>
    <button data-i="1" aria-pressed="false">會議排程</button>
    <button data-i="2" aria-pressed="false">需要回應</button>
    <button data-i="3" aria-pressed="false">建置失敗</button>
  </div>
  <button class="ghost" id="auto" data-on="true">Auto &#9656; 自動巡演</button>
</div>
<p class="cap">
  重要性階梯：<b>需要回應</b>（Hero）&gt; <b>會議排程</b> &gt; <b>Marvin</b> &gt; <b>單純資訊</b>。
  卡片依此自動變大小；<b>Marvin＝會動的頭</b>當 1.5 權重卡；「現正播放」黑膠卡輪詢 <b>/now</b>，
  有歌在播就換成真封面調色盤潑漆，沒歌在播維持 demo 樣子。
</p>

<script>
(function(){
  const TOKEN="__TOKEN__";
  const I = {
    calendar:'<rect x="4" y="6" width="16" height="15" rx="2.5"/><path d="M4 10h16M8 3v4M16 3v4"/>',
    messages:'<path d="M4 5h16v11H10l-4 4v-4H4z" stroke-linejoin="round"/>',
    music:'<circle cx="7.5" cy="17.5" r="2.5"/><circle cx="17.5" cy="15.5" r="2.5"/><path d="M10 17.5V6l10-2v11.5"/>',
    build:'<path d="M9 8l-4 4 4 4M15 8l4 4-4 4"/>',
    system:'<path d="M6 19v-6M12 19V6M18 19v-4"/>',
    weather:'<path d="M7.5 18a4.2 4.2 0 0 1-.3-8.4 5.2 5.2 0 0 1 9.9-1.1A3.7 3.7 0 0 1 16.8 18z"/>',
    alerts:'<path d="M12 4a5 5 0 0 0-5 5v4l-1.8 2.6h13.6L17 13V9a5 5 0 0 0-5-5z"/><path d="M10.2 19a1.8 1.8 0 0 0 3.6 0"/>',
    check:'<path d="M5 12l5 5 9-10"/>', mic:'<rect x="9" y="4" width="6" height="11" rx="3"/><path d="M6 12a6 6 0 0 0 12 0M12 18v3"/>',
    list:'<path d="M8 7h11M8 12h11M8 17h11M4 7h.01M4 12h.01M4 17h.01"/>',
    sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19"/>'
  };
  const svg=k=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">${I[k]||''}</svg>`;
  const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const MFACE=`<svg viewBox="0 0 40 40"><defs><radialGradient id="mg" cx="38%" cy="34%" r="70%">
      <stop offset="0" stop-color="#ffffff"/><stop offset="0.6" stop-color="#d3dae0"/><stop offset="1" stop-color="#8b959d"/></radialGradient></defs>
      <circle cx="20" cy="20" r="16" fill="url(#mg)"/>
      <path d="M10 18 L17 18 L14 24 Z" fill="#9BE04B"/><path d="M30 18 L23 18 L26 24 Z" fill="#9BE04B"/>
      <path d="M9 17.5 Q20 16 31 17.5" stroke="#12150f" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg>`;
  document.getElementById('mface').innerHTML=MFACE;

  // ---- importance ladder (weights + who becomes hero) ----
  const KIND={ respond:{w:2.6,hero:1}, schedule:{w:1.9}, marvin:{w:1.5}, info:{w:1} };

  const M = [
    [ {kind:'marvin', s:'marvin', l:'Marvin', t:'待命中', sub:'「又是漫長的一天，而它才過了兩秒。」', mood:'idle'},
      {kind:'info', s:'ok', l:'現正播放', vinyl:{title:'七里香', pal:['#F5B841','#E8749B','#7A4CC4','#2A1A44']}, meta:'七里香 · 周杰倫 · 1:23'},
      {kind:'info', s:'info', l:'待播清單', queue:[{title:'後知後覺',by:'周杰倫'},{title:'隔壁泰山',by:'周杰倫'}], g:'list'} ],
    [ {kind:'schedule', s:'info', l:'行事曆', t:'設計評審', sub:'10:30 · 42 分後 · Zoom', g:'calendar'},
      {kind:'marvin', s:'marvin', l:'Marvin', t:'要我到時提醒你？', sub:'說「好」即可', mood:'wake'},
      {kind:'info', s:'ok', l:'現正播放', vinyl:{title:'七里香', pal:['#F5B841','#E8749B','#7A4CC4','#2A1A44']}, meta:'七里香 · 周杰倫'} ],
    [ {kind:'respond', s:'warn', l:'需要回應', t:'設計評審 5 分鐘後', sub:'要現在加入嗎？', g:'calendar', actions:['加入','稍後'], ex:'設計評審 5 分鐘後開始，Zoom 連結我準備好了。說「加入」我就幫你開。'},
      {kind:'marvin', s:'marvin', l:'Marvin', t:'我可以幫你開連結', sub:'', mood:'speak'},
      {kind:'info', s:'info', l:'訊息', t:'3 則未讀', sub:'Jack、設計組…', g:'messages'} ],
    [ {kind:'respond', s:'urgent', l:'需要回應', t:'建置失敗 · main', sub:'test_stt_queue 逾時 · 要重跑嗎？', g:'build', actions:['重跑 CI','忽略'], ex:'建置在 test_stt_queue 逾時掛掉——排隊等太久。八成是暫時的，要我重跑就說一聲。'},
      {kind:'marvin', s:'marvin', l:'Marvin', t:'我看了 log，是排隊逾時', sub:'', mood:'think'} ]
  ];

  const stage=document.getElementById('stage');
  const mvParams={ mood:'idle', focusDir:0 };
  const reduce=window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- live 現正播放（/now 輪詢覆蓋 demo 黑膠）----
  let liveNow=null;
  const FALLBACK_PAL=['#9BE04B','#4C9DFF','#2A1A44','#080B11'];
  function padPal(pal){
    const out=(Array.isArray(pal)?pal:[]).filter(Boolean).slice(0,4);
    while(out.length<4) out.push(FALLBACK_PAL[out.length]);
    return out;
  }
  function resolveVinyl(demo){
    if(liveNow && liveNow.title) return {title:liveNow.title, pal:padPal(liveNow.pal), cover:liveNow.cover||''};
    return demo;
  }
  function resolveMeta(demo){
    if(liveNow && liveNow.title) return esc(liveNow.title)+(liveNow.by?' · '+esc(liveNow.by):'');
    return demo||'';
  }
  function resolveQueue(demo){
    if(liveNow && Array.isArray(liveNow.queue) && liveNow.queue.length) return liveNow.queue;
    return demo||[];
  }

  function render(i){
    const cards=[...M[i]].sort((a,b)=>KIND[b.kind].w-KIND[a.kind].w);
    stage.innerHTML=cards.map(c=>{
      const k=KIND[c.kind];
      if(c.kind==='marvin'){
        return `<div class="card mcard" role="button" tabindex="0" aria-label="Marvin，點擊講解重點卡" style="flex:${k.w} 1 0;--c:var(--marvin)">
          <div class="top"><span class="label">${c.l}</span><span class="dot"></span></div>
          <div class="mrow"><canvas class="mvhead"></canvas>
            <div class="mtext"><div class="title">${c.t}</div>${c.sub?`<div class="sub">${c.sub}</div>`:''}</div></div>
          <div class="exhint">點我＝講解重點卡</div></div>`;
      }
      if(c.vinyl){
        return `<div class="card vinyl-card" style="flex:${k.w} 1 0;--c:var(--${c.s})">
          <div class="vwrap"><canvas class="vdisc"></canvas></div>
          <div class="top"><span class="label">${c.l}</span><span class="dot"></span></div>
          <div class="vmeta">${resolveMeta(c.meta)}</div></div>`;
      }
      if(c.queue){
        const rows=resolveQueue(c.queue).slice(0,3).map((q,idx)=>
          `<li><span class="qi">${idx+1}</span><span class="qt">${esc(q.title)}</span>${q.by?`<span class="qb">${esc(q.by)}</span>`:''}</li>`).join('');
        return `<div class="card" style="flex:${k.w} 1 0;--c:var(--${c.s})">
          <div class="top"><span class="label">${c.l}</span><span class="dot"></span></div>
          <ul class="qlist">${rows||'<li class="qempty">目前沒有排隊中的歌</li>'}</ul>
          <div class="glyph">${svg(c.g||'list')}</div></div>`;
      }
      const acts=c.actions?`<div class="acts">${c.actions.map((a,x)=>`<button class="chip ${x===0?'primary':''}">${a}</button>`).join('')}</div>`:'';
      return `<div class="card ${k.hero?'hero':''}" style="flex:${k.w} 1 0;--c:var(--${c.s})" ${c.ex?`data-explain="${c.ex}"`:''}>
        <div class="top"><span class="label">${c.l}</span><span class="dot"></span></div>
        <div><div class="title">${c.t}</div>${c.sub?`<div class="sub">${c.sub}</div>`:''}${acts}</div>
        <div class="glyph ${c.g==='marvin'?'face':''}">${c.g==='marvin'?MFACE:svg(c.g)}</div></div>`;
    }).join('');
    explaining=false;
    mvParams.mood=(cards.find(c=>c.kind==='marvin')||{}).mood||'idle';
    mountHead(stage.querySelector('.mvhead'));
    const vc=cards.find(c=>c.vinyl); mountVinyl(stage.querySelector('.vinyl-card'), vc?resolveVinyl(vc.vinyl):null);
    updateFocusDir();
  }

  function updateFocusDir(){
    const mv=stage.querySelector('.mcard');
    const focus=stage.querySelector('.card.explain')||stage.querySelector('.card.hero');
    if(!mv||!focus){ mvParams.focusDir=0; return; }
    const a=mv.getBoundingClientRect(), b=focus.getBoundingClientRect();
    mvParams.focusDir=Math.max(-1,Math.min(1, ((b.left+b.right)-(a.left+a.right))/2 / a.width ));
  }

  let explaining=false, savedLine='';
  function startExplain(){
    const focus=stage.querySelector('.card.hero')||stage.querySelector('.card:not(.mcard)');
    if(!focus) return;
    focus.dataset.g0=focus.style.flexGrow||'';
    focus.style.flexGrow='4.2'; focus.classList.add('explain');
    const mt=stage.querySelector('.mcard .mtext .title'); if(mt){ savedLine=mt.textContent;
      mt.textContent=focus.dataset.explain||`關於「${focus.querySelector('.title').textContent}」，${focus.querySelector('.sub')?.textContent||'沒什麼特別的。'}`; }
    mvParams.mood='speak'; explaining=true; stopAuto(); updateFocusDir();
  }
  function endExplain(){
    if(!explaining) return; explaining=false;
    stage.querySelectorAll('.card.explain').forEach(c=>{ c.style.flexGrow=c.dataset.g0||''; c.classList.remove('explain'); });
    const mt=stage.querySelector('.mcard .mtext .title'); if(mt&&savedLine) mt.textContent=savedLine;
    mvParams.mood=(M[cur].find(c=>c.kind==='marvin')||{}).mood||'idle'; updateFocusDir();
  }
  const REPLY={'加入':'好，開連結。','稍後':'好，30 分鐘後再叫你。','重跑 CI':'重跑了。八成會過。','忽略':'隨你。反正我也不意外。'};
  function handleChip(chip){
    const card=chip.closest('.card'), label=chip.textContent.trim();
    const acts=card.querySelector('.acts'); if(acts) acts.outerHTML=`<div class="done">&#10003; ${label}</div>`;
    const mt=stage.querySelector('.mcard .mtext .title'); if(mt) mt.textContent=REPLY[label]||'好。';
    mvParams.mood='speak'; stopAuto();
  }
  stage.addEventListener('click',e=>{
    const chip=e.target.closest('.chip'); if(chip){ handleChip(chip); return; }
    const mc=e.target.closest('.mcard');
    if(mc){ explaining?endExplain():startExplain(); } else if(explaining){ endExplain(); }
  });
  stage.addEventListener('keydown',e=>{ if((e.key==='Enter'||e.key===' ')&&e.target.closest('.mcard')){
    e.preventDefault(); explaining?endExplain():startExplain(); } });

  // ---------- spinning vinyl for 現正播放 ----------
  function rng(seed){ return ()=>{ seed=(seed*1664525+1013904223)>>>0; return seed/4294967296; }; }
  function shade(hex, amt){   // amt<0 變暗（往黑混）、amt>0 變亮（往白混）
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
  // 中央標籤優先用真封面圖（iTunes/YouTube thumbnail，見 cover_palette.py）；
  // 沒有圖或圖還沒載完 → 退回程序化 splatter 標籤。快取 Image 物件避免每次 render 重抓。
  const imgCache=new Map();   // url -> Image | 'error'
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
      // 真潑漆黑膠的樣子：從標籤邊緣往外放射的噴痕，每條方向長短完全不均——多數噴痕很短、
      // 少數噴得很遠，方向之間留大片空白，不是整圈平均鋪滿（見參考圖：翻譯半透明藍膠+黑噴痕）。
      // 三種筆觸（細噴痕/潑濺塊/孤立小點）的比例每張唱片自己隨機抽一次，不是固定配方——
      // 有的歌噴痕多、有的歌潑濺塊多，混合起來才不會每張看起來都同一套公式。
      const mixRay=0.55+r()*0.7, mixSplash=0.45+r()*0.9, mixDot=0.45+r()*0.9;
      const rays=Math.floor((90+r()*70)*mixRay);
      for(let i=0;i<rays;i++){
        const ang=r()*PI2;
        const reach=Math.pow(r(),2.4);                    // 平方以上→大部分噴痕短，少數噴得遠
        const endR=LR+reach*(Rdisc-LR)*1.02;
        const segs=3+Math.floor(reach*9);                  // 噴得越遠，沿路留的斑點越多（拖尾感）
        const col=pal[Math.floor(r()*pal.length)];
        for(let s=0;s<segs;s++){
          const t=s/Math.max(1,segs-1);
          const rr=LR+t*(endR-LR)+(r()-0.5)*S*0.006;       // 半徑方向也帶點抖動，噴痕不是死直線
          const ja=ang+(r()-0.5)*0.05;
          const w=S*(0.005*(1-t*0.75)+r()*0.0025);         // 越接近尾端越細
          dctx.globalAlpha=(0.85-t*0.45)*(0.6+r()*0.4);
          dctx.fillStyle=col;
          dctx.beginPath();
          dctx.arc(cx+Math.cos(ja)*rr, cy+Math.sin(ja)*rr, w, 0, PI2);
          dctx.fill();
        }
      }
      // 混一些較大的潑濺塊（splash），不是只有細噴痕——每塊由幾顆重疊圓組成不規則形狀，
      // 大多落在靠標籤近的地方（reach 冪次偏小），少數飛遠一點。
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
      // 少量脫離主噴痕、飛得比噴痕更遠的孤立小點
      for(let i=0;i<Math.floor(rays*0.25*mixDot);i++){
        const ang=r()*PI2, rr=LR+Math.pow(r(),0.35)*(Rdisc-LR);
        dctx.globalAlpha=0.5+r()*0.4; dctx.fillStyle=pal[Math.floor(r()*pal.length)];
        dctx.beginPath(); dctx.arc(cx+Math.cos(ang)*rr, cy+Math.sin(ang)*rr, S*(0.0015+r()*0.003), 0, PI2); dctx.fill();
      }
      dctx.globalAlpha=1;
      // 溝槽反光：畫在潑漆最上層（亮線+暗線成對＝溝槽斷面的高光/陰影），alpha 要夠強才不會
      // 被下面較實心的 splash/ray 蓋掉、在整張潑漆圖案上仍看得出一圈圈唱片紋理。
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
        // 真封面：撐滿整個中央標籤圓（expand+fill，不留縫、不加邊框）。
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

  // ---------- live Marvin head (metallic + green triangle eyes) ----------
  const MOODCOL={ idle:[104,158,58], wake:[150,224,72], speak:[152,222,82], think:[92,178,120] };
  let head=null;
  function mountHead(canvas){
    if(head){ cancelAnimationFrame(head.raf); head.ro.disconnect(); head=null; }
    if(!canvas) return;
    const ctx=canvas.getContext('2d');
    const st={t:0,gphi:0,glam:0,vphi:0,vlam:0,blink:1,blinkT:1.2,blinkStart:-1,sacT:0,sacX:0,sacY:0,ec:[104,158,58].slice(),cam:1};
    let W=0,Hh=0,DPR=1;
    function size(){ const r=canvas.getBoundingClientRect(); DPR=Math.min(2,window.devicePixelRatio||1);
      W=canvas.width=Math.max(1,r.width*DPR); Hh=canvas.height=Math.max(1,r.height*DPR); }
    const ro=new ResizeObserver(size); ro.observe(canvas); size();
    const P2=Math.PI*2;
    function frame(){
      st.t+=0.03; ctx.clearRect(0,0,W,Hh);
      const mood=mvParams.mood;
      const env = mood==='speak' ? Math.max(0, Math.sin(st.t*7.3)*0.5+Math.sin(st.t*11.1)*0.3+0.2) : 0;
      st.cam += (((mood==='speak'||mood==='wake')?1.05:1)-st.cam)*0.05;
      const base=Math.min(W,Hh);
      const cx=W/2+Math.sin(st.t*0.4)*base*0.02;
      const floatY=Math.sin(st.t*0.28)*base*0.045;   // 明顯一點的上下起伏，才有懸浮感（不只是待機微動）
      const cy=Hh*0.45+floatY+env*base*0.03;
      const R=base*0.40*st.cam*(1+Math.sin(st.t*0.9)*0.006);
      // 陰影跟球體脫開一段距離、且隨浮動高度縮放變淡——飄得越高陰影越小越淡、
      // 沉得越低陰影越大越實，這種「陰影跟物體不貼在一起」才會讀成懸浮，不是貼地站著。
      const floatNorm=(floatY/(base*0.045)+1)/2;
      const shadowGap=base*0.10+floatNorm*base*0.05;
      const shadowScale=1-floatNorm*0.22, shadowAlpha=0.40-floatNorm*0.16;
      ctx.save(); ctx.translate(cx,cy+R+shadowGap); ctx.scale(shadowScale,0.22*shadowScale);
      const cs=ctx.createRadialGradient(0,0,0,0,0,R*0.85);
      cs.addColorStop(0,`rgba(0,0,0,${shadowAlpha})`); cs.addColorStop(0.7,`rgba(0,0,0,${shadowAlpha*0.4})`); cs.addColorStop(1,'rgba(0,0,0,0)');
      ctx.fillStyle=cs; ctx.beginPath(); ctx.arc(0,0,R*0.85,0,P2); ctx.fill();
      // 卡片底色接近黑，純黑陰影對比不夠、看不出來（跟卡片 box-shadow 那次同個問題）。
      // 疊一層 screen 混合的淡綠光暈——加法混合永遠比背景亮，暗底也讀得出來，順便呼應
      // 整個 HUD 的霓虹風格，讀起來像懸浮物投下的光暈而不是純陰影。
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
      const tc=MOODCOL[mood]||MOODCOL.idle; st.ec=st.ec.map((v,i)=>v+(tc[i]-v)*0.06);
      const boost=1+env*0.5, gr=Math.min(255,st.ec[0]*boost), gg=Math.min(255,st.ec[1]*boost), gb=Math.min(255,st.ec[2]*boost);
      const dark=k=>`rgba(${gr*k|0},${gg*k|0},${gb*k|0},0.98)`;
      const bright=`rgba(${Math.min(255,gr+80)|0},${Math.min(255,gg+70)|0},${Math.min(255,gb+70)|0},0.98)`;
      let tphi,tlam;
      if(mood==='think'){ tphi=-0.1+Math.sin(st.t*0.5)*0.08; tlam=-0.16+Math.sin(st.t*0.7)*0.05; }
      else if(mood==='speak'){ tphi=Math.sin(st.t*0.8)*0.05; tlam=-0.02; }
      else if(mood==='wake'){ tphi=0; tlam=-0.03; }
      else { tphi=Math.sin(st.t*0.33)*0.18; tlam=Math.sin(st.t*0.23+1.1)*0.12; }
      tphi += mvParams.focusDir*0.42;
      if(st.t>st.sacT){ st.sacT=st.t+0.4+Math.random()*1.7; st.sacX=(Math.random()-0.5)*0.1; st.sacY=(Math.random()-0.5)*0.06; }
      tphi+=st.sacX; tlam+=st.sacY;
      st.vphi+=(tphi-st.gphi)*0.018-st.vphi*0.14; st.gphi+=st.vphi;
      st.vlam+=(tlam-st.glam)*0.018-st.vlam*0.14; st.glam+=st.vlam;
      if(st.blinkStart<0&&st.t>st.blinkT){ st.blinkStart=st.t; st.blinkT=st.t+2+Math.random()*4; }
      st.blink=1;
      if(st.blinkStart>=0){ const pr=(st.t-st.blinkStart)/0.16; if(pr>=1) st.blinkStart=-1; else st.blink=1-0.92*Math.sin(pr*Math.PI); }
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
      eye(-1);eye(1);
      ctx.save();ctx.strokeStyle='rgba(16,19,17,.92)';ctx.lineWidth=Math.max(1.5,R*0.02);ctx.lineCap='round';ctx.lineJoin='round';
      const phiEnd=phiC+dw+0.26,curve=0.035;ctx.beginPath();
      for(let i=0;i<=24;i++){ const s=-1+i/12, ph=st.gphi+s*phiEnd, lm=lamC+st.glam+curve*s*s, q=proj(ph,lm); i?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); }
      ctx.stroke();ctx.restore();
      if(!reduce) head.raf=requestAnimationFrame(frame);
    }
    head={raf:requestAnimationFrame(frame),ro};
  }

  // ---- dock sources + notification center (demo) ----
  const SRC = {
    calendar:{name:'行事曆', c:'info', items:[
      {c:'warn', i:'calendar', t:'設計評審', m:'10:30 · Zoom · 設計組', time:'5 分後'},
      {c:'info', i:'calendar', t:'一對一 · Jack', m:'14:00 · 辦公室', time:'今天'},
      {c:'info', i:'calendar', t:'Marvin 週檢討', m:'明天 09:00', time:'明天'} ]},
    messages:{name:'訊息', c:'info', items:[
      {c:'info', i:'messages', t:'Jack', m:'記得看一下那個 STT 佇列的圖', time:'3 分'},
      {c:'info', i:'messages', t:'設計組', m:'新的 bar 螢幕稿放上去了', time:'21 分'},
      {c:'urgent', i:'build', t:'CI Bot', m:'main 建置失敗', time:'2 分'} ]},
    music:{name:'音樂', c:'ok', items:[
      {c:'ok', i:'music', t:'現正播放', m:'七里香 — 周杰倫', time:'now'},
      {c:'ok', i:'music', t:'待播', m:'遇見 — 孫燕姿', time:'—'},
      {c:'ok', i:'list', t:'Marvin 選的', m:'千禧華語抒情 · 8 首', time:'—'} ]},
    build:{name:'建置', c:'urgent', items:[
      {c:'urgent', i:'build', t:'marvin · main 失敗', m:'test_stt_queue 逾時 · 3m12s', time:'2 分'},
      {c:'ok', i:'check', t:'marvin · feat/hud 通過', m:'全綠 · 2m48s', time:'26 分'},
      {c:'ok', i:'check', t:'部署 prod 成功', m:'v0.9.1', time:'1 小時'} ]},
    system:{name:'系統', c:'ok', items:[
      {c:'ok', i:'system', t:'CPU 38% · 記憶體 61%', m:'一切正常', time:'now'},
      {c:'ok', i:'system', t:'電量 92%', m:'預估可用 6 小時', time:'now'},
      {c:'info', i:'system', t:'備份完成', m:'Time Machine · 昨晚', time:'昨天'} ]},
    weather:{name:'天氣', c:'info', items:[
      {c:'info', i:'sun', t:'台北 · 晴 31°', m:'體感 34° · 午後有雷陣雨', time:'now'},
      {c:'info', i:'weather', t:'15:00 降雨', m:'機率 60%', time:'午後'} ]},
    alerts:{name:'通知', c:'warn', items:[
      {c:'warn', i:'calendar', t:'設計評審 5 分鐘後', m:'要我開連結嗎？', time:'5 分'},
      {c:'urgent', i:'build', t:'CI 失敗', m:'main · test_stt_queue', time:'2 分'} ]}
  };
  const order=['calendar','messages','music','build','system','weather','alerts'];
  const badges={ messages:['3','info'], build:['!','urgent'], alerts:['2','warn'] };
  document.getElementById('icons').innerHTML = order.map(k=>{
    const b=badges[k]; const c=SRC[k].c;
    return `<button class="ibtn" data-src="${k}" aria-label="${SRC[k].name}" style="--ic:var(--${c})">
      ${svg(SRC[k].items[0].i)}${b?`<span class="badge" style="--bc:var(--${b[1]})">${b[0]}</span>`:''}</button>`;
  }).join('');
  // 資訊量降到最低：整排 icon 預設收起，Marvin 頭像只掛一顆紅點——有任何分類有未讀才亮。
  document.getElementById('mdot').classList.toggle('show', Object.keys(badges).length>0);

  const nc=document.getElementById('nc'), ncTitle=document.getElementById('nc-title'), ncList=document.getElementById('nc-list');
  function openSrc(k){
    const s=SRC[k]; ncTitle.innerHTML=`${s.name} <small>${s.items.length} 則</small>`;
    ncList.innerHTML=s.items.map(n=>`<div class="note" style="--c:var(--${n.c})">
      <div class="ni">${svg(n.i)}</div>
      <div class="nb"><div class="nt">${n.t}<time>${n.time}</time></div><div class="nm">${n.m}</div></div></div>`).join('');
    nc.classList.add('open'); stopAuto();
  }
  const closeNC=()=>nc.classList.remove('open');
  document.getElementById('icons').addEventListener('click',e=>{ const b=e.target.closest('.ibtn'); if(b) openSrc(b.dataset.src); });
  // 點 Marvin＝滑出整排 icon（不是打開通知面板）；再點一次收回去，跟 iOS 縮時通知同一套邏輯。
  const dock=document.getElementById('dock'), iconsEl=document.getElementById('icons'), mshortEl=document.getElementById('mshort');
  let iconsOpen=false;
  function setIconsOpen(open){
    iconsOpen=open;
    iconsEl.classList.toggle('collapsed', !open);
    dock.classList.toggle('icons-collapsed', !open);
    mshortEl.setAttribute('aria-expanded', String(open));
  }
  mshortEl.addEventListener('click', ()=>setIconsOpen(!iconsOpen));
  mshortEl.addEventListener('keydown', e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); setIconsOpen(!iconsOpen); } });
  document.getElementById('nc-close').addEventListener('click',closeNC);
  nc.addEventListener('click',e=>{ if(e.target===nc) closeNC(); });

  // ---- scenes ----
  const seg=document.getElementById('scene'), autoBtn=document.getElementById('auto');
  let cur=0, auto=true, timer=null;
  const STAT=['待命中','待命中','等你回應','排查中'];
  function setScene(i){ cur=i; render(i);
    seg.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed', String(+b.dataset.i===i)));
    document.getElementById('mstatus').textContent=STAT[i]; }
  seg.addEventListener('click',e=>{ const b=e.target.closest('button'); if(!b) return; stopAuto(); closeNC(); setScene(+b.dataset.i); });
  function tick(){ setScene((cur+1)%M.length); timer=setTimeout(tick,5200); }
  function startAuto(){ if(reduce){ autoBtn.dataset.on='false'; return; } auto=true; autoBtn.dataset.on='true'; clearTimeout(timer); timer=setTimeout(tick,5200); }
  function stopAuto(){ auto=false; autoBtn.dataset.on='false'; clearTimeout(timer); }
  autoBtn.addEventListener('click',()=> auto?stopAuto():startAuto());

  const pad=n=>String(n).padStart(2,'0');
  function clock(){ const d=new Date(); document.getElementById('clk').textContent=pad(d.getHours())+':'+pad(d.getMinutes());
    document.getElementById('clkd').textContent=`週${['日','一','二','三','四','五','六'][d.getDay()]} ${d.getMonth()+1}/${d.getDate()}`; }
  clock(); setInterval(clock,10000);

  // ---- 輪詢 /now：有歌在播就把「現正播放」卡換成真資料 ----
  let lastLiveKey='';
  async function refreshNow(){
    try{
      const r=await fetch("/now?t="+encodeURIComponent(TOKEN),{cache:"no-store"});
      const j=await r.json();
      liveNow = j.playing ? {title:j.title||'', by:j.by||'', pal:Array.isArray(j.palette)?j.palette:[], cover:j.cover||'',
        queue:Array.isArray(j.queue)?j.queue:[]} : null;
    }catch(e){ liveNow=null; }
    const key = liveNow ? liveNow.title+'|'+liveNow.by+'|'+liveNow.pal.join(',')+'|'+liveNow.cover+'|'+liveNow.queue.map(q=>q.title).join(',') : '';
    if(key!==lastLiveKey){ lastLiveKey=key; render(cur); }
  }

  setScene(0); startAuto();
  refreshNow(); setInterval(refreshNow,4000);
})();
</script>

</body></html>"""


async def inject_audio(vc, wav_bytes: bytes) -> bool:
    """把瀏覽器上傳的 WAV 轉錄後，走 inject_text（is_text_input）強制回覆。

    為何不走 process_audio_slice：那條經喚醒判定，沒喊「馬文」會被當環境對話→不回話。
    PTT 按鈕＝明確在跟馬文講話，故先用引擎的編譯 STT 二進位（_run_swift_stt，Speech
    辨識需簽章 entitlements，直譯 swift 會被 SIGKILL）拿文字，再走已驗證會回覆的 /say 路
    （inject_text→is_text_input=True）。回覆 TTS 走 mixer → BrowserSpeakerOutput → /reply。

    回 True＝已注入；False＝空 / STT 無結果。
    """
    if not wav_bytes:
        return False
    speaker = os.getenv("MARVIN_SATELLITE_SPEAKER", "狗與露")
    v2 = os.getenv("STT_ENGINE_V2", "").strip().lower() in ("1", "true", "yes", "on")
    fd, tmp_path = tempfile.mkstemp(prefix="satellite_ptt_", suffix=".wav")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as f:
            f.write(wav_bytes)
        raw_text, _meta = await vc.bot.engine._run_swift_stt(
            tmp_path, is_wake_check=False, v2=v2)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    raw_text = (raw_text or "").strip()
    if not raw_text:
        logger.info("🎙️ [SatelliteAudio] STT 無結果（雜訊/靜音），略過")
        return False
    logger.info(f"🎙️ [SatelliteAudio] {speaker}: {raw_text}")
    await inject_text(vc, speaker, raw_text)   # is_text_input=True → 強制回覆
    return True


def build_text_app(vc, *, token: str | None = None, default_speaker: str = "狗與露",
                   reply_source=None, car_presence=None, audio_rate_limiter=None,
                   stream_source=None, location_state_path=None,
                   now_playing_state_path=None):
    """組 aiohttp Application：POST /say 收文字→注入 pipeline（Siri 捷徑入口）。

    純 wiring、無 side effect（不起 server），好測。token=None＝不驗證
    （Tailscale 私網信任）；設了 token 就檢查 X-Marvin-Token header。
    location_state_path＝GPS 訊號存檔路徑（None＝用 location_state.DEFAULT_PATH，測試時
    傳 tmp_path 隔離）。
    now_playing_state_path＝跨進程現正播放橋接檔路徑（None＝用
    now_playing_state.DEFAULT_PATH；main_discord.py 的 MusicCog 寫、這裡的 /now 讀，見
    now_playing_state.py docstring）。
    """
    from aiohttp import web

    from location_state import DEFAULT_PATH as _GPS_DEFAULT_PATH
    from location_state import save_location_state
    from now_playing_state import DEFAULT_PATH as _NOW_DEFAULT_PATH
    from now_playing_state import load_now_playing_state

    _gps_path = location_state_path or _GPS_DEFAULT_PATH
    _now_path = now_playing_state_path or _NOW_DEFAULT_PATH

    _CORS = {"Access-Control-Allow-Origin": "*",
             "Access-Control-Allow-Headers": "*",
             "Access-Control-Allow-Methods": "POST, OPTIONS"}

    async def handle_say(request):
        ctype = request.headers.get("Content-Type", "")
        if "application/json" in ctype:
            data = await request.json()
            text = (data.get("text") or "").strip()
            speaker = data.get("speaker") or default_speaker
        else:
            text = (await request.text()).strip()
            speaker = request.query.get("speaker") or default_speaker
        if not text:
            return web.json_response({"error": "empty"}, status=400, headers=_CORS)
        await inject_text(vc, speaker, text)
        return web.json_response({"ok": True, "speaker": speaker, "text": text}, headers=_CORS)

    async def handle_play(request):
        """GET /play?q=歌名&t=token — Siri 捷徑點歌（伺服器補「放一首」，捷徑只要一格 URL）。"""
        q = (request.query.get("q") or "").strip()
        if not q:
            return web.json_response({"error": "empty"}, status=400, headers=_CORS)
        # 統一成 strong_play「放一首X」：裸「放X」不夠強（見記憶）；已含「放一首」不重複補
        if q.startswith("放一首"):
            text = q
        else:
            core = q[1:].strip() if q.startswith("放") else q
            text = f"放一首{core}"
        speaker = request.query.get("speaker") or default_speaker
        await inject_text(vc, speaker, text)
        return web.json_response({"ok": True, "speaker": speaker, "text": text}, headers=_CORS)

    async def handle_now(request):
        """回當前播放的歌（控制台「現正播放中」輪詢）。走統一 token gate（?t= 帶 token）。

        satellite 自己的 bot 不登入 Discord，本地 MusicCog 只在 car puck/瀏覽器模式自己播歌
        時才有東西；沒有的話退回讀跨進程橋接檔（main_discord.py 真正在 Discord 播的狀態）。
        本地優先——car puck 播放是即時真相，不該被舊橋接檔蓋掉。
        """
        mc = None
        try:
            mc = vc.bot.cogs.get("MusicCog")
        except Exception:  # noqa: BLE001
            mc = None
        info = getattr(mc, "_current_stream_info", None) if mc else None
        playing = bool(mc and getattr(mc, "stream_mode", False) and info)
        if not playing:
            state = load_now_playing_state(path=_now_path)
            if state and state.get("playing"):
                return web.json_response({
                    "playing": True,
                    "paused": False,
                    "title": state.get("title", ""),
                    "by": state.get("by", ""),
                    "cover": state.get("cover", ""),
                    "palette": state.get("palette", []),
                    "queue": [],
                }, headers=_CORS)
            return web.json_response({"playing": False}, headers=_CORS)
        _q = getattr(mc, "stream_queue", None)
        queue = ([{"title": s.get("title", ""), "by": s.get("requested_by", "")}
                  for s in _q[:10]] if isinstance(_q, list) else [])
        return web.json_response({
            "playing": True,
            "paused": bool(getattr(mc, "stream_paused", False)),
            "title": info.get("title", ""),
            "by": info.get("requested_by", ""),
            "cover": info.get("thumbnail", ""),  # 封面 URL（iTunes 優先，退回 yt-dlp 縮圖）
            "palette": info.get("palette", []),  # 封面主色，給 vinyl splatter 用
            "queue": queue,  # 待播放佇列（next up），控制台顯示
        }, headers=_CORS)

    async def handle_wake(request):
        if hasattr(vc, "_on_satellite_wake"):
            # 🎙️ [PTT Optimization] 設定 mixer 的 PTT 狀態為 True，使其在 80ms 內平滑降至 0% 靜音
            if getattr(vc, "_mixer", None) is not None:
                vc._mixer._ptt_active = True
            
            vc._on_satellite_wake("hey_marvin")
            bridge = getattr(vc, "_satellite_bridge", None)
            if bridge and bridge.sink and hasattr(bridge.sink, "reset"):
                bridge.sink.reset()
                # 🎙️ [PTT Optimization] PTT 期間將自動靜默切句閾值拉高至 999 秒，
                # 防止說話中途停頓或播音時間長導致 VAD 自動切句，強制只在 PTT 結束時由 /flush 切句。
                bridge.sink._silence_cut_s = 999.0
            logger.info("🎙️ [PTT] Mac 端已收到 /wake 請求，成功 Ducking 音樂並重置語音緩衝區（停用自動 VAD）")
            return web.json_response({"ok": True}, headers=_CORS)
        return web.json_response({"error": "method_not_found"}, status=500, headers=_CORS)

    async def handle_flush(request):
        
        # 🎙️ [PTT Optimization] 解除 mixer PTT 狀態，音樂將自動淡入恢復播放
        if getattr(vc, "_mixer", None) is not None:
            vc._mixer._ptt_active = False
        
        bridge = getattr(vc, "_satellite_bridge", None)
        if bridge and bridge.sink:
            # 🎙️ [PTT Optimization] 恢復標準 VAD 靜默切句時間 (1.5s) 並立刻強制切句
            bridge.sink._silence_cut_s = 1.5
            bridge.sink._cut_segment()
            logger.info("🎙️ [PTT] Mac 端已收到 /flush 請求，已強行斷句進行 STT（恢復 VAD）")
            return web.json_response({"ok": True}, headers=_CORS)
        return web.json_response({"error": "no_active_bridge"}, status=400, headers=_CORS)

    async def handle_audio(request):
        """POST /audio — 純軟體 satellite：瀏覽器收音的 WAV → 引擎 pipeline。

        body＝原始 WAV bytes。餵 process_audio_slice（user_id=satellite），STT / 回覆非同
        步；回覆音訊走 GET /reply。回 {ok}；ok=False＝空 / 非法 WAV。
        """
        # eng review 架構#2：funnel 公開後 per-token 限速，擋 token 外洩→付費灌爆。
        if audio_rate_limiter is not None:
            key = (request.headers.get("X-Marvin-Token") or request.query.get("t")
                   or request.remote or "anon")
            if not audio_rate_limiter.allow(key):
                return web.json_response({"error": "rate_limited"}, status=429, headers=_CORS)
        wav_bytes = await request.read()
        if not wav_bytes:
            return web.json_response({"error": "empty"}, status=400, headers=_CORS)
        ok = await inject_audio(vc, wav_bytes)
        return web.json_response({"ok": ok}, headers=_CORS)

    async def handle_reply(request):
        """GET /reply?since=N — 純軟體 satellite：回馬文最新一段 TTS 的 WAV。

        seq 遞增；瀏覽器帶上次 since，新段（seq>since）回 200+WAV，否則 204。
        無 reply_source（未接輸出 tee）→ 一律 204。
        """
        if reply_source is None:
            return web.Response(status=204, headers=_CORS)
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            since = 0
        seq, wav = reply_source.latest_wav()
        if seq <= since or not wav:
            return web.Response(status=204, headers=_CORS)
        headers = {**_CORS, "X-Reply-Seq": str(seq)}
        return web.Response(body=wav, content_type="audio/wav", headers=headers)

    async def handle_satellite(request):
        """GET /satellite — Mac 自服務的純軟體 satellite 網頁（Pi 不參與）。"""
        return web.Response(
            text=SATELLITE_HTML.replace("__TOKEN__", token or ""),
            content_type="text/html", headers=_CORS)

    async def handle_hud(request):
        """GET /hud — Marvin HUD v12 寬屏顯示頁（Mac 自服務，比照 /satellite）。

        ?kiosk=1 拿掉簡報用外殼（品牌標題/裝置邊框/說明文字），screen 滿版貼齊實體螢幕；
        不帶則是瀏覽器預覽模式（保留外殼方便截圖/討論）。
        """
        kiosk = (request.query.get("kiosk") or "").strip().lower() in ("1", "true", "yes")
        body_class = "kiosk" if kiosk else ""
        html = HUD_HTML.replace("__TOKEN__", token or "").replace("__BODY_CLASS__", body_class)
        return web.Response(text=html, content_type="text/html", headers=_CORS)

    async def handle_audio_stream(request):
        """GET /audio_stream — 車載 puck 連續收音：chunked 即時轉送 mixer PCM。

        跟 /reply 不同：不做靜音切段緩衝整段回，而是 frame 一到就吐給連線中的 client，
        讓 ESP32 能像收音機一樣連續播放整份歌單，不受單段緩衝上限限制（見 StreamSpeakerOutput）。
        無 stream_source（車載模式未接串流輸出）→ 404。
        """
        if stream_source is None:
            return web.Response(status=404, headers=_CORS)
        resp = web.StreamResponse(status=200, headers={
            **_CORS, "Content-Type": "application/octet-stream",
            "X-Audio-Rate": str(stream_source.rate),
            "X-Audio-Channels": str(stream_source.channels),
            "X-Audio-Bits": str(stream_source.bits),
        })
        await resp.prepare(request)
        q = stream_source.subscribe()
        try:
            while True:
                frame = await q.get()
                if frame is None:
                    break
                await resp.write(frame)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            stream_source.unsubscribe(q)
        return resp

    async def handle_car(request):
        """POST /car {"state": "present"|"absent", "lat"?, "lon"?} — ESP32 puck 車載觸發。

        present＝上車/heartbeat（到達觸發讀空氣開場一次、後續續期）；
        absent＝主動離開停播。熄火斷電靠 CarPresence 的 TTL 收尾（present 不 sticky）。
        車載模式未接（car_presence=None）→ 400 car_mode_off。
        lat/lon 為韌體端 15 分鐘節流後才附帶的 GPS 讀數；沒帶就不動 location_state
        （其餘心跳沒座標，不該把上次存的座標覆蓋成空）。
        """
        if car_presence is None:
            return web.json_response({"error": "car_mode_off"}, status=400, headers=_CORS)
        if "application/json" in request.headers.get("Content-Type", ""):
            body = await request.json()
            state = (body.get("state") or "").strip()
            lat, lon = body.get("lat"), body.get("lon")
        else:
            state = (request.query.get("state") or "").strip()
            lat, lon = None, None
        if state == "present":
            await car_presence.present()
        elif state == "absent":
            await car_presence.absent()
        else:
            return web.json_response({"error": "bad_state"}, status=400, headers=_CORS)
        if lat is not None and lon is not None:
            save_location_state(lat=float(lat), lon=float(lon), ts=time.time(), path=_gps_path)
        return web.json_response(
            {"ok": True, "state": state, "present": car_presence.is_present}, headers=_CORS)

    async def handle_preflight(request):
        return web.Response(status=204, headers=_CORS)

    @web.middleware
    async def _token_gate(request, handler):
        # eng review 架構#1：Funnel 公開整台 server → 統一 token gate 全端點。
        # token=None＝Tailscale 私網信任、不驗證；OPTIONS preflight 帶不了自訂 auth 故放行。
        # token 可走 X-Marvin-Token header 或 ?t=（控制台網頁跨網域呼叫方便）。
        if token and request.method != "OPTIONS":
            tok = request.headers.get("X-Marvin-Token") or request.query.get("t")
            if tok != token:
                return web.json_response({"error": "unauthorized"}, status=401, headers=_CORS)
        return await handler(request)

    app = web.Application(client_max_size=32 * 1024 * 1024,  # 容納整句 PTT 音訊
                          middlewares=[_token_gate])
    app.router.add_post("/say", handle_say)
    app.router.add_options("/say", handle_preflight)
    app.router.add_get("/play", handle_play)
    app.router.add_get("/now", handle_now)
    app.router.add_post("/wake", handle_wake)
    app.router.add_options("/wake", handle_preflight)
    app.router.add_post("/flush", handle_flush)
    app.router.add_options("/flush", handle_preflight)
    app.router.add_post("/audio", handle_audio)
    app.router.add_options("/audio", handle_preflight)
    app.router.add_get("/reply", handle_reply)
    app.router.add_get("/audio_stream", handle_audio_stream)
    app.router.add_get("/satellite", handle_satellite)
    app.router.add_get("/hud", handle_hud)
    app.router.add_post("/car", handle_car)
    app.router.add_options("/car", handle_preflight)
    return app


async def start_text_http_server(vc, reply_source=None, stream_source=None):
    """起 Siri 文字 HTTP 伺服器（0.0.0.0，走 Tailscale）。回傳 runner（好收）。

    埠＝MARVIN_TEXT_PORT（預設 8790）；token＝MARVIN_TEXT_TOKEN（空＝不驗證）。
    reply_source＝純軟體 satellite 的 BrowserSpeakerOutput（GET /reply）；Pi 模式傳 None。
    stream_source＝車載模式的 StreamSpeakerOutput（GET /audio_stream）；非車載模式傳 None。
    """
    from aiohttp import web

    port = int(os.getenv("MARVIN_TEXT_PORT", "8790"))
    token = os.getenv("MARVIN_TEXT_TOKEN", "").strip() or None
    default_speaker = os.getenv("MARVIN_SATELLITE_SPEAKER", "狗與露")

    # ── 車載模式（ESP32 puck）：MARVIN_CAR_MODE=1 才接；預設 off＝零行為改變 ──
    car_presence = None
    audio_rate_limiter = None
    if os.getenv("MARVIN_CAR_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
        from car_mode import build_car_presence, run_car_ttl_loop
        from music_recommender import build_member_pools
        from rate_limiter import RateLimiter
        owner = default_speaker

        def _pool_provider():
            # 車載＝機主一人的候選池（復用既有 build_member_pools 純函式，見 CodeQ#4）。失敗→空池降級。
            try:
                mc = vc.bot.cogs.get("MusicCog")
                mm = getattr(mc, "mm", None) or getattr(mc, "_music_memory", None)
                if mm is None:
                    return []
                pools = build_member_pools(members=[owner], songs=mm.all_songs(),
                                           exclude_titles=[], now=time.time())
                return pools.get(owner, [])
            except Exception:  # noqa: BLE001
                logger.exception("[CarMode] pool_provider 失敗，回空池")
                return []

        async def _play_open(car_open):
            # 開場：復用 /play 那招 inject_text「放一首X」讓 pipeline 解析+播+DJ；絕不即時付費 LLM。
            try:
                if car_open.song:
                    await inject_text(vc, owner, f"放一首{car_open.song.anchor_title}")
                logger.info("🚗 [CarMode] 上車開場：%s → 放《%s》",
                            car_open.line, car_open.song.anchor_title if car_open.song else "—")
            except Exception:  # noqa: BLE001
                logger.exception("[CarMode] play_open 失敗")

        async def _stop_playback():
            try:
                mc = vc.bot.cogs.get("MusicCog")
                if mc and hasattr(mc, "stop_stream"):
                    await mc.stop_stream(reason="下車（puck absent）")
                logger.info("🚗 [CarMode] 下車停播")
            except Exception:  # noqa: BLE001
                logger.exception("[CarMode] stop_playback 失敗")

        car_presence = build_car_presence(
            play_open=_play_open, stop_playback=_stop_playback, pool_provider=_pool_provider)
        # funnel 公開後 /audio per-token 限速：每 token 每分鐘 30 次（架構#2 付費鐵則）。
        audio_rate_limiter = RateLimiter(max_per_window=30, window_s=60.0)
        logger.info("🚗 [CarMode] 車載模式啟用（/car present/absent + TTL 收尾 + /audio 限速）")

    app = build_text_app(vc, token=token, default_speaker=default_speaker,
                         reply_source=reply_source, car_presence=car_presence,
                         audio_rate_limiter=audio_rate_limiter, stream_source=stream_source)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    if car_presence is not None:
        asyncio.create_task(run_car_ttl_loop(car_presence))
    _auth = "有 token 保護" if token else "⚠️ 無 token（僅靠 Tailscale 私網）"
    logger.info(
        f"📝 [TextInput] Siri HTTP 伺服器啟動：POST :{port}/say（speaker={default_speaker}，{_auth}）")
    return runner


async def _stdin_text_input_loop(vc):
    """監聽 stdin 輸入文字，直接注入 Marvin pipeline（本機終端手打／貼 Siri 轉錄）。"""
    import sys
    # ⚠️ stdin 非互動終端（launchd / nohup </dev/null / 背景進程）：readline 立即回 ""
    # 不阻塞，且 EOFError 不會觸發（readline 回 "" 不 raise）→ while 迴圈瘋狂空轉、狂丟
    # run_in_executor 任務 → 燒滿一核 CPU。非 tty 就不啟用（本來也沒終端可打字）。
    if not sys.stdin or not sys.stdin.isatty():
        logger.info("📝 [TextInput] stdin 非互動終端，跳過 stdin 輸入迴圈（避免 EOF busy-spin）")
        return

    loop = asyncio.get_event_loop()
    speaker = os.getenv("MARVIN_SATELLITE_SPEAKER", "狗與露")

    logger.info(f"📝 [TextInput] stdin 模式啟用（speaker={speaker}）；打字後按 Enter 送出")

    while True:
        try:
            text = await loop.run_in_executor(None, sys.stdin.readline)
            if not text:   # EOF：readline 回 "" 不 raise EOFError；不 break 會 busy-spin
                break
            await inject_text(vc, speaker, text)
        except EOFError:
            break
        except Exception as e:  # noqa: BLE001
            logger.error(f"❌ [TextInput] 處理文字失敗: {e}", exc_info=True)


async def main():
    # 錨定 repo 根目錄：相對路徑的正本記憶(marvin.db/music_memory.json/records/)+assets+
    # models+repo 的 .env(GUILD_ID) 全用正本，不論從哪啟動都不會漂到別的 worktree。
    os.chdir(repo_root())
    load_dotenv()
    # 沙盒必須在建 bot（→建各 store）之前啟用，否則 store 會以讀寫模式開連線
    maybe_activate_memory_sandbox(os.environ)
    _warnings = check_identity_alignment(os.environ)
    for _w in _warnings:
        logger.warning(f"⚠️ [Satellite] 記憶對齊：{_w}")
    if not _warnings:
        _gid = os.environ.get("GUILD_ID", "0")
        _spk = os.environ.get("MARVIN_SATELLITE_SPEAKER", "")
        logger.info(f"🛰️ [Satellite] 記憶錨定正本：repo={repo_root()} guild={_gid} speaker={_spk}（同一個靈魂）")
    bot = build_local_bot()
    # async with bot: 進入 _async_setup_hook（設 event loop）但不呼叫 setup_hook，
    # 不觸發 tree.sync 或任何 Discord 連線動作。
    async with bot:
        # 純軟體 satellite（MARVIN_SATELLITE_BROWSER=1）：手機瀏覽器收音+放音，完全不連 Pi。
        # 與 Pi 衛星是兩條獨立路，Pi 模式（else）一行不受影響。
        if os.getenv("MARVIN_SATELLITE_BROWSER", "").strip().lower() in ("1", "true", "yes", "on"):
            vc, browser_out, stream_out = await setup_browser_satellite(bot)
            port = os.getenv("MARVIN_TEXT_PORT", "8790")
            logger.info(f"🛰️ [Satellite] 純軟體瀏覽器模式（無 Pi）：手機開 http://<mac>:{port}/satellite")
            asyncio.create_task(_stdin_text_input_loop(vc))
            await start_text_http_server(vc, reply_source=browser_out, stream_source=stream_out)
            await asyncio.Event().wait()
            return

        vc = await setup_satellite(bot)
        host = os.getenv("MARVIN_SATELLITE_HOST", "marvinpi.local")
        logger.info(f"🛰️ [Satellite] 衛星模式啟動完成，連向 {host}，等 Pi 麥喚醒...")

        # 文字輸入：stdin（本機手打）+ HTTP（Siri 捷徑走 Tailscale POST /say）
        asyncio.create_task(_stdin_text_input_loop(vc))
        await start_text_http_server(vc)
        # Selftest：免喚醒直接播放，測音訊路徑（腦 mixer→衛星→喇叭，如 BT 連續音樂穩定性）。
        # 不設任何 SELFTEST_* env＝一般模式、零影響。兩種模式：
        #   MARVIN_SATELLITE_SELFTEST_MP3   ＝本地 mp3 檔或資料夾，連續播（繞過 YouTube／yt-dlp
        #                                     限流＝乾淨測 BT 音訊路徑＋換歌轉場）
        #   MARVIN_SATELLITE_SELFTEST_QUERY ＝語音點歌 query（走 yt-dlp）
        _mp3 = os.getenv("MARVIN_SATELLITE_SELFTEST_MP3", "").strip()
        _q = os.getenv("MARVIN_SATELLITE_SELFTEST_QUERY", "").strip()
        if _mp3:
            import glob
            import discord
            async def _selftest_local():
                await asyncio.sleep(6)
                mc = bot.cogs.get("MusicCog")
                vc = bot.cogs.get("VoiceController")
                if mc is None or vc is None:
                    logger.warning("⚠️ [Selftest] cog 未載入，跳過")
                    return
                files = sorted(glob.glob(os.path.join(_mp3, "*.mp3"))) if os.path.isdir(_mp3) else [_mp3]
                device = vc._resolve_playback_device()
                if device is None:
                    logger.warning("⚠️ [Selftest] 無播放裝置，跳過")
                    return
                logger.info(f"🎵 [Selftest] 本地 MP3 連續播放 {len(files)} 首（繞過 YouTube）")
                mc.stream_mode = True
                for f in files:
                    if not mc.stream_mode:
                        break
                    logger.info(f"🎵 [Selftest] ▶ {os.path.basename(f)}")
                    vc._current_stream_url = f
                    try:
                        # 乾淨 FFmpegPCMAudio（無 -reconnect 網路參數，本地檔才開得起來）→
                        # 真實 mixer 路徑，連續播＝測 BT 音訊 + 換歌轉場。
                        await vc._mixer_play_music(
                            device, discord.FFmpegPCMAudio(f),
                            still_active=lambda: mc.stream_mode, volume_attr="stream_volume")
                    except Exception as e:   # noqa: BLE001
                        logger.warning(f"⚠️ [Selftest] 播放失敗 {os.path.basename(f)}: {e}")
                mc.stream_mode = False
                logger.info("🎵 [Selftest] 本地 MP3 全部播完")
            asyncio.create_task(_selftest_local())
        elif _q:
            _spk = os.getenv("MARVIN_SATELLITE_SPEAKER", "狗與露")
            async def _selftest_play():
                await asyncio.sleep(6)   # 等衛星橋連上 Pi + Pi 端就緒
                mc = bot.cogs.get("MusicCog")
                if mc is None:
                    logger.warning("⚠️ [Selftest] MusicCog 未載入，跳過")
                    return
                logger.info(f"🎵 [Selftest] 免喚醒直接點歌：{_q}（speaker={_spk}）")
                await mc._safe_music_command(_spk, _q, "play")
            asyncio.create_task(_selftest_play())
        await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 [Satellite] 收到 Ctrl-C，正在結束...")
