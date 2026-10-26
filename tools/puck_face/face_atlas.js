// face_atlas.js — 「Mac 預烤圖集、S3 背答案」樣機（Node 與瀏覽器共用）。
// 標準答案是 face_3d.js 的 render3D（每像素現算幾何）；這裡驗證另一種做法：
// Mac 事先把「眼睛、眉線的形狀」依位置切格子烤成小圖（sprite），S3 每格只依姿勢挑小圖、貼到精確位置、疊色，
// 不做幾何數學。要量出：圖集多大、跟精確版差多少（atlas_eval.js 跑表、atlas_mockup.html 看畫面）。
//
// 烤圖用 face_3d.js 的 strokeFields（幾何），疊色用 face_3d.js 匯出的 mix / gradColor（不複製一份）。
//
// 鏡像：眼睛只烤右眼（sign=+1）。左眼在 yaw 時 = 右眼在 -yaw 時左右鏡像
//   （Marvin、圓眼都左右對稱；不對稱的角色要左右各烤一套）。
//
// 兩種 mode（每像素每通道存 u8 = Math.round(v*255)）：
//   'mask'  ：眼小圖存 [cov, rc]、眉線存 [bc]，只存形狀；顏色在 recall（S3）時算，圖集小。
//   'weight'：眼小圖存 [T, sCol, wB, wR]，把 render3D 對這隻眼的疊色鏈展開成
//             c_after = T*c_before + sCol*col + wB*bright + wR*RIM_COL 的係數（見 chainCoeffs）。
//             意義是「Mac 想怎麼畫都行，S3 只背係數」：S3 端不需要知道漸層、陰影怎麼來的，圖集較大。
//             顏色線性化忽略 dark() 的取整、bright 的 255 上限（誤差 <1 級）。眉線仍存 [bc]。
//             小圖外的 weight 係數是恆等（T=1、其餘 0），不是 0。
//
// 座標：未旋轉座標，原點在球心（螢幕像素 (x,y) 中心 = x+0.5-C, y+0.5-C 再依 roll 反轉）。
//   眼睛錨點 = R*v3(phiE, lamE) 的 [x, y]，眉線錨點 = R*v3(yaw, L0) 的 [x, y]；小圖座標都相對錨點。

// 整支包進 IIFE：瀏覽器用 <script src> 載入時，頂層 const 會跟 face_3d.js 的同名常數撞在同一個全域詞法範圍
(function () {
const Face3D = typeof require !== 'undefined' ? require('./face_3d.js') : window.Face3D;
const { prepare3D, strokeFields, gradColor, mix, clamp01, v3, RIM_COL, BROW_COL, BLACK } = Face3D;

const now = typeof performance !== 'undefined' ? () => performance.now() : () => Date.now();
const EYE_CH = { mask: 2, weight: 4 };

// 格子範圍：眼 phiR ∈ [cx-1.3, cx+1.3]、lamR ∈ [cy-0.5, cy+0.5]；眉線 yaw ∈ [-1.3, 1.3]、L0 ∈ [lam-0.56, lam+0.5]
function axis(lo, hi, step) {
  const n = Math.round((hi - lo) / step) + 1;
  return { n, lo, hi };
}
const axisVal = (lo, hi, n, i) => lo + i * (hi - lo) / (n - 1);

// weight 模式：把 render3D 對一隻眼的疊色鏈（k=cov，clip=1）展開成 c' = T*c + sCol*col + wB*bright + wR*RIM_COL。
// o = [T, sCol, wB, wR]，從 T=1、其餘 0 開始。mix(c,K,a) 的對應：
//   所有係數 ×(1-a)，再把 a 加到 K 對應的係數上：
//   dark45 → sCol += a*0.45；dark1 → sCol += a*1；dark42 → sCol += a*0.42；
//   bright → wB += a；BLACK → 不加；RIM_COL → wR += a。
// 漸層 g = A + (B-A)*u（A、B 是 bright/dark1/dark42 之一），mix(c,g,α) 拆成
//   「所有係數 ×(1-α)，A 的係數 += α(1-u)，B 的係數 += α*u」。
// E 是烤圖姿勢（roll=0）的 F.eyes[1]，漸層中心用 E.sx/E.sy、陰影用 E.topY。
function scaleAll(o, a) { const m = 1 - a; o[0] *= m; o[1] *= m; o[2] *= m; o[3] *= m; }
function chainCoeffs(cov, rc, ux, uy, E, C, R, o) {
  o[0] = 1; o[1] = 0; o[2] = 0; o[3] = 0;
  if (cov > 0) {
    const k = cov;
    let a = 0.98 * k;
    scaleAll(o, a); o[1] += a * 0.45;                                // mix(c, dark45, 0.98k)
    const t = clamp01(Math.hypot(ux + C - E.sx, uy + C - E.sy) / (R * 0.46));
    if (t < 0.4) { const u = t / 0.4; scaleAll(o, a); o[2] += a * (1 - u); o[1] += a * u; }                      // bright → dark1
    else { const u = (t - 0.4) / 0.6; scaleAll(o, a); o[1] += a * (1 - u); o[1] += a * u * 0.42; }              // dark1 → dark42
    const ty = (uy + C - (E.topY - R * 0.01)) / (R * 0.17);
    a = 0.5 * (1 - clamp01(ty)) * k; scaleAll(o, a);                 // mix(c, BLACK, …)：只衰減
  }
  if (rc > 0) { const a = 0.96 * rc; scaleAll(o, a); o[3] += a; }    // mix(c, RIM_COL, 0.96rc)
}

// 這個像素的 u8 值是不是「空」（mask：全 0；weight：恆等 T=255、其餘 0）
function isEmpty(d, p, nch, weight) {
  if (weight) return d[p] === 255 && d[p + 1] === 0 && d[p + 2] === 0 && d[p + 3] === 0;
  for (let k = 0; k < nch; k++) if (d[p + k] !== 0) return false;
  return true;
}

// 掃一個烤圖姿勢的 rect，kind 'eye' 取眼 1（右眼）的 cov/rc，'brow' 取 bc；存成裁到外框的小圖
function bakeSprite(W, ch, pose, anchor, kind, mode) {
  const C = W / 2, R = W * 0.45;
  const F = prepare3D(Object.assign({}, pose, { col: ch.idle }), W, ch), r = F.rect;
  const nch = kind === 'eye' ? EYE_CH[mode] : 1, weight = kind === 'eye' && mode === 'weight';
  const rw = r.x1 - r.x0 + 1, rh = r.y1 - r.y0 + 1;
  const tmp = new Uint8Array(rw * rh * nch);
  const f = { cov: [0, 0], rc: [0, 0], bc: 0 }, o = [1, 0, 0, 0];
  let bx0 = Infinity, by0 = Infinity, bx1 = -1, by1 = -1;
  for (let y = r.y0; y <= r.y1; y++) for (let x = r.x0; x <= r.x1; x++) {
    const dx = x + 0.5 - C, dy = y + 0.5 - C;
    const ux = F.cr * dx + F.sr * dy, uy = -F.sr * dx + F.cr * dy;
    const p = ((y - r.y0) * rw + (x - r.x0)) * nch;
    if (weight) { tmp[p] = 255; }                                   // 外框內沒覆蓋的像素也是恆等
    if (!strokeFields(F, W, ux, uy, f)) continue;
    let hit;
    if (kind === 'brow') { hit = f.bc > 0; if (hit) tmp[p] = Math.round(f.bc * 255); }
    else {
      const cov = f.cov[1], rc = f.rc[1];
      hit = cov > 0 || rc > 0;
      if (hit) {
        if (mode === 'mask') { tmp[p] = Math.round(cov * 255); tmp[p + 1] = Math.round(rc * 255); }
        else {
          chainCoeffs(cov, rc, ux, uy, F.eyes[1], C, R, o);
          for (let k = 0; k < 4; k++) tmp[p + k] = Math.round(o[k] * 255);
        }
      }
    }
    if (hit) { if (x < bx0) bx0 = x; if (x > bx1) bx1 = x; if (y < by0) by0 = y; if (y > by1) by1 = y; }
  }
  if (bx1 < 0) return { x0: 0, y0: 0, w: 0, h: 0, ax: anchor[0], ay: anchor[1], nch, data: new Uint8Array(0) };
  const w = bx1 - bx0 + 1, h = by1 - by0 + 1, data = new Uint8Array(w * h * nch);
  for (let j = 0; j < h; j++) {
    const s = ((by0 - r.y0 + j) * rw + (bx0 - r.x0)) * nch;
    data.set(tmp.subarray(s, s + w * nch), j * w * nch);
  }
  return { x0: bx0, y0: by0, w, h, ax: anchor[0], ay: anchor[1], nch, data };
}

// 分批烤圖器：step(n) 烤 n 個格點，回傳已完成數；全部烤完 atlas.bakeMs 才有值。bake() 與 atlas_mockup.html 共用
function createBaker(W, ch, step, mode) {
  const R = W * 0.45, e = ch.eye;
  const ea = [axis(e.cx - 1.3, e.cx + 1.3, step), axis(e.cy - 0.5, e.cy + 0.5, step)];
  const ba = ch.brow ? [axis(-1.3, 1.3, step), axis(ch.brow.lam - 0.56, ch.brow.lam + 0.5, step)] : null;
  const pack = a => ({ n: [a[0].n, a[1].n], lo: [a[0].lo, a[1].lo], hi: [a[0].hi, a[1].hi], sprites: [] });
  const atlas = { W, ch, step, mode, eye: pack(ea), brow: ba ? pack(ba) : null, bakeMs: 0 };
  const nEye = ea[0].n * ea[1].n, total = nEye + (ba ? ba[0].n * ba[1].n : 0);
  let done = 0;
  const bakeOne = () => {
    if (done < nEye) {
      const i = Math.floor(done / ea[1].n), j = done % ea[1].n;
      const phiR = axisVal(ea[0].lo, ea[0].hi, ea[0].n, i), lamR = axisVal(ea[1].lo, ea[1].hi, ea[1].n, j);
      const P = v3(phiR, lamR);
      const pose = { yaw: phiR - e.cx, pitch: lamR - e.cy, roll: 0, browUp: 0, err: 0 };
      atlas.eye.sprites.push(bakeSprite(W, ch, pose, [R * P[0], R * P[1]], 'eye', mode));
    } else {
      const k = done - nEye, i = Math.floor(k / ba[1].n), j = k % ba[1].n;
      const yaw = axisVal(ba[0].lo, ba[0].hi, ba[0].n, i), L0 = axisVal(ba[1].lo, ba[1].hi, ba[1].n, j);
      const P = v3(yaw, L0);
      const pose = { yaw, pitch: L0 - ch.brow.lam, roll: 0, browUp: 0, err: 0 };
      atlas.brow.sprites.push(bakeSprite(W, ch, pose, [R * P[0], R * P[1]], 'brow', mode));
    }
    done++;
  };
  return {
    atlas, total,
    step(n) { const t0 = now(); for (let k = 0; k < n && done < total; k++) bakeOne(); atlas.bakeMs += now() - t0; return done; }
  };
}

function bake(W, ch, step, mode, onProgress) {
  const b = createBaker(W, ch, step, mode);
  while (b.step(1) < b.total) if (onProgress) onProgress(b.step(0), b.total);
  if (onProgress) onProgress(b.total, b.total);
  return b.atlas;
}

// 每列只存第一個到最後一個非零像素那段：回傳 [起, 止)（像素索引），整列空回傳 null
function rowSpan(sp, weight, y) {
  const nch = sp.nch, base = y * sp.w * nch;
  let a = -1, b = -1;
  for (let x = 0; x < sp.w; x++) {
    if (!isEmpty(sp.data, base + x * nch, nch, weight)) { if (a < 0) a = x; b = x + 1; }
  }
  return a < 0 ? null : [a, b];
}
const isWeight = (atlas, sp) => atlas.mode === 'weight' && sp.nch === 4;

// 每張小圖 raw = 16（標頭：x0,y0,w,h 各 u16、ax,ay 各 f32）＋ Σ每列（4 bytes 起訖 ＋ 跨距像素數 × 通道數）
function atlasSize(atlas) {
  let sprites = 0, rawBytes = 0;
  const all = atlas.eye.sprites.concat(atlas.brow ? atlas.brow.sprites : []);
  for (const sp of all) {
    sprites++; rawBytes += 16;
    const wt = isWeight(atlas, sp);
    for (let y = 0; y < sp.h; y++) {
      const s = rowSpan(sp, wt, y);
      rawBytes += 4 + (s ? (s[1] - s[0]) * sp.nch : 0);
    }
  }
  return { sprites, rawBytes };
}

// 一張小圖的 raw 位元組（跟 atlasSize 同格式），給 atlas_eval.js 算 deflate
function spriteRaw(atlas, sp) {
  const wt = isWeight(atlas, sp), rows = [];
  let len = 16;
  for (let y = 0; y < sp.h; y++) { const s = rowSpan(sp, wt, y); rows.push(s); len += 4 + (s ? (s[1] - s[0]) * sp.nch : 0); }
  const buf = new Uint8Array(len), dv = new DataView(buf.buffer);
  dv.setUint16(0, sp.x0, true); dv.setUint16(2, sp.y0, true); dv.setUint16(4, sp.w, true); dv.setUint16(6, sp.h, true);
  dv.setFloat32(8, sp.ax, true); dv.setFloat32(12, sp.ay, true);
  let p = 16;
  for (let y = 0; y < sp.h; y++) {
    const s = rows[y];
    dv.setUint16(p, s ? s[0] : 0, true); dv.setUint16(p + 2, s ? s[1] : 0, true); p += 4;
    if (s) { const n = (s[1] - s[0]) * sp.nch; buf.set(sp.data.subarray((y * sp.w + s[0]) * sp.nch, (y * sp.w + s[0]) * sp.nch + n), p); p += n; }
  }
  return buf;
}

// 一個座標落在哪些格點、各佔多少權重：回傳 [{sp, w}]；超出範圍 clamp 到範圍內
function gridPlan(grid, a, b, interp) {
  const g = [0, 1].map(k => {
    const v = k === 0 ? a : b, lo = grid.lo[k], hi = grid.hi[k], n = grid.n[k];
    return Math.max(0, Math.min(n - 1, (v - lo) / (hi - lo) * (n - 1)));
  });
  const n1 = grid.n[1], S = grid.sprites;
  if (interp === 'nearest') return [{ sp: S[Math.round(g[0]) * n1 + Math.round(g[1])], w: 1 }];
  const i0 = Math.min(Math.floor(g[0]), grid.n[0] - 2), j0 = Math.min(Math.floor(g[1]), n1 - 2);
  const fi = g[0] - i0, fj = g[1] - j0, plan = [];
  for (let di = 0; di < 2; di++) for (let dj = 0; dj < 2; dj++) {
    const w = (di ? fi : 1 - fi) * (dj ? fj : 1 - fj);
    if (w > 0) plan.push({ sp: S[(i0 + di) * n1 + (j0 + dj)], w });
  }
  return plan;
}

// 在小圖的 (fx, fy)（小圖 texel 座標，texel 中心在整數）雙線性取樣，乘 w 累加進 v；小圖外用 bg
function sampleAdd(sp, fx, fy, w, v, bg) {
  const nch = sp.nch, x0 = Math.floor(fx), y0 = Math.floor(fy), tx = fx - x0, ty = fy - y0;
  for (let dj = 0; dj < 2; dj++) for (let di = 0; di < 2; di++) {
    const ww = w * (di ? tx : 1 - tx) * (dj ? ty : 1 - ty);
    if (ww === 0) continue;
    const x = x0 + di, y = y0 + dj, inside = x >= 0 && x < sp.w && y >= 0 && y < sp.h, p = (y * sp.w + x) * nch;
    for (let k = 0; k < nch; k++) v[k] += ww * (inside ? sp.data[p + k] / 255 : bg[k]);
  }
}

const BG_ZERO = [0, 0, 0, 0], BG_IDENT = [1, 0, 0, 0];

function recall(out, base, W, pose, ch, atlas, interp) {
  out.set(base);
  const C = W / 2, R = W * 0.45, e = ch.eye, weight = atlas.mode === 'weight';
  const ec = clamp01(pose.err), col = ch.idle.map((v, i) => v + (ch.err[i] - v) * ec);
  const F = prepare3D(Object.assign({}, pose, { col }), W, ch), { x0, y0, x1, y1 } = F.rect;
  const A = atlas.eye;
  // 每隻眼每格一次：錨點（clamp 後）、要取的小圖與權重
  const eyePlans = [-1, 1].map(s => {
    const phiR = Math.max(A.lo[0], Math.min(A.hi[0], e.cx + s * pose.yaw)), lamR = Math.max(A.lo[1], Math.min(A.hi[1], e.cy + pose.pitch));
    const P = v3(phiR, lamR);
    return { s, ax: s * R * P[0], ay: R * P[1], plan: gridPlan(A, phiR, lamR, interp) };
  });
  let browPlan = null;
  if (F.brow && atlas.brow) {
    const B = atlas.brow;
    const yaw = Math.max(B.lo[0], Math.min(B.hi[0], pose.yaw)), L0 = Math.max(B.lo[1], Math.min(B.hi[1], ch.brow.lam + pose.pitch - pose.browUp));
    const P = v3(yaw, L0);
    browPlan = { ax: R * P[0], ay: R * P[1], plan: gridPlan(B, yaw, L0, interp) };
  }
  const bgE = weight ? BG_IDENT : BG_ZERO;
  const c = [0, 0, 0], g = [0, 0, 0], v = [0, 0, 0, 0], vb = [0];
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
    const dx = x + 0.5 - C, dy = y + 0.5 - C;
    const ux = F.cr * dx + F.sr * dy, uy = -F.sr * dx + F.cr * dy;
    const clip = clamp01(R - 0.5 - Math.hypot(ux, uy) + 0.5);
    if (clip <= 0) continue;
    const nx = ux / R, ny = uy / R; if (nx * nx + ny * ny >= 1) continue;
    const i = (y * W + x) * 3;
    c[0] = out[i]; c[1] = out[i + 1]; c[2] = out[i + 2];
    for (let idx = 0; idx < 2; idx++) {
      const P = eyePlans[idx], E = F.eyes[idx];
      const lx = P.s * (ux - P.ax), ly = uy - P.ay;
      v[0] = v[1] = v[2] = v[3] = 0;
      for (const q of P.plan) sampleAdd(q.sp, lx + q.sp.ax + C - 0.5 - q.sp.x0, ly + q.sp.ay + C - 0.5 - q.sp.y0, q.w, v, bgE);
      if (weight) {
        const cp0 = v[0] * c[0] + v[1] * col[0] + v[2] * F.bright[0] + v[3] * RIM_COL[0];
        const cp1 = v[0] * c[1] + v[1] * col[1] + v[2] * F.bright[1] + v[3] * RIM_COL[1];
        const cp2 = v[0] * c[2] + v[1] * col[2] + v[2] * F.bright[2] + v[3] * RIM_COL[2];
        c[0] += (cp0 - c[0]) * clip; c[1] += (cp1 - c[1]) * clip; c[2] += (cp2 - c[2]) * clip;
      } else {
        const cov = v[0], rc = v[1];
        if (cov > 0) {
          const k = cov * clip;
          mix(c, F.dark45, 0.98 * k);
          gradColor(F, Math.hypot(ux + C - E.sx, uy + C - E.sy) / (R * 0.46), g); mix(c, g, 0.98 * k);
          const ty = (uy + C - (E.topY - R * 0.01)) / (R * 0.17); mix(c, BLACK, 0.5 * (1 - clamp01(ty)) * k);
        }
        if (rc > 0) mix(c, RIM_COL, 0.96 * rc * clip);
      }
    }
    if (browPlan) {
      vb[0] = 0;
      const lx = ux - browPlan.ax, ly = uy - browPlan.ay;
      for (const q of browPlan.plan) sampleAdd(q.sp, lx + q.sp.ax + C - 0.5 - q.sp.x0, ly + q.sp.ay + C - 0.5 - q.sp.y0, q.w, vb, BG_ZERO);
      if (vb[0] > 0) mix(c, BROW_COL, 0.92 * vb[0] * clip);
    }
    out[i] = c[0] + 0.5 | 0; out[i + 1] = c[1] + 0.5 | 0; out[i + 2] = c[2] + 0.5 | 0;
  }
  return F.rect;
}

const API = { bake, createBaker, atlasSize, spriteRaw, recall, chainCoeffs };
if (typeof module !== 'undefined') module.exports = API; else window.FaceAtlas = API;
})();
