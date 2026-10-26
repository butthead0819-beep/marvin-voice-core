// face_3d.js — tools/puck_face/sphere3d_mockup.html「2b. 3D 內積版」(prepare3D / render3D) 的 Node 版，
// 去掉 DOM/Canvas，是 firmware/car_puck/face_rt3d.h（C++ 移植）的標準答案，
// 由 gen_3d_golden.js 產生 golden 給 test_face_rt3d.cpp 比對。
// 規格以樣機的 <script> 為準，這裡逐字搬，不准改數值/公式/運算順序。
//
// 與樣機的差異（只有這幾項）：
// 1. 角色改成純資料（可序列化，之後要給買家客製）：MARVIN / ROUND_DEMO。樣機裡是 verts()/pts()/center() 函式，
//    這裡改成同一組運算順序的資料欄位，算出來必須是完全相同的 double。
// 2. render3D 自己依 pose.err 算眼睛基色 col（樣機的 frame() 做的事）。
// 3. base/out 是 RGB888（3 bytes/px），樣機是 RGBA8888（4 bytes/px），同 face_pixel.js 的慣例。
// 4. 不需要 px3Count。
//
// 用法：const { render3D, MARVIN, ROUND_DEMO } = require('./face_3d.js');
//       render3D(out, base, W, {yaw,pitch,roll,browUp,err}, MARVIN);   // 開頭 out.set(base)
//
// 角色資料格式：
//   eye.kind==='poly'：頂點 k 的經緯度 = [sign*cx + yaw + sign*verts[k][0], cy + pitch + verts[k][1]]
//     rim：要描邊的邊（頂點索引對）
//   eye.kind==='cap'：中心 = [sign*cx + yaw, cy + pitch]，radius 弧度，rim:true 整圈描邊
//   brow：null 或 {lam, half, curve}：L0 = lam + pitch - browUp，
//     三點 = [[yaw-half, L0+curve], [yaw, L0], [yaw+half, L0+curve]]
//   idle / err：眼睛基色與錯誤色，col = idle + (err-idle)*clamp01(pose.err)

const P2 = Math.PI * 2, HALF = Math.PI / 2;
const clamp01 = x => x < 0 ? 0 : (x > 1 ? 1 : x);
const LIMB_MIN = 0.15;
const clampH = v => Math.max(-HALF, Math.min(HALF, v));

const mix = (c, col, a) => { c[0] += (col[0] - c[0]) * a; c[1] += (col[1] - c[1]) * a; c[2] += (col[2] - c[2]) * a; };
function gradColor(F, t, out) {
  t = clamp01(t);
  let a, b, u; if (t < 0.4) { a = F.bright; b = F.dark1; u = t / 0.4; } else { a = F.dark1; b = F.dark42; u = (t - 0.4) / 0.6; }
  out[0] = a[0] + (b[0] - a[0]) * u; out[1] = a[1] + (b[1] - a[1]) * u; out[2] = a[2] + (b[2] - a[2]) * u;
}
const RIM_COL = [8, 10, 9], BROW_COL = [16, 19, 17], BLACK = [0, 0, 0];

const v3 = (phi, lam) => [Math.cos(lam) * Math.sin(phi), Math.sin(lam), Math.cos(lam) * Math.cos(phi)];
const dot3 = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross3 = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const nrm3 = a => { const L = Math.hypot(a[0], a[1], a[2]); return [a[0] / L, a[1] / L, a[2] / L]; };
const neg3 = a => [-a[0], -a[1], -a[2]];

// 角色資料（純資料，格式見檔頭）
const MARVIN = {
  eye: { kind: 'poly', cx: 0.72, cy: 0.15, verts: [[0.27, 0.05], [-0.27, 0], [0, 0.26]], rim: [[0, 2], [2, 1]] },
  brow: { lam: 0.15, half: 0.72 + 0.27 + 0.26, curve: 0.035 },
  idle: [104, 158, 58], err: [230, 90, 55]
};
// 示範用，不是定案造型
const ROUND_DEMO = {
  eye: { kind: 'cap', cx: 0.40, cy: 0.05, radius: 0.19, rim: true },
  brow: null,
  idle: [104, 158, 58], err: [230, 90, 55]
};

// 每格先算好的東西（跟像素無關）：邊的法向量、描邊線段、眉線平面、漸層中心、陰影位置、重算區域
function prepare3D(p, W, ch) {
  const C = W / 2, R = W * 0.45;
  const projW = (phi, lam) => { phi = clampH(phi); lam = clampH(lam); return [C + R * Math.cos(lam) * Math.sin(phi), C + R * Math.sin(lam)]; };
  const col = p.col;
  const dark = k => [col[0] * k | 0, col[1] * k | 0, col[2] * k | 0];
  const F = {
    col, dark45: dark(0.45), dark1: dark(1), dark42: dark(0.42),
    bright: [Math.min(255, col[0] + 80) | 0, Math.min(255, col[1] + 70) | 0, Math.min(255, col[2] + 70) | 0],
    cr: Math.cos(p.roll), sr: Math.sin(p.roll), eyes: [], brow: null
  };
  const pts = [];   // 未旋轉座標下 z>0 的點，用來算重算區域
  const addPt = (phi, lam) => { const P = v3(phi, lam); if (P[2] > 0) pts.push([C + R * P[0], C + R * P[1]]); };
  for (const sign of [-1, 1]) {
    const e = ch.eye;
    if (e.kind === 'poly') {
      const p0 = sign * e.cx + p.yaw, lam0 = e.cy + p.pitch;
      const V = e.verts.map(v => [p0 + sign * v[0], lam0 + v[1]]), P = V.map(([a, b]) => v3(a, b)), n = V.length;
      const ctr = nrm3(P.reduce((s, q) => [s[0] + q[0], s[1] + q[1], s[2] + q[2]], [0, 0, 0]));
      const edges = P.map((Pk, k) => { const nn = nrm3(cross3(Pk, P[(k + 1) % n])); return dot3(nn, ctr) < 0 ? neg3(nn) : nn; });
      const rims = e.rim.map(([i, j]) => {
        const nn = nrm3(cross3(P[i], P[j]));
        return {
          n: nn, a: cross3(nn, P[i]), b: cross3(P[j], nn),
          xi: R * P[i][0], yi: R * P[i][1], zi: P[i][2], xj: R * P[j][0], yj: R * P[j][1], zj: P[j][2]
        };
      });
      const PB = projW(V[1][0], V[1][1]), PC = projW(V[2][0], V[2][1]);
      const sx = (PB[0] + PC[0]) / 2 + Math.sin(clampH(p.yaw)) * R * 0.5, sy = (PB[1] + PC[1]) / 2;
      let topY = Infinity;
      for (let k = 0; k < n; k++) {
        const A = V[k], B = V[(k + 1) % n];
        for (let i = 0; i <= 8; i++) {
          const t = i / 8, q = projW(A[0] + (B[0] - A[0]) * t, A[1] + (B[1] - A[1]) * t); if (q[1] < topY) topY = q[1];
          addPt(A[0] + (B[0] - A[0]) * t, A[1] + (B[1] - A[1]) * t);
        }
      }
      F.eyes.push({ kind: 'poly', edges, rims, sx, sy, topY });
    } else {
      const cen = [sign * e.cx + p.yaw, e.cy + p.pitch], c = v3(cen[0], cen[1]), cosr = Math.cos(e.radius);
      const pc = projW(cen[0], cen[1]);
      F.eyes.push({ kind: 'cap', c, cosr, rim: e.rim, sx: pc[0] - R * 0.06, sy: pc[1] - R * 0.06, topY: pc[1] - R * Math.sin(e.radius) });
      for (let i = 0; i < 24; i++) { const t = i / 24 * P2; addPt(cen[0] + e.radius * Math.cos(t), cen[1] + e.radius * Math.sin(t)); }
    }
  }
  if (ch.brow) {
    const b = ch.brow, L0 = b.lam + p.pitch - p.browUp;
    const Q = [[p.yaw - b.half, L0 + b.curve], [p.yaw, L0], [p.yaw + b.half, L0 + b.curve]], P = Q.map(([a, c]) => v3(a, c));
    const sub = (u, v) => [u[0] - v[0], u[1] - v[1], u[2] - v[2]];
    const n = nrm3(cross3(sub(P[1], P[0]), sub(P[2], P[0]))), d = -dot3(n, P[0]);
    let m1 = cross3(n, P[0]); if (dot3(m1, P[1]) < 0) m1 = neg3(m1);
    let m3 = cross3(n, P[2]); if (dot3(m3, P[1]) < 0) m3 = neg3(m3);
    F.brow = { n, d, m1, m3, x1: R * P[0][0], y1: R * P[0][1], z1: P[0][2], x3: R * P[2][0], y3: R * P[2][1], z3: P[2][2] };
    for (let s = 0; s < 2; s++) for (let i = 0; i <= 12; i++) { const t = i / 12, A = Q[s], B = Q[s + 1]; addPt(A[0] + (B[0] - A[0]) * t, A[1] + (B[1] - A[1]) * t); }
  }
  let x0 = W, y0 = W, x1 = 0, y1 = 0;
  for (const [ux, uy] of pts) {
    const dx = ux - C, dy = uy - C, X = C + F.cr * dx - F.sr * dy, Y = C + F.sr * dx + F.cr * dy;
    if (X < x0) x0 = X; if (X > x1) x1 = X; if (Y < y0) y0 = Y; if (Y > y1) y1 = Y;
  }
  const pad = 4;
  F.rect = { x0: Math.max(0, Math.floor(x0 - pad)), y0: Math.max(0, Math.floor(y0 - pad)), x1: Math.min(W - 1, Math.ceil(x1 + pad)), y1: Math.min(W - 1, Math.ceil(y1 + pad)) };
  return F;
}

function render3D(out, base, W, pose, ch) {
  out.set(base);
  const C = W / 2, R = W * 0.45, RIM_W = Math.max(2, R * 0.035), BROW_W = Math.max(1.5, R * 0.02);
  const e = clamp01(pose.err), col = ch.idle.map((v, i) => v + (ch.err[i] - v) * e);
  const F = prepare3D(Object.assign({}, pose, { col }), W, ch), { x0, y0, x1, y1 } = F.rect;
  const c = [0, 0, 0], g = [0, 0, 0], p3 = [0, 0, 0];
  let kx = 0, ky = 0;
  // 線性函數 v(p)=n·p 在螢幕上的有號距離（像素）
  const sdist = (n, v) => { const gx = (n[0] - n[2] * kx) / R, gy = (n[1] - n[2] * ky) / R; return v / Math.hypot(gx, gy); };
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
    const dx = x + 0.5 - C, dy = y + 0.5 - C;
    const ux = F.cr * dx + F.sr * dy, uy = -F.sr * dx + F.cr * dy;
    const clip = clamp01(R - 0.5 - Math.hypot(ux, uy) + 0.5);
    if (clip <= 0) continue;
    const nx = ux / R, ny = uy / R, r2 = nx * nx + ny * ny; if (r2 >= 1) continue;
    const nz = Math.sqrt(1 - r2), nzg = Math.max(nz, LIMB_MIN);
    kx = nx / nzg; ky = ny / nzg;
    p3[0] = nx; p3[1] = ny; p3[2] = nz;
    const i = (y * W + x) * 3;
    c[0] = out[i]; c[1] = out[i + 1]; c[2] = out[i + 2];
    for (const E of F.eyes) {
      let cov, dmin = Infinity;
      if (E.kind === 'poly') {
        cov = 1;
        for (const n of E.edges) {
          cov = Math.min(cov, clamp01(0.5 + sdist(n, dot3(n, p3))));
          if (cov <= 0) break;
        }
        for (const m of E.rims) {
          let d;
          if (dot3(m.a, p3) >= 0 && dot3(m.b, p3) >= 0) d = Math.abs(sdist(m.n, dot3(m.n, p3)));
          else d = Math.min(m.zi > 0 ? Math.hypot(ux - m.xi, uy - m.yi) : Infinity, m.zj > 0 ? Math.hypot(ux - m.xj, uy - m.yj) : Infinity);
          if (d < dmin) dmin = d;
        }
      } else {
        const s = sdist(E.c, dot3(E.c, p3) - E.cosr);
        cov = clamp01(0.5 + s);
        if (E.rim) dmin = Math.abs(s);
      }
      if (cov > 0) {
        const k = cov * clip;
        mix(c, F.dark45, 0.98 * k);
        gradColor(F, Math.hypot(ux + C - E.sx, uy + C - E.sy) / (R * 0.46), g); mix(c, g, 0.98 * k);
        const ty = (uy + C - (E.topY - R * 0.01)) / (R * 0.17); mix(c, BLACK, 0.5 * (1 - clamp01(ty)) * k);
      }
      const rc = clamp01(RIM_W / 2 - dmin + 0.5);
      if (rc > 0) mix(c, RIM_COL, 0.96 * rc * clip);
    }
    if (F.brow) {
      const B = F.brow;
      let bd;
      if (dot3(B.m1, p3) >= 0 && dot3(B.m3, p3) >= 0) bd = Math.abs(sdist(B.n, dot3(B.n, p3) + B.d));
      else bd = Math.min(B.z1 > 0 ? Math.hypot(ux - B.x1, uy - B.y1) : Infinity, B.z3 > 0 ? Math.hypot(ux - B.x3, uy - B.y3) : Infinity);
      const bc = clamp01(BROW_W / 2 - bd + 0.5);
      if (bc > 0) mix(c, BROW_COL, 0.92 * bc * clip);
    }
    out[i] = c[0] + 0.5 | 0; out[i + 1] = c[1] + 0.5 | 0; out[i + 2] = c[2] + 0.5 | 0;
  }
  return F.rect;
}

module.exports = { render3D, MARVIN, ROUND_DEMO };
