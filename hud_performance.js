// HUD 疊加表演引擎：Action(做什麼) + Emotion(怎麼做) 兩層資料 + 直譯器純函式。
// 設計文件：~/.gstack/projects/butthead0819-beep-marvin-voice-core/jackhuang-main-design-MarvinPerformanceScript-20260727-104135.md
// main_satellite.py 的 HUD_HTML 用 __PERF_JS__ 佔位字串把這份檔案整段內嵌進 <script>，
// 這裡同時是唯一真相來源——node --test 直接 require 這個檔案測，不是另外重寫一份邏輯。

const PERF_ROUND_SEC = 420; // 7 分鐘一輪，見設計文件 Open Questions #（重播間隔定案）

const ACTIONS = {
  put_on_sunglasses: {
    dur: 0.6, kind: 'enter', pair: 'take_off_sunglasses',
    channels: {
      headTiltZ: [{t:0,v:0},{t:0.4,v:8},{t:1,v:0}],
      scale:     [{t:0,v:1},{t:0.3,v:1.05},{t:1,v:1}],
      freeze:    [{t:0,v:0},{t:0.8,v:1},{t:1,v:1}],
    },
  },
  take_off_sunglasses: {
    dur: 0.6, kind: 'exit', pair: 'put_on_sunglasses',
    channels: { headTiltZ: [{t:0,v:0},{t:0.5,v:-6},{t:1,v:0}] },
  },
  wave: {
    dur: 1.8, kind: 'oneshot',
    channels: {
      swayX:   [{t:0,v:0},{t:.25,v:1},{t:.5,v:-1},{t:.75,v:1},{t:1,v:0}],
      gazeDir: [{t:0,v:0},{t:1,v:.3}],
    },
  },
  idle_bob: {
    kind: 'loop',
    channels: { bobY: [{t:0,v:0},{t:.5,v:1},{t:1,v:0}] },
  },
};

const EMOTIONS = {
  confident: { gain: { headTiltZ: 1.3, scale: 1.1 }, speed: 1.2, gazeBias: 0.15 },
  sad:       { gain: { headTiltZ: 0.5, scale: 0.9 }, speed: 0.6, gazeBias: -0.2 },
  neutral:   { gain: {}, speed: 1, gazeBias: 0 },
};

const PERF_THEMES = {
  matrix: { actions: ACTIONS },
};

// 每個通道的中性值——動作沒宣告這個通道時，直譯器要回傳這個而不是 0（scale 的中性值是 1，不是 0）。
const CHANNEL_NEUTRAL = { scale: 1 };
function neutralOf(ch){ return CHANNEL_NEUTRAL[ch] ?? 0; }

function evalKeyframes(kf, progress){
  if (!kf || !kf.length) return 0;
  if (progress <= kf[0].t) return kf[0].v;
  for (let i = 1; i < kf.length; i++){
    const a = kf[i-1], b = kf[i];
    if (progress <= b.t){
      const span = b.t - a.t;
      const p = span > 0 ? (progress - a.t) / span : 1;
      return a.v + (b.v - a.v) * p;
    }
  }
  return kf[kf.length-1].v;
}

function sampleAction(action, emotion, progress){
  emotion = emotion || EMOTIONS.neutral;
  const out = {};
  for (const [ch, kf] of Object.entries(action.channels || {})){
    out[ch] = evalKeyframes(kf, progress) * (emotion.gain?.[ch] ?? 1);
  }
  if (emotion.gazeBias) out.gazeDir = (out.gazeDir || 0) + emotion.gazeBias;
  return out;
}

function shuffle(arr, rand){
  rand = rand || Math.random;
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--){
    const j = Math.floor(rand() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// 洗牌規則：enter/exit 配對永遠保持相對順序(先開後關)，oneshot 隨機插入配對之間或前後。
function buildRoundOrder(theme, rand){
  rand = rand || Math.random;
  const ids = Object.keys(theme.actions).filter(id => theme.actions[id].kind !== 'loop');
  const enterIds = ids.filter(id => theme.actions[id].kind === 'enter');
  const singleIds = shuffle(ids.filter(id => theme.actions[id].kind === 'oneshot'), rand);
  const order = [];
  for (const enterId of enterIds) order.push(enterId, theme.actions[enterId].pair);
  singleIds.forEach(id => {
    const pos = 1 + Math.floor(rand() * (order.length || 1));
    order.splice(Math.min(pos, order.length), 0, id);
  });
  return order;
}

// 給定這輪的排序跟從輪次開始算起的秒數，回傳目前該播哪個動作(含它自己的局部進度)；
// 播完整輪回傳 null，呼叫端據此判斷該不該開新的一輪。
function pickActiveAction(theme, order, elapsedSec){
  let t = 0;
  for (const id of order){
    const action = theme.actions[id];
    const d = action.dur ?? 1.2;
    if (elapsedSec < t + d) return { id, localT: elapsedSec - t, action };
    t += d;
  }
  return null;
}

// enter/exit 配對之間的「持有」窗：enter 開始之後、exit 結束之前都算「持有中」(例如墨鏡戴著)。
function pairHoldWindow(theme, order, enterId){
  const exitId = theme.actions[enterId]?.pair;
  let acc = 0, enterStart = null, exitEnd = null;
  for (const id of order){
    const d = theme.actions[id].dur ?? 1.2;
    if (id === enterId && enterStart === null) enterStart = acc;
    if (id === exitId) exitEnd = acc + d;
    acc += d;
  }
  return { enterStart, exitEnd };
}

function isPairHeld(theme, order, enterId, elapsedSec){
  const w = pairHoldWindow(theme, order, enterId);
  if (w.enterStart === null) return false;
  if (elapsedSec < w.enterStart) return false;
  if (w.exitEnd !== null && elapsedSec >= w.exitEnd) return false;
  return true;
}

// mood 優先權：pending/escalate/speak/wake/think 等任何非 idle 的對話狀態一律暫停疊加表演，
// 只留基礎待機 loop——見設計文件 Premise 5（outside voice 補了 speak 這個原本漏掉的狀態）。
function perfShouldRender(mood){
  return mood === 'idle';
}

if (typeof module !== 'undefined' && module.exports){
  module.exports = {
    PERF_ROUND_SEC, ACTIONS, EMOTIONS, PERF_THEMES,
    evalKeyframes, sampleAction, shuffle, buildRoundOrder,
    pickActiveAction, pairHoldWindow, isPairHeld, perfShouldRender,
  };
}
