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
                   stream_source=None):
    """組 aiohttp Application：POST /say 收文字→注入 pipeline（Siri 捷徑入口）。

    純 wiring、無 side effect（不起 server），好測。token=None＝不驗證
    （Tailscale 私網信任）；設了 token 就檢查 X-Marvin-Token header。
    """
    from aiohttp import web

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
        """回當前播放的歌（控制台「現正播放中」輪詢）。走統一 token gate（?t= 帶 token）。"""
        mc = None
        try:
            mc = vc.bot.cogs.get("MusicCog")
        except Exception:  # noqa: BLE001
            mc = None
        info = getattr(mc, "_current_stream_info", None) if mc else None
        playing = bool(mc and getattr(mc, "stream_mode", False) and info)
        if not playing:
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
        """POST /car {"state": "present"|"absent"} — ESP32 puck 車載觸發。

        present＝上車/heartbeat（到達觸發讀空氣開場一次、後續續期）；
        absent＝主動離開停播。熄火斷電靠 CarPresence 的 TTL 收尾（present 不 sticky）。
        車載模式未接（car_presence=None）→ 400 car_mode_off。
        """
        if car_presence is None:
            return web.json_response({"error": "car_mode_off"}, status=400, headers=_CORS)
        if "application/json" in request.headers.get("Content-Type", ""):
            state = ((await request.json()).get("state") or "").strip()
        else:
            state = (request.query.get("state") or "").strip()
        if state == "present":
            await car_presence.present()
        elif state == "absent":
            await car_presence.absent()
        else:
            return web.json_response({"error": "bad_state"}, status=400, headers=_CORS)
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
