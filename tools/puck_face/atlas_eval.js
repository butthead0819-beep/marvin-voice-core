// atlas_eval.js — 圖集（face_atlas.js）對精確版（face_3d.js render3D）的誤差與大小比較表。
//
// 用法：node tools/puck_face/atlas_eval.js [--char marvin|round] [--W 240,466] [--step 0.2,0.1,0.05]
//
// 每個 W × step × mode 烤一次，再對 interp（nearest / bilinear）各跑 248 個姿勢，
// 比 render3D 回傳的 rect 內每通道差。表格欄位：
//   W step mode interp sprites rawKB deflateKB bakeS max mean bad24_avg bad24_worst recallMs
//   bad24 = 單一像素任一通道差 > 24 的像素數（所有姿勢平均／最壞）；recallMs 是 JS 的平均耗時，只供相對比較。
// 健全性檢查（assert，失敗 exit 1）：姿勢剛好落在眼格點與眉格點上、roll=0、err=0 時，
// recall 跟 render3D 在 rect 內每通道差 ≤ 3（只剩 u8 量化誤差）。

const zlib = require('zlib');
const { render3D, MARVIN, ROUND_DEMO } = require('./face_3d.js');
const { bake, atlasSize, spriteRaw, recall } = require('./face_atlas.js');

const CHARS = { marvin: MARVIN, round: ROUND_DEMO };
const arg = (name, def) => { const i = process.argv.indexOf('--' + name); return i > 0 ? process.argv[i + 1] : def; };
const charName = arg('char', 'marvin');
const Ws = arg('W', '240,466').split(',').map(Number);
const steps = arg('step', '0.2,0.1,0.05').split(',').map(Number);
if (!CHARS[charName] || Ws.some(w => !(w > 0)) || steps.some(s => !(s > 0))) {
  console.error('usage: node atlas_eval.js [--char marvin|round] [--W 240,466] [--step 0.2,0.1,0.05]');
  process.exit(1);
}
const ch = CHARS[charName];

// base 同 gen_3d_golden.js 的 makeBase
function makeBase(W) {
  const base = new Uint8Array(W * W * 3);
  for (let y = 0; y < W; y++) {
    for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 3;
      base[i] = (x * 7 + y * 3) & 255;
      base[i + 1] = (x * y) & 255;
      base[i + 2] = (x + 2 * y) & 255;
    }
  }
  return base;
}

// 姿勢清單：跟 gen_pixel_golden.js、gen_3d_golden.js 同一份清單（三份同一清單，同 seed 12345、同 248 個姿勢），改一邊要改另外兩邊。
function mulberry32(seed) {
  let a = seed;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function buildPoses() {
  const poses = [];
  const YAWS = [-1.25, -0.6, 0, 0.6, 1.25];
  const PITCHES = [-0.45, 0, 0.42];
  const ROLLS = [-0.34, 0, 0.34];
  for (const yaw of YAWS) {
    for (const pitch of PITCHES) {
      for (const roll of ROLLS) {
        poses.push({ yaw, pitch, roll, browUp: 0, err: 0 });
      }
    }
  }
  poses.push({ yaw: 0.3, pitch: -0.2, roll: 0.1, browUp: 0.05, err: 0 });
  poses.push({ yaw: -0.4, pitch: 0.1, roll: -0.2, browUp: 0.02, err: 1 });
  poses.push({ yaw: 0.1, pitch: 0.05, roll: 0, browUp: 0, err: 0.5 });

  const rand = mulberry32(12345);
  const ranges = [
    ['yaw', -1.3, 1.3],
    ['pitch', -0.5, 0.5],
    ['roll', -0.4, 0.4],
    ['browUp', 0, 0.06],
    ['err', 0, 1],
  ];
  for (let n = 0; n < 200; n++) {
    const p = {};
    for (const [key, lo, hi] of ranges) {
      const r = rand();
      p[key] = lo + (hi - lo) * r;
    }
    poses.push(p);
  }
  return poses;
}

// rect 內比較：回傳 { max, sum, n, bad }（sum/n 是每通道差的總和與通道數，bad 是任一通道差 > 24 的像素數）
// skipLimb：跳過球緣那圈（clip<1 的像素），見 sanity 的說明
function compare(a, b, W, rect, skipLimb) {
  const C = W / 2, R = W * 0.45;
  let max = 0, sum = 0, n = 0, bad = 0;
  for (let y = rect.y0; y <= rect.y1; y++) for (let x = rect.x0; x <= rect.x1; x++) {
    if (skipLimb && Math.hypot(x + 0.5 - C, y + 0.5 - C) > R - 1) continue;   // clip = R - r，r > R-1 就是 clip < 1
    const i = (y * W + x) * 3;
    let px = 0;
    for (let k = 0; k < 3; k++) { const d = Math.abs(a[i + k] - b[i + k]); sum += d; n++; if (d > px) px = d; }
    if (px > max) max = px;
    if (px > 24) bad++;
  }
  return { max, sum, n, bad };
}

function deflateKB(atlas) {
  let total = 0;
  for (const sp of atlas.eye.sprites.concat(atlas.brow ? atlas.brow.sprites : []))
    total += zlib.deflateRawSync(spriteRaw(atlas, sp), { level: 9 }).length;
  return total / 1024;
}

// 健全性檢查：眼、眉線都剛好落在格點上（眉線的 L0 靠 browUp 對到格點，眼睛不受 browUp 影響）。
// yaw 取偏離 0 的格點（不然左右鏡像寫錯也看不出來）。
function sanity(atlas, W, base) {
  const A = atlas.eye, e = ch.eye;
  const i = Math.min(A.n[0] - 1, Math.floor(A.n[0] / 2) + Math.floor(A.n[0] / 4)), j = Math.floor(A.n[1] / 2);
  const phiR = A.lo[0] + i * (A.hi[0] - A.lo[0]) / (A.n[0] - 1), lamR = A.lo[1] + j * (A.hi[1] - A.lo[1]) / (A.n[1] - 1);
  const pose = { yaw: phiR - e.cx, pitch: lamR - e.cy, roll: 0, browUp: 0, err: 0 };
  if (atlas.brow) {
    const B = atlas.brow;
    if (B.n[0] !== A.n[0]) throw new Error('眉線與眼睛的 yaw 格點數不同，健全性檢查無法同時落在格點上');
    const jb = Math.floor(B.n[1] / 2), L0 = B.lo[1] + jb * (B.hi[1] - B.lo[1]) / (B.n[1] - 1);
    pose.browUp = ch.brow.lam + pose.pitch - L0;
  }
  const exact = new Uint8Array(W * W * 3), got = new Uint8Array(W * W * 3);
  const rect = render3D(exact, base, W, pose, ch);
  for (const interp of ['nearest', 'bilinear']) {
    recall(got, base, W, pose, ch, atlas, interp);
    // weight 模式的球緣 clip 是近似（c + (c'-c)*clip，精確版是每段疊色的 alpha 都乘 clip），
    // 球緣那圈本來就不是只有量化誤差，健全性檢查跳過；那圈的誤差照樣算進主表。
    const r = compare(exact, got, W, rect, atlas.mode === 'weight');
    if (r.max > 3) {
      console.error(`健全性檢查失敗：W=${W} step=${atlas.step} mode=${atlas.mode} interp=${interp} 格點上的姿勢 max_diff=${r.max} > 3`);
      process.exit(1);
    }
  }
}

const poses = buildPoses();
const rows = [];
for (const W of Ws) {
  const base = makeBase(W);
  const exact = poses.map(p => { const o = new Uint8Array(W * W * 3); const rect = render3D(o, base, W, p, ch); return { o, rect }; });
  const got = new Uint8Array(W * W * 3);
  for (const step of steps) for (const mode of ['mask', 'weight']) {
    const atlas = bake(W, ch, step, mode);
    const bakeS = atlas.bakeMs / 1000;
    console.error(`bake W=${W} step=${step} mode=${mode}: ${bakeS.toFixed(2)} s`);
    sanity(atlas, W, base);
    const { sprites, rawBytes } = atlasSize(atlas), dKB = deflateKB(atlas);
    for (const interp of ['nearest', 'bilinear']) {
      let max = 0, sum = 0, n = 0, badSum = 0, badWorst = 0, ms = 0;
      poses.forEach((p, k) => {
        const t0 = performance.now(); recall(got, base, W, p, ch, atlas, interp); ms += performance.now() - t0;
        const r = compare(exact[k].o, got, W, exact[k].rect);
        if (r.max > max) max = r.max;
        sum += r.sum; n += r.n; badSum += r.bad; if (r.bad > badWorst) badWorst = r.bad;
      });
      rows.push([W, step, mode, interp, sprites, (rawBytes / 1024).toFixed(1), dKB.toFixed(1), bakeS.toFixed(1),
        max, (sum / n).toFixed(3), (badSum / poses.length).toFixed(1), badWorst, (ms / poses.length).toFixed(2)]);
    }
  }
}

const head = ['W', 'step', 'mode', 'interp', 'sprites', 'rawKB', 'deflateKB', 'bakeS', 'max', 'mean', 'bad24_avg', 'bad24_worst', 'recallMs'];
const table = [head, ...rows].map(r => r.map(String));
const widths = head.map((_, c) => Math.max(...table.map(r => r[c].length)));
for (const r of table) console.log(r.map((s, c) => s.padStart(widths[c])).join('  '));
console.log('健全性檢查通過（格點上的姿勢，nearest / bilinear 皆 max_diff ≤ 3）');
