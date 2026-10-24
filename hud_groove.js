// hud_groove.js — 放歌時 Marvin 頭跟著律動的純函式，/hud 與 /stage 共用唯一真相來源。
// car_http_app.py 用 GROOVE_JS 佔位字串（前後各兩個底線；這裡故意不寫全，否則內嵌後
// HTML 裡會殘留那串字、被「佔位字串有沒有替換乾淨」的測試抓到）把這份檔案整段內嵌進 <script>；
// node --test 直接 require 這個檔案測，不是另外重寫一份邏輯。
// 不得引用其他 HUD 檔（hud_shared.js／hud_performance.js）的任何符號。

const GROOVE_AMP = { pitch:0.16, bob:1.10, tiltNod:4.0, tiltSway:9, gaze:0.24, swayX:0.80 }; // 使用者選的「大」檔
const GROOVE_NOD_MIN_BPM = 120;   // BPM ≥ 120 點頭，其他（含沒有 BPM）搖擺（10/25 原 100）
const GROOVE_OUTRO_SEC = 15;      // 歌曲最後 15 秒改搖擺並漸弱
const GROOVE_FADE_SEC = 0.5;      // mood 非 idle / 停歌時淡出的時間尺度

function grooveNodBeats(bpm){
  const candidates=[1,2,4];
  // 1e-9：平手時浮點誤差會讓較大的拍數「贏」一點點（180 BPM 的 2 拍 vs 4 拍），規格是平手取小
  return candidates.reduce((best,b)=>
    Math.abs(60*b/bpm-1) < Math.abs(60*best/bpm-1)-1e-9 ? b : best
  );
}

function grooveMode(bpm, remainingSec){
  return (Number.isFinite(bpm) && bpm>=GROOVE_NOD_MIN_BPM && remainingSec>=GROOVE_OUTRO_SEC) ? 'nod' : 'sway';
}

function groovePeriod(mode, bpm){
  if(mode==='nod') return 60*grooveNodBeats(bpm)/bpm;
  return bpm ? 60*8/bpm : 6.5;
}

function grooveRemainingSec(duration, songStartTime, nowSec){
  if(duration>0 && songStartTime>0) return duration-(nowSec-songStartTime);
  return Infinity;
}

const ss=x=>x*x*(3-2*x);

function createGroove(){
  let last=null, gain=0, phase=0, num=0, mode='sway';
  return {
    step(nowSec, input){
      const {playing, bpm, remainingSec: remainingSecIn, moodIdle} = input;
      const remainingSec = (remainingSecIn==null || Number.isNaN(remainingSecIn)) ? Infinity : remainingSecIn;
      const dt = last==null ? 0 : Math.min(0.1, Math.max(0, nowSec-last));
      last = nowSec;

      const want = (playing && moodIdle) ? 1 : 0;
      gain += (want-gain) * Math.min(1, dt/GROOVE_FADE_SEC*3);

      const m = grooveMode(bpm, remainingSec);
      if(m!==mode){ mode=m; phase=0; num=0; }

      if(playing){
        phase += dt/groovePeriod(mode, bpm);
        if(phase>=1){ phase-=1; num++; }
      }

      const fade = gain * (remainingSec<GROOVE_OUTRO_SEC ? Math.max(0, remainingSec/GROOVE_OUTRO_SEC) : 1);
      if(fade<0.001) return {};

      if(mode==='nod'){
        const d = phase<0.45 ? ss(phase/0.45) : 1-ss((phase-0.45)/0.55);
        const A = (num%2===0 ? 1 : 0.6) * fade * d;
        return {pitch:GROOVE_AMP.pitch*A, bobY:GROOVE_AMP.bob*A, headTiltZ:GROOVE_AMP.tiltNod*A};
      }
      const sw = Math.sin(2*Math.PI*phase) * fade;
      return {headTiltZ:GROOVE_AMP.tiltSway*sw, gazeDir:GROOVE_AMP.gaze*sw, swayX:GROOVE_AMP.swayX*sw};
    },
  };
}

if (typeof module !== 'undefined' && module.exports){
  module.exports = {
    GROOVE_AMP, GROOVE_NOD_MIN_BPM, GROOVE_OUTRO_SEC, GROOVE_FADE_SEC,
    grooveNodBeats, grooveMode, groovePeriod, grooveRemainingSec, createGroove,
  };
}
