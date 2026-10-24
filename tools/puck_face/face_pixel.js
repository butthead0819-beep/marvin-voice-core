// face_pixel.js — tools/puck_face/pixel_mockup.html 裡「2. 逐像素版」那段的 Node 版，
// 去掉 DOM/Canvas，供 gen_pixel_golden.js 產生 C++ 要比對的標準答案。
// 規格以 pixel_mockup.html 的 <script> 為準，這裡逐字搬，不准改數值/公式/運算順序。
//
// 與樣機的唯一差異：base/out 是 RGB888（3 bytes/px），樣機是 RGBA8888（4 bytes/px）。

const W = 240, C = 120, R = 108, P2 = Math.PI * 2, HALF = Math.PI / 2;
const IDLE = [104, 158, 58], ERR = [230, 90, 55];
const clamp01 = x => x < 0 ? 0 : (x > 1 ? 1 : x);
// 臉的幾何常數（同 face_render.js）
const phiC = 0.72, dw = 0.27, lamC = 0.15, dhA = 0.26, BROW_CURVE = 0.035, PHI_END = phiC + dw + 0.26;
const RIM_W = Math.max(2, R * 0.035), BROW_W = Math.max(1.5, R * 0.02), LIMB_MIN = 0.15;

const clampH = v => Math.max(-HALF, Math.min(HALF, v));
const projF = (phi, lam) => { phi = clampH(phi); lam = clampH(lam); return [C + R * Math.cos(lam) * Math.sin(phi), C + R * Math.sin(lam)]; };

// 每格先算好的東西（跟像素無關）：三角形邊方程式、漸層中心、陰影位置、重算區域
function prepare(p) {
  const col = p.col;
  const dark = k => [col[0] * k | 0, col[1] * k | 0, col[2] * k | 0];
  const F = {
    col, dark45: dark(0.45), dark1: dark(1), dark42: dark(0.42),
    bright: [Math.min(255, col[0] + 80) | 0, Math.min(255, col[1] + 70) | 0, Math.min(255, col[2] + 70) | 0],
    cr: Math.cos(p.roll), sr: Math.sin(p.roll), eyes: [], yaw: p.yaw,
    browL0: lamC + p.pitch - p.browUp
  };
  const pts = [];   // 未旋轉座標下的可見點，用來算重算區域
  for (const sign of [-1, 1]) {
    const p0 = sign * phiC + p.yaw, lam0 = lamC + p.pitch;
    const A = [p0 + sign * dw, lam0 + 0.05], B = [p0 - sign * dw, lam0], Cc = [p0, lam0 + dhA];
    const V = [A, B, Cc], ctr = [(A[0] + B[0] + Cc[0]) / 3, (A[1] + B[1] + Cc[1]) / 3];
    // 三條邊：單位法向量朝內，e>0 在裡面
    const edges = [[A, B], [B, Cc], [Cc, A]].map(([P, Q]) => {
      let nx = -(Q[1] - P[1]), ny = Q[0] - P[0]; const L = Math.hypot(nx, ny); nx /= L; ny /= L;
      if (nx * (ctr[0] - P[0]) + ny * (ctr[1] - P[1]) < 0) { nx = -nx; ny = -ny; }
      return { nx, ny, c: -(nx * P[0] + ny * P[1]) };
    });
    const PB = projF(B[0], B[1]), PC = projF(Cc[0], Cc[1]);
    const sx = (PB[0] + PC[0]) / 2 + Math.sin(clampH(p.yaw)) * R * 0.5, sy = (PB[1] + PC[1]) / 2;
    // 陰影的 topY：同 Canvas 版，取細分後多邊形的最高點（未旋轉座標）
    let topY = Infinity;
    for (const [P, Q] of [[A, B], [B, Cc], [Cc, A]]) for (let i = 0; i <= 8; i++) { const t = i / 8, q = projF(P[0] + (Q[0] - P[0]) * t, P[1] + (Q[1] - P[1]) * t); if (q[1] < topY) topY = q[1]; pts.push(q); }
    F.eyes.push({ edges, rim: [[A, Cc], [Cc, B]], sx, sy, topY });
  }
  for (let i = 0; i <= 24; i++) { const s = -1 + i / 12; pts.push(projF(p.yaw + s * PHI_END, F.browL0 + BROW_CURVE * s * s)); }
  // 未旋轉座標 → 螢幕：轉 roll
  let x0 = W, y0 = W, x1 = 0, y1 = 0;
  for (const [ux, uy] of pts) {
    const dx = ux - C, dy = uy - C, X = C + F.cr * dx - F.sr * dy, Y = C + F.sr * dx + F.cr * dy;
    if (X < x0) x0 = X; if (X > x1) x1 = X; if (Y < y0) y0 = Y; if (Y > y1) y1 = Y;
  }
  const pad = 4;
  F.rect = { x0: Math.max(0, Math.floor(x0 - pad)), y0: Math.max(0, Math.floor(y0 - pad)), x1: Math.min(W - 1, Math.ceil(x1 + pad)), y1: Math.min(W - 1, Math.ceil(y1 + pad)) };
  return F;
}

// 線段距離：先用當地 Jacobian 把線段和像素都換到螢幕座標，再在螢幕上找最近點
// （靠近球邊緣時經度方向被壓扁，在角度空間找最近點會高估距離、描邊變細）
function segDistPx(phi, lam, P, Q, J) {
  const vx = Q[0] - P[0], vy = Q[1] - P[1], wx = phi - P[0], wy = lam - P[1];
  const Vx = J.a * vx + J.b * vy, Vy = J.d * vy, Wx = J.a * wx + J.b * wy, Wy = J.d * wy;
  const t = clamp01((Wx * Vx + Wy * Vy) / (Vx * Vx + Vy * Vy));
  return Math.hypot(Wx - Vx * t, Wy - Vy * t);
}
const mix = (c, col, a) => { c[0] += (col[0] - c[0]) * a; c[1] += (col[1] - c[1]) * a; c[2] += (col[2] - c[2]) * a; };
function gradColor(F, t, out) {
  t = clamp01(t);
  let a, b, u; if (t < 0.4) { a = F.bright; b = F.dark1; u = t / 0.4; } else { a = F.dark1; b = F.dark42; u = (t - 0.4) / 0.6; }
  out[0] = a[0] + (b[0] - a[0]) * u; out[1] = a[1] + (b[1] - a[1]) * u; out[2] = a[2] + (b[2] - a[2]) * u;
}
const RIM_COL = [8, 10, 9], BROW_COL = [16, 19, 17], BLACK = [0, 0, 0];

function renderPixel(out, base, p) {
  const e = clamp01(p.err), col = IDLE.map((v, i) => v + (ERR[i] - v) * e);
  out.set(base);
  const F = prepare({ ...p, col }), { x0, y0, x1, y1 } = F.rect;
  const c = [0, 0, 0], g = [0, 0, 0], J = { a: 0, b: 0, d: 0 };
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
    // 像素中心 → 反轉 roll → 未旋轉座標
    const dx = x + 0.5 - C, dy = y + 0.5 - C;
    const ux = F.cr * dx + F.sr * dy, uy = -F.sr * dx + F.cr * dy;
    const clip = clamp01(R - 0.5 - Math.hypot(ux, uy) + 0.5);
    if (clip <= 0) continue;
    const sl = uy / R; if (sl <= -1 || sl >= 1) continue;
    const lam = Math.asin(sl), cl = Math.cos(lam), u = ux / (R * cl);
    if (u <= -1 || u >= 1) continue;
    const phi = Math.asin(u), cp = Math.cos(phi), sp = u;
    // 螢幕 → 角度的偏微分（算邊緣平滑用）
    // 球緣 cos→0 時一階近似會失準（遠處的形狀被算成 0 距離，在球緣留下一圈色暈），所以 cos 設下限
    const clg = Math.max(cl, LIMB_MIN), cpg = Math.max(cp, LIMB_MIN);
    const dphi_dx = 1 / (R * clg * cpg), dphi_dy = ux * sl / (R * R * clg * clg * clg * cpg), dlam_dy = 1 / (R * clg);
    // 角度 → 螢幕（算描邊距離用）：dX=a·dφ+b·dλ, dY=d·dλ
    J.a = R * clg * cpg; J.b = -R * sl * sp; J.d = R * clg;
    const i = (y * W + x) * 3;
    c[0] = out[i]; c[1] = out[i + 1]; c[2] = out[i + 2];
    for (const E of F.eyes) {
      let cov = 1;
      for (const e of E.edges) {
        const v = e.nx * phi + e.ny * lam + e.c;
        const gx = e.nx * dphi_dx, gy = e.nx * dphi_dy + e.ny * dlam_dy;
        cov = Math.min(cov, clamp01(0.5 + v / Math.hypot(gx, gy)));
        if (cov <= 0) break;
      }
      if (cov > 0) {
        const k = cov * clip;
        mix(c, F.dark45, 0.98 * k);
        gradColor(F, Math.hypot(ux + C - E.sx, uy + C - E.sy) / (R * 0.46), g); mix(c, g, 0.98 * k);
        const ty = (uy + C - (E.topY - R * 0.01)) / (R * 0.17); mix(c, BLACK, 0.5 * (1 - clamp01(ty)) * k);
      }
      let dmin = Infinity;
      for (const [P, Q] of E.rim) { const d = segDistPx(phi, lam, P, Q, J); if (d < dmin) dmin = d; }
      const rc = clamp01(RIM_W / 2 - dmin + 0.5);
      if (rc > 0) mix(c, RIM_COL, 0.96 * rc * clip);
    }
    // 眉線：角度空間的拋物線 λ = L0 + curve·s²，s=(φ-yaw)/PHI_END
    const s = (phi - F.yaw) / PHI_END;
    let bd;
    if (s < -1 || s > 1) { const se = s < 0 ? -1 : 1, dp = phi - (F.yaw + se * PHI_END), dl = lam - (F.browL0 + BROW_CURVE); bd = Math.hypot(J.a * dp + J.b * dl, J.d * dl); }
    else { const m = 2 * BROW_CURVE * s / PHI_END, dl = lam - (F.browL0 + BROW_CURVE * s * s), k = dl / (1 + m * m); bd = Math.hypot(J.a * (-m * k) + J.b * k, J.d * k); }
    const bc = clamp01(BROW_W / 2 - bd + 0.5);
    if (bc > 0) mix(c, BROW_COL, 0.92 * bc * clip);
    out[i] = c[0] + 0.5 | 0; out[i + 1] = c[1] + 0.5 | 0; out[i + 2] = c[2] + 0.5 | 0;
  }
  return F.rect;
}

module.exports = { renderPixel, W };
