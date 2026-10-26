// helpers.js — puck 頭像服裝底圖的共用畫筆（在 headless Chrome 裡跑，見 build.py）。
//
// 全域常數 W=240, C=120, R=108, P2 由 build 頁面提供，跟 face_pixel.js 一致。
// 投影也跟 face_pixel.js 的 projF 一致：x = C + R·cosλ·sinφ、y = C + R·sinλ（λ 往下為正），
// 所以底圖上的花紋跟眼睛在同一個球面座標裡。3D 法向量 n = [x, y, z]：x 右、y 下、z 朝觀者。
//
// 底圖只是一張靜態 RGB888，眼睛眉線每格疊在上面；服裝不會跟著轉頭（使用者 10/25 接受）。
// 球半徑 108、螢幕圓半徑 120：超出球的東西（頭髮、光環、土星環）最多只有 12px。

const H = {};

H.clamp01 = v => v < 0 ? 0 : (v > 1 ? 1 : v);
H.mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
H.hex = s => [parseInt(s.slice(1, 3), 16), parseInt(s.slice(3, 5), 16), parseInt(s.slice(5, 7), 16)];
H.norm = v => { const l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; };
H.dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

// 確定性雜湊雜訊（同輸入同輸出，底圖每次產生都一樣）
H.hash3 = (x, y, z) => { const s = Math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453; return s - Math.floor(s); };
H.noise3 = (x, y, z) => {   // 三線性內插的 value noise，0..1
  const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z), xf = x - xi, yf = y - yi, zf = z - zi;
  const s = t => t * t * (3 - 2 * t), u = s(xf), v = s(yf), w = s(zf);
  const h = (a, b, c) => H.hash3(xi + a, yi + b, zi + c);
  const l = (a, b, t) => a + (b - a) * t;
  return l(l(l(h(0, 0, 0), h(1, 0, 0), u), l(h(0, 1, 0), h(1, 1, 0), u), v),
           l(l(h(0, 0, 1), h(1, 0, 1), u), l(h(0, 1, 1), h(1, 1, 1), u), v), w);
};
H.fbm = (n, f, oct = 4) => { let a = 0, amp = 0.5, k = f; for (let i = 0; i < oct; i++) { a += amp * H.noise3(n[0] * k, n[1] * k, n[2] * k); amp *= 0.5; k *= 2; } return a; };

// 繞 x 軸（點頭方向）再繞 y 軸（轉頭方向）旋轉，讓球的花紋擺個好看的角度
H.rot = (n, ax, ay) => {
  const ca = Math.cos(ax), sa = Math.sin(ax), cb = Math.cos(ay), sb = Math.sin(ay);
  const y1 = n[1] * ca - n[2] * sa, z1 = n[1] * sa + n[2] * ca;
  return [n[0] * cb + z1 * sb, y1, -n[0] * sb + z1 * cb];
};

H.bg = r => { r.setTransform(1, 0, 0, 1, 0, 0); r.globalAlpha = 1; r.fillStyle = '#000'; r.fillRect(0, 0, W, W); };
H.proj = (phi, lam) => [C + R * Math.cos(lam) * Math.sin(phi), C + R * Math.sin(lam)];

// 逐像素畫球：fn(n, phi, lam, x, y) 回傳未打光的 [r,g,b]，或 {c:[r,g,b], emit:true, spec:倍率}。
// 打光：環境光 + Lambert + Blinn 高光 + 球緣變暗。光源預設左上前（跟原本 drawSphere 的高光位置一致）。
H.paintSphere = (r, fn, o = {}) => {
  const ambient = o.ambient ?? 0.42, diffuse = o.diffuse ?? 0.7, spec = o.spec ?? 0.15,
        shin = o.shininess ?? 24, limb = o.limb ?? 0.35, L = H.norm(o.light || [-0.42, -0.55, 0.72]);
  const Hv = H.norm([L[0], L[1], L[2] + 1]);
  const img = r.getImageData(0, 0, W, W), d = img.data;
  for (let y = 0; y < W; y++) for (let x = 0; x < W; x++) {
    const dx = x + 0.5 - C, dy = y + 0.5 - C, dist = Math.hypot(dx, dy);
    const cov = H.clamp01(R - dist + 0.5);
    if (cov <= 0) continue;
    const nx = Math.min(1, Math.max(-1, dx / R)), ny = Math.min(1, Math.max(-1, dy / R));
    const nz = Math.sqrt(Math.max(0, 1 - nx * nx - ny * ny)), n = [nx, ny, nz];
    const lam = Math.asin(ny), cl = Math.cos(lam);
    const phi = Math.asin(Math.min(1, Math.max(-1, cl > 1e-6 ? nx / cl : 0)));
    let res = fn(n, phi, lam, x, y);
    if (res == null) continue;
    let col = res, emit = false, sp = 1;
    if (!Array.isArray(res)) { col = res.c; emit = !!res.emit; sp = res.spec ?? 1; }
    let out;
    if (emit) out = col;
    else {
      const lam1 = Math.max(0, H.dot(n, L));
      const shade = (ambient + diffuse * lam1) * (1 - limb * Math.pow(1 - nz, 2));
      const hi = 255 * spec * sp * Math.pow(Math.max(0, H.dot(n, Hv)), shin);
      out = [col[0] * shade + hi, col[1] * shade + hi, col[2] * shade + hi];
    }
    const i = (y * W + x) * 4;
    for (let k = 0; k < 3; k++) d[i + k] = d[i + k] * (1 - cov) + Math.min(255, Math.max(0, out[k])) * cov;
  }
  r.putImageData(img, 0, 0);
};

// 只在球內畫 2D 東西
H.inSphere = (r, f) => { r.save(); r.beginPath(); r.arc(C, C, R, 0, P2); r.clip(); f(r); r.restore(); };

// 玻璃/亮面高光（跟原本 drawSphere 的熱點同位置）
H.gloss = (r, a = 0.55, size = 0.5) => H.inSphere(r, r => {
  const g = r.createRadialGradient(C - R * 0.33, C - R * 0.42, 0, C - R * 0.33, C - R * 0.42, R * size);
  g.addColorStop(0, `rgba(255,255,255,${a})`); g.addColorStop(1, 'rgba(255,255,255,0)');
  r.fillStyle = g; r.fillRect(C - R, C - R, 2 * R, 2 * R);
});
H.rimLine = (r, col = 'rgba(255,255,255,.35)') => { r.strokeStyle = col; r.lineWidth = 1; r.beginPath(); r.arc(C, C, R, 0, P2); r.stroke(); };

// 貼圖取樣器：img 是已載入的 Image；回傳 (u,v)→[r,g,b]，u 水平環繞、v 夾邊
H.sampler = img => {
  const cv = document.createElement('canvas'); cv.width = img.width; cv.height = img.height;
  const x = cv.getContext('2d'); x.drawImage(img, 0, 0);
  const d = x.getImageData(0, 0, cv.width, cv.height).data, w = cv.width, h = cv.height;
  return (u, v) => {
    u = u - Math.floor(u); v = Math.min(0.9999, Math.max(0, v));
    const fx = u * w - 0.5, fy = v * h - 0.5, x0 = Math.floor(fx), y0 = Math.floor(fy), tx = fx - x0, ty = fy - y0;
    const g = (xx, yy) => { xx = ((xx % w) + w) % w; yy = Math.min(h - 1, Math.max(0, yy)); const i = (yy * w + xx) * 4; return [d[i], d[i + 1], d[i + 2], d[i + 3]]; };
    const a = g(x0, y0), b = g(x0 + 1, y0), c = g(x0, y0 + 1), e = g(x0 + 1, y0 + 1);
    return [0, 1, 2, 3].map(k => (a[k] * (1 - tx) + b[k] * tx) * (1 - ty) + (c[k] * (1 - tx) + e[k] * tx) * ty);
  };
};

// 經緯度貼圖（equirectangular）包到球上：lon0 決定正面朝哪個經度
H.wrapTexture = (r, img, o = {}) => {
  const s = H.sampler(img), lon0 = o.lon0 ?? 0, tilt = o.tilt ?? 0;
  H.paintSphere(r, (n) => {
    const m = H.rot(n, tilt, 0);
    const lon = Math.atan2(m[0], m[2]) + lon0, lat = -Math.asin(Math.max(-1, Math.min(1, m[1])));
    const c = s(lon / P2 + 0.5, 0.5 - lat / Math.PI);
    return o.emit ? { c: c.slice(0, 3), emit: true } : c.slice(0, 3);
  }, o);
};

// 平面圖案（國旗、水晶球場景）畫在離屏 canvas，再依球面角度包上去：
// 正面 phi∈[-spanX, spanX]、lam∈[-spanY, spanY] 對到整張圖，超出的部分夾邊延伸。
H.wrapFlat = (r, drawFn, o = {}) => {
  const cw = o.w || 600, ch = o.h || 400, cv = document.createElement('canvas'); cv.width = cw; cv.height = ch;
  drawFn(cv.getContext('2d'), cw, ch);
  const s = H.sampler(cv), sx = o.spanX ?? 1.35, sy = o.spanY ?? 0.9;
  H.paintSphere(r, (n, phi, lam) => s(Math.min(0.9999, Math.max(0, phi / (2 * sx) + 0.5)), lam / (2 * sy) + 0.5).slice(0, 3), o);
};

// 球面上一小塊平面貼紙（8 號球的數字）：center 是方向向量，size 是角半徑
H.decal = (n, center, size, sampler) => {
  const c = H.norm(center), up = Math.abs(c[1]) > 0.9 ? [1, 0, 0] : [0, -1, 0];
  const e1 = H.norm([up[1] * c[2] - up[2] * c[1], up[2] * c[0] - up[0] * c[2], up[0] * c[1] - up[1] * c[0]]);
  const e2 = [c[1] * e1[2] - c[2] * e1[1], c[2] * e1[0] - c[0] * e1[2], c[0] * e1[1] - c[1] * e1[0]];
  const ang = Math.acos(Math.max(-1, Math.min(1, H.dot(n, c))));
  if (ang > size) return null;
  const u = H.dot(n, e1) / (2 * Math.sin(size)) + 0.5, v = -H.dot(n, e2) / (2 * Math.sin(size)) + 0.5;
  return { ang, rgba: sampler(Math.min(0.999, Math.max(0, u)), v) };
};

// 球外的毛邊/光暈只能畫到螢幕圓（半徑 120）裡
H.inScreen = (r, f) => { r.save(); r.beginPath(); r.arc(C, C, 119.5, 0, P2); r.clip(); f(r); r.restore(); };

// 雪花水晶球的水球折射：內容（raw 整張底圖，球半徑 R＝場景單位 1）是放在水裡、球心後方
// z=depth 的平面場景。正交視線打到半徑 ri·R 的水球表面，依 ior 折射後落到場景平面取色——
// 中央放大、越靠球緣壓縮越強（魚眼），這是雪花球的視覺特徵。回傳只有水球範圍的 canvas。
H.globeLens = (raw, ri, depth, ior, scale = 1) => {
  const src = raw.getContext('2d').getImageData(0, 0, W, W).data;
  const out = document.createElement('canvas'); out.width = out.height = W;
  const ox = out.getContext('2d'), img = ox.createImageData(W, W), d = img.data;
  const Rin = R * ri, eta = 1 / ior;
  const sample = (fx, fy, k) => {   // 雙線性、夾邊
    fx = Math.min(W - 1.001, Math.max(0, fx)); fy = Math.min(W - 1.001, Math.max(0, fy));
    const x0 = Math.floor(fx), y0 = Math.floor(fy), tx = fx - x0, ty = fy - y0, i = (y0 * W + x0) * 4 + k;
    return (src[i] * (1 - tx) + src[i + 4] * tx) * (1 - ty) + (src[i + W * 4] * (1 - tx) + src[i + W * 4 + 4] * tx) * ty;
  };
  for (let y = 0; y < W; y++) for (let x = 0; x < W; x++) {
    const nx = (x + 0.5 - C) / Rin, ny = (y + 0.5 - C) / Rin, rr = nx * nx + ny * ny;
    if (rr > 1.02) continue;
    const nz = Math.sqrt(Math.max(0, 1 - rr)), k = 1 - eta * eta * (1 - nz * nz);
    const a = eta * nz - Math.sqrt(Math.max(0, k));
    const tx = a * nx, ty = a * ny, tz = -eta + a * nz;          // 折射方向（視線 = (0,0,-1)）
    const s = (depth - nz) / tz;                                   // 走到 z=depth 的場景平面
    const qx = (nx + s * tx) * ri / scale, qy = (ny + s * ty) * ri / scale;   // 換回素材座標（素材半徑 = scale）
    const i = (y * W + x) * 4, qr = Math.hypot(qx, qy);
    for (let c = 0; c < 4; c++) d[i + c] = sample(C + qx * R - 0.5, C + qy * R - 0.5, c);
    if (scale < 1) d[i + 3] *= H.clamp01((1 - qr) * R * scale / 1.5 + 0.5);   // 素材外是水（透明）
  }
  ox.putImageData(img, 0, 0);
  return out;
};

// 雪花水晶球（3D 版）：鏡頭從上方 tilt 弧度俯視；水球裡有一片水平雪地（世界 y = ground，
// 球心往下、y 向下為正），擺設 sprite 是直立在雪地上、通過球心的立牌（世界 z = 0 平面）。
// 每道視線打到水球表面依 ior 折射，取「立牌／雪地」較近的命中；都沒中＝水（淡藍半透明）。
// sprite：透明背景 canvas，baseY＝擺設底部在 sprite 上的 y，unit＝sprite 幾 px 等於球半徑 1。
H.globe3d = (sprite, o = {}) => {
  const ri = o.ri ?? 0.97, a = o.tilt ?? (35 / 180 * Math.PI), g = o.ground ?? 0.33, eta = 1 / (o.ior ?? 1.33);
  const baseY = o.baseY ?? (C + 66), unit = o.unit ?? 150;
  const sp = sprite.getContext('2d').getImageData(0, 0, W, W).data;
  const spAt = (px, py) => {   // 最近鄰取樣，範圍外透明
    const x = Math.round(px), y = Math.round(py);
    if (x < 0 || y < 0 || x >= W || y >= W) return null;
    const i = (y * W + x) * 4; return sp[i + 3] > 8 ? [sp[i], sp[i + 1], sp[i + 2], sp[i + 3] / 255] : null;
  };
  const out = document.createElement('canvas'); out.width = out.height = W;
  const ox = out.getContext('2d'), img = ox.createImageData(W, W), d = img.data;
  const Rin = R * ri, ca = Math.cos(a), sa = Math.sin(a), dir = [0, sa, -ca];
  const water = o.water || [140, 185, 230, 0.6];
  for (let y = 0; y < W; y++) for (let x = 0; x < W; x++) {
    const sx = (x + 0.5 - C) / Rin, sy = (y + 0.5 - C) / Rin, rr = sx * sx + sy * sy;
    if (rr > 1.02) continue;
    const sz = Math.sqrt(Math.max(0, 1 - rr));
    const P = [sx, sy * ca - sz * sa, sy * sa + sz * ca];               // 水球表面點（世界座標）＝法向量
    const cosi = -H.dot(P, dir), k = 1 - eta * eta * (1 - cosi * cosi), m = eta * cosi - Math.sqrt(Math.max(0, k));
    const t = [eta * dir[0] + m * P[0], eta * dir[1] + m * P[1], eta * dir[2] + m * P[2]];
    let col = null, alpha = 0;
    // 雪地
    let ground = null;
    if (t[1] > 1e-6) {
      const s2 = (g - P[1]) / t[1], hx = P[0] + s2 * t[0], hz = P[2] + s2 * t[2];
      if (s2 > 0 && hx * hx + hz * hz <= 1 - g * g) {
        const shadow = 1 - 0.28 * Math.exp(-((hx / 0.32) ** 2 + (hz / 0.14) ** 2));
        const edge = 1 - 0.35 * Math.pow((hx * hx + hz * hz) / (1 - g * g), 2);   // 雪地邊緣壓暗，看得出地平面
        const v = 248 * shadow * edge, sn = (H.hash3(Math.round(hx * 90), Math.round(hz * 90), 1) - 0.5) * 8;
        ground = { s: s2, c: [v - 6 + sn, v - 2 + sn, v + 2 + sn] };
      }
    }
    // 立牌
    let bb = null;
    if (t[2] < -1e-6) {
      const s1 = -P[2] / t[2], hx = P[0] + s1 * t[0], hy = P[1] + s1 * t[1];
      if (s1 > 0 && hy < g) { const c = spAt(C + hx * unit, baseY + (hy - g) * unit); if (c) bb = { s: s1, c }; }
    }
    if (bb && (!ground || bb.s < ground.s)) {
      const behind = ground ? ground.c : water.slice(0, 3), ba = ground ? 1 : water[3];
      col = [0, 1, 2].map(q => bb.c[q] * bb.c[3] + behind[q] * (1 - bb.c[3])); alpha = bb.c[3] + ba * (1 - bb.c[3]);
    } else if (ground) { col = ground.c; alpha = 1; }
    else { col = water.slice(0, 3); alpha = water[3]; }
    const i = (y * W + x) * 4;
    d[i] = col[0]; d[i + 1] = col[1]; d[i + 2] = col[2]; d[i + 3] = 255 * alpha;
  }
  ox.putImageData(img, 0, 0);
  return out;
};

// 黏土質感：把擺設 sprite（透明背景）變成手捏黏土——用模糊後的不透明度當高度圖，
// 算法線打光（左上光、柔和高光），再疊低頻凹凸（手指壓痕）。LLM 畫得再扁平也會變圓鼓鼓。
H.clay = (sprite, o = {}) => {
  const rad = o.radius ?? 5, mk = () => { const c = document.createElement('canvas'); c.width = c.height = W; return c; };
  const hm = mk(), hx = hm.getContext('2d'); hx.filter = `blur(${rad}px)`; hx.drawImage(sprite, 0, 0);
  const h = hx.getImageData(0, 0, W, W).data, src = sprite.getContext('2d').getImageData(0, 0, W, W);
  const d = src.data, L = H.norm([-0.5, -0.6, 0.62]), Hv = H.norm([L[0], L[1], L[2] + 1]);
  const A = (x, y) => h[(Math.min(W - 1, Math.max(0, y)) * W + Math.min(W - 1, Math.max(0, x))) * 4 + 3] / 255;
  for (let y = 0; y < W; y++) for (let x = 0; x < W; x++) {
    const i = (y * W + x) * 4; if (d[i + 3] < 8) continue;
    const bump = (H.noise3(x * 0.09, y * 0.09, 3) - 0.5) * 0.35;
    const gx = (A(x + 1, y) - A(x - 1, y)) * 6 + bump, gy = (A(x, y + 1) - A(x, y - 1)) * 6 + bump * 0.6;
    const n = H.norm([-gx, -gy, 1]);
    const sh = 0.62 + 0.5 * Math.max(0, H.dot(n, L)), sp = 70 * Math.pow(Math.max(0, H.dot(n, Hv)), 18);
    for (let k = 0; k < 3; k++) d[i + k] = Math.min(255, d[i + k] * sh + sp);
  }
  const out = mk(); out.getContext('2d').putImageData(src, 0, 0); return out;
};

// 水裡漂浮的雪花（確定性亂數，畫在折射之後、模糊之前）
H.snow = (g, ri, n) => {
  for (let k = 0; k < n; k++) {
    const a = H.hash3(k, 11, 3) * P2, rr = Math.sqrt(H.hash3(k, 12, 4)) * R * ri * 0.95;
    g.fillStyle = `rgba(255,255,255,${0.6 + 0.4 * H.hash3(k, 13, 5)})`;
    g.beginPath(); g.arc(C + rr * Math.cos(a), C + rr * Math.sin(a), 0.8 + 1.6 * H.hash3(k, 14, 6), 0, P2); g.fill();
  }
};

// 磨砂玻璃彈珠：外殼＝原版白球的打光（第一層立體感、眼睛畫在殼上永遠看得清楚），
// 服裝縮成內球（ri×R）、模糊後透過磨砂玻璃看到。
// o.frost：玻璃霧度 0..1（越大內球越淡）；o.ri：內球半徑比例；o.blur：模糊 px；o.feather：內球邊緣柔化 px；
// o.depth：有給就改成雪花水晶球折射（場景平面 z，負值＝球心後方）；o.ior 折射率；o.snow 雪花數；
// o.scale：素材在水裡的大小（1＝填滿，物件類建議 0.6，周圍是乳白的水）。
// o.base：外殼打光用的畫法（預設原版 drawDefault 的白球）。
H.marble = (r, innerDraw, o = {}) => {
  const frost = o.frost ?? 0.45, ri = o.ri ?? 0.8, blur = o.blur ?? 2.5, feather = o.feather ?? 3;
  const mk = () => { const c = document.createElement('canvas'); c.width = c.height = W; return c; };
  // 1. 內球：照原樣畫完整服裝，再縮到 ri、模糊
  const raw = mk(), rr = raw.getContext('2d'); if (!o.transparent) H.bg(rr); innerDraw(rr);   // transparent：擺設背景是水
  const inner = mk(), ir = inner.getContext('2d');
  ir.filter = `blur(${blur}px)`;
  ir.save(); ir.beginPath(); ir.arc(C, C, R * ri + 1, 0, P2); ir.clip();
  if (o.tilt != null) {   // 雪花水晶球 3D 版：俯視、擺設站在雪地上（見 H.globe3d）
    const lensed = H.globe3d(o.clay ? H.clay(raw) : raw, o);
    if (o.snow) H.snow(lensed.getContext('2d'), ri, o.snow);
    ir.drawImage(lensed, 0, 0);
  } else if (o.depth != null) {   // 雪花水晶球：內容當水裡的場景，透過水球折射看到（見 H.globeLens）
    const lensed = H.globeLens(raw, ri, o.depth, o.ior ?? 1.33, o.scale ?? 1);
    if (o.snow) H.snow(lensed.getContext('2d'), ri, o.snow);
    ir.drawImage(lensed, 0, 0);
  } else ir.drawImage(raw, C - C * ri, C - C * ri, W * ri, W * ri);
  ir.restore();
  const id = ir.getImageData(0, 0, W, W).data;
  // 2. 外殼：原版白球
  const shell = mk(), sr = shell.getContext('2d'); H.bg(sr); (o.base || drawDefault)(sr);
  const sd = sr.getImageData(0, 0, W, W).data;
  // 3. 合成：內球透過磨砂（越靠球緣 Fresnel 越白、越看不到內球），加一點霧面顆粒
  const img = r.getImageData(0, 0, W, W), d = img.data;
  for (let y = 0; y < W; y++) for (let x = 0; x < W; x++) {
    const i = (y * W + x) * 4, dx = x + 0.5 - C, dy = y + 0.5 - C, dist = Math.hypot(dx, dy);
    if (dist > R + 0.5) { d[i] = sd[i]; d[i + 1] = sd[i + 1]; d[i + 2] = sd[i + 2]; continue; }
    const nz = Math.sqrt(Math.max(0, 1 - (dist / R) ** 2)), fres = Math.pow(1 - nz, 2.2);
    const m = H.clamp01((R * ri - dist) / feather + 0.5);   // 內球遮罩（feather＝邊緣柔化寬度 px）
    const t = m * (id[i + 3] / 255) * (1 - frost) * (1 - 0.85 * fres);
    const g = (H.hash3(x, y, 5) - 0.5) * 6;
    for (let k = 0; k < 3; k++) d[i + k] = Math.min(255, Math.max(0, sd[i + k] * (1 - t) + id[i + k] * t + g));
  }
  r.putImageData(img, 0, 0);
  H.gloss(r, 0.35, 0.6);   // 磨砂的高光是散開的
};
