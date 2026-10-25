// costumes.js — puck 頭像服裝底圖清單。每套：id、分類、標題、draw(r)、建議眼睛色 eye。
//
// draw(r) 進來時畫布已經是全黑（H.bg）；畫完的整張 240×240 就是底圖。
// 規則（見 .claude/skills/puck-costume/SKILL.md）：
// - 球半徑 108，超出球的東西只能畫到螢幕圓（119.5）裡
// - 眼睛色要跟底圖色相對比，而且不能偏紅橘（錯誤狀態是紅眼 [230,90,55]）
// - 不做受版權保護的角色、不做真人肖像；做「原型」（吸血鬼、瘋狂科學家）
const COSTUMES = [];
const add = (id, cat, title, draw, eye) => COSTUMES.push({ id, cat, title, draw, eye });

const GREEN = [104, 158, 58], YEL = [250, 210, 60], CYAN = [60, 200, 215], BLUE = [80, 140, 235];

// ───────────────────────── 原版 ─────────────────────────
function drawDefault(r) {
  const cx = C, cy = C;
  r.setTransform(1, 0, 0, 1, 0, 0); r.globalAlpha = 1; r.lineCap = 'butt'; r.lineJoin = 'miter'; r.fillStyle = '#000'; r.fillRect(0, 0, W, W);
  const sph = r.createRadialGradient(cx - R * 0.34, cy - R * 0.42, R * 0.05, cx, cy, R * 1.07);
  sph.addColorStop(0, '#ffffff'); sph.addColorStop(0.3, '#eef2f4'); sph.addColorStop(0.66, '#cfd6dc'); sph.addColorStop(0.9, '#b6c0c8'); sph.addColorStop(1, '#8b959d');
  r.fillStyle = sph; r.beginPath(); r.arc(cx, cy, R, 0, P2); r.fill();
  r.save(); r.beginPath(); r.arc(cx, cy, R, 0, P2); r.clip();
  const hot = r.createRadialGradient(cx - R * 0.33, cy - R * 0.42, 0, cx - R * 0.33, cy - R * 0.42, R * 0.5);
  hot.addColorStop(0, 'rgba(255,255,255,0.9)'); hot.addColorStop(1, 'rgba(255,255,255,0)');
  r.fillStyle = hot; r.fillRect(cx - R, cy - R, 2 * R, 2 * R);
  r.restore();
  r.strokeStyle = 'rgba(255,255,255,.4)'; r.lineWidth = 1; r.beginPath(); r.arc(cx, cy, R, 0, P2); r.stroke();
}
add('default', '原版', '原版 Marvin', drawDefault, GREEN);

// ───────────────────────── 球類 ─────────────────────────
add('basketball', '球類', '籃球', r => {
  H.paintSphere(r, n => {
    const m = H.rot(n, 0.35, 0.5), w = 0.02;
    if (Math.abs(m[0]) < w || Math.abs(m[1]) < w || Math.abs(Math.hypot(m[1], m[2]) - 0.7) < w) return [28, 20, 16];
    const k = 0.88 + 0.16 * H.noise3(m[0] * 45, m[1] * 45, m[2] * 45);
    return [213 * k, 98 * k, 28 * k];
  }, { spec: 0.12 });
}, GREEN);

const PHI_G = (1 + Math.sqrt(5)) / 2;
const ICO = [[0, 1, PHI_G], [0, -1, PHI_G], [0, 1, -PHI_G], [0, -1, -PHI_G], [1, PHI_G, 0], [-1, PHI_G, 0], [1, -PHI_G, 0], [-1, -PHI_G, 0],
             [PHI_G, 0, 1], [-PHI_G, 0, 1], [PHI_G, 0, -1], [-PHI_G, 0, -1]].map(H.norm);
const DOD = [];
for (const a of [-1, 1]) for (const b of [-1, 1]) {
  for (const c of [-1, 1]) DOD.push(H.norm([a, b, c]));
  DOD.push(H.norm([0, a * PHI_G, b / PHI_G]), H.norm([a / PHI_G, 0, b * PHI_G]), H.norm([a * PHI_G, b / PHI_G, 0]));   // 二十面體各面中心（對偶十二面體頂點）
}
add('soccer', '球類', '足球', r => {
  H.paintSphere(r, n => {
    const m = H.rot(n, 0.3, 0.25);
    let best = -9, second = -9, pent = false;
    for (const c of ICO) { const d = H.dot(m, c) * 0.9741; if (d > best) { second = best; best = d; pent = true; } else if (d > second) second = d; }
    for (const c of DOD) { const d = H.dot(m, c); if (d > best) { second = best; best = d; pent = false; } else if (d > second) second = d; }
    if (best - second < 0.007) return [38, 38, 42];
    return pent ? { c: [24, 24, 28], spec: 0.3 } : [244, 244, 240];
  }, { spec: 0.25 });
  H.gloss(r, 0.3);
}, GREEN);

// 網球/棒球的縫線：球面上的封閉曲線
const SEAM = [];
for (let i = 0; i < 480; i++) {
  const t = i / 480 * P2, a = 0.68, b = 0.32;
  SEAM.push(H.norm([a * Math.cos(t) + b * Math.cos(3 * t), a * Math.sin(t) - b * Math.sin(3 * t), 2 * Math.sqrt(a * b) * Math.sin(2 * t)]));
}
const seamNear = m => { let best = -2, bi = 0; for (let i = 0; i < SEAM.length; i++) { const d = H.dot(m, SEAM[i]); if (d > best) { best = d; bi = i; } } return [Math.acos(Math.min(1, best)), bi]; };

add('tennis', '球類', '網球', r => {
  H.paintSphere(r, n => {
    const m = H.rot(n, 0.5, 0.4), [ang] = seamNear(m);
    if (ang < 0.035) return [238, 238, 222];
    const k = 0.85 + 0.25 * H.noise3(m[0] * 70, m[1] * 70, m[2] * 70);
    return [205 * k, 222 * k, 62 * k];
  }, { spec: 0.04, ambient: 0.5 });
}, BLUE);

add('baseball', '球類', '棒球', r => {
  H.paintSphere(r, n => {
    const m = H.rot(n, 0.5, 0.4), [ang, bi] = seamNear(m);
    if (ang < 0.01) return [160, 150, 140];
    if (ang > 0.022 && ang < 0.06 && Math.sin(bi / SEAM.length * P2 * 60) > 0.15) return [200, 36, 42];
    const k = 0.95 + 0.06 * H.noise3(m[0] * 30, m[1] * 30, m[2] * 30);
    return [242 * k, 238 * k, 228 * k];
  }, { spec: 0.15 });
}, GREEN);

add('billiard8', '球類', '撞球 8 號', r => {
  const pc = document.createElement('canvas'); pc.width = pc.height = 128;
  const p = pc.getContext('2d'); p.fillStyle = '#fff'; p.fillRect(0, 0, 128, 128);
  p.fillStyle = '#111'; p.font = 'bold 92px Helvetica, Arial, sans-serif'; p.textAlign = 'center'; p.textBaseline = 'middle'; p.fillText('8', 64, 70);
  const s = H.sampler(pc), center = [0.42, -0.42, 0.8], size = 0.42;
  H.paintSphere(r, n => {
    const dc = H.decal(n, center, size, s);
    if (dc && dc.ang < size * 0.95) return dc.rgba.slice(0, 3);
    return [16, 16, 20];
  }, { spec: 0.9, shininess: 70, ambient: 0.5 });
  H.gloss(r, 0.45, 0.35);
}, GREEN);

const FIB = [];
for (let i = 0; i < 420; i++) { const y = 1 - (i + 0.5) / 420 * 2, rr = Math.sqrt(1 - y * y), t = i * Math.PI * (3 - Math.sqrt(5)); FIB.push([Math.cos(t) * rr, y, Math.sin(t) * rr]); }
add('golf', '球類', '高爾夫球', r => {
  H.paintSphere(r, n => {
    let best = -2; for (const c of FIB) { const d = H.dot(n, c); if (d > best) best = d; }
    const ang = Math.acos(Math.min(1, best)), rd = 0.075;
    const k = ang < rd ? 0.8 + 0.2 * (ang / rd) ** 2 : 1;
    return [246 * k, 246 * k, 242 * k];
  }, { spec: 0.2 });
}, GREEN);

add('disco', '球類', '迪斯可球', r => {
  const L = H.norm([-0.42, -0.55, 0.72]), Hv = H.norm([L[0], L[1], L[2] + 1]);
  H.paintSphere(r, n => {
    const m = H.rot(n, 0.2, 0.3), lat = Math.asin(m[1]), lon = Math.atan2(m[0], m[2]), dl = 0.105;
    const i = Math.floor(lat / dl), cl = Math.max(0.15, Math.cos((i + 0.5) * dl)), j = Math.floor(lon * cl / dl);
    const fy = lat / dl - i, fx = lon * cl / dl - j;
    if (fx < 0.12 || fy < 0.12) return { c: [30, 30, 36], emit: true };
    const h = H.hash3(i, j, 7), tn = H.rot([Math.cos(i * dl) * Math.sin(j * dl / cl), Math.sin(i * dl), Math.cos(i * dl) * Math.cos(j * dl / cl)], -0.2, -0.3);
    const shine = Math.pow(Math.max(0, H.dot(H.norm(tn), Hv)), 40);
    let g = 70 + 150 * h * (0.4 + 0.6 * Math.max(0, H.dot(n, L)));
    if (h > 0.94) return { c: [[255, 120, 200], [120, 220, 255], [255, 220, 120]][(((i + j) % 3) + 3) % 3], emit: true };
    g = Math.min(255, g + 255 * shine);
    return { c: [g, g, g + 8], emit: true };
  });
  r.fillStyle = 'rgba(255,255,255,0.9)';
  for (const [x, y, s] of [[82, 70, 9], [150, 96, 6], [70, 150, 5], [176, 160, 7]]) {
    r.beginPath(); r.moveTo(x - s, y); r.lineTo(x, y - 1.2); r.lineTo(x + s, y); r.lineTo(x, y + 1.2); r.closePath(); r.fill();
    r.beginPath(); r.moveTo(x, y - s); r.lineTo(x + 1.2, y); r.lineTo(x, y + s); r.lineTo(x - 1.2, y); r.closePath(); r.fill();
  }
}, GREEN);

// ───────────────────────── 國旗（印在球上） ─────────────────────────
add('flag_tw', '國旗', '台灣', r => {
  H.wrapFlat(r, (g, w, h) => {
    g.fillStyle = '#fe0000'; g.fillRect(0, 0, w, h);
    g.fillStyle = '#000095'; g.fillRect(0, 0, w / 2, h / 2);
    const cx = w / 4, cy = h / 4, ro = h * 0.19, ri = h * 0.105;
    g.fillStyle = '#fff'; g.beginPath();
    for (let k = 0; k < 24; k++) { const a = -Math.PI / 2 + k * Math.PI / 12, rr = k % 2 ? ri : ro; g.lineTo(cx + rr * Math.cos(a), cy + rr * Math.sin(a)); }
    g.fill();
    g.fillStyle = '#000095'; g.beginPath(); g.arc(cx, cy, h * 0.1, 0, P2); g.fill();
    g.fillStyle = '#fff'; g.beginPath(); g.arc(cx, cy, h * 0.088, 0, P2); g.fill();
  }, { spec: 0.12 });
}, GREEN);

add('flag_jp', '國旗', '日本', r => {
  H.wrapFlat(r, (g, w, h) => {
    g.fillStyle = '#fff'; g.fillRect(0, 0, w, h);
    g.fillStyle = '#bc002d'; g.beginPath(); g.arc(w / 2, h / 2, h * 0.3, 0, P2); g.fill();
  }, { spec: 0.12 });
}, GREEN);

function star5(g, x, y, ro) {
  g.beginPath();
  for (let k = 0; k < 10; k++) { const a = -Math.PI / 2 + k * Math.PI / 5, rr = k % 2 ? ro * 0.382 : ro; g.lineTo(x + rr * Math.cos(a), y + rr * Math.sin(a)); }
  g.closePath(); g.fill();
}
add('flag_us', '國旗', '美國', r => {
  H.wrapFlat(r, (g, w, h) => {
    for (let k = 0; k < 13; k++) { g.fillStyle = k % 2 ? '#fff' : '#b22234'; g.fillRect(0, k * h / 13, w, h / 13 + 1); }
    const cw = w * 0.4, ch = h * 7 / 13; g.fillStyle = '#3c3b6e'; g.fillRect(0, 0, cw, ch);
    g.fillStyle = '#fff';
    for (let row = 0; row < 9; row++) for (let col = 0; col < (row % 2 ? 5 : 6); col++)
      star5(g, cw / 12 * (col * 2 + 1 + (row % 2)), ch / 10 * (row + 1), h * 0.026);
  }, { spec: 0.12 });
}, GREEN);

add('flag_uk', '國旗', '英國', r => {
  H.wrapFlat(r, (g, w, h) => {
    g.fillStyle = '#012169'; g.fillRect(0, 0, w, h);
    g.lineCap = 'butt';
    g.strokeStyle = '#fff'; g.lineWidth = h * 0.2;
    g.beginPath(); g.moveTo(0, 0); g.lineTo(w, h); g.moveTo(w, 0); g.lineTo(0, h); g.stroke();
    g.strokeStyle = '#c8102e'; g.lineWidth = h * 0.067;
    g.beginPath(); g.moveTo(0, 0); g.lineTo(w, h); g.moveTo(w, 0); g.lineTo(0, h); g.stroke();
    g.fillStyle = '#fff'; g.fillRect(w / 2 - h / 6, 0, h / 3, h); g.fillRect(0, h / 2 - h / 6, w, h / 3);
    g.fillStyle = '#c8102e'; g.fillRect(w / 2 - h / 10, 0, h / 5, h); g.fillRect(0, h / 2 - h / 10, w, h / 5);
  }, { spec: 0.12 });
}, YEL);

// ───────────────────────── 人物原型（Marvin 戴假髮/變裝） ─────────────────────────
function puff(r, x, y, rad, light, dark) {
  const g = r.createRadialGradient(x - rad * 0.35, y - rad * 0.4, rad * 0.1, x, y, rad);
  g.addColorStop(0, light); g.addColorStop(1, dark); r.fillStyle = g; r.beginPath(); r.arc(x, y, rad, 0, P2); r.fill();
}

add('dracula', '人物', '吸血鬼', r => {
  H.paintSphere(r, () => [214, 222, 206], { ambient: 0.48 });
  H.gloss(r, 0.35);
  // 油頭＋美人尖
  H.inSphere(r, r => {
    const g = r.createLinearGradient(0, C - R, 0, C - 10);
    g.addColorStop(0, '#2a2a33'); g.addColorStop(1, '#09090c');
    r.fillStyle = g; r.beginPath();
    r.moveTo(C - R - 2, C - 2); r.lineTo(C - R - 2, C - R - 2); r.lineTo(C + R + 2, C - R - 2); r.lineTo(C + R + 2, C - 2);
    r.quadraticCurveTo(C + R * 0.82, C - R * 0.5, C + R * 0.45, C - R * 0.52);
    r.quadraticCurveTo(C + R * 0.15, C - R * 0.5, C, C - R * 0.2);
    r.quadraticCurveTo(C - R * 0.15, C - R * 0.5, C - R * 0.45, C - R * 0.52);
    r.quadraticCurveTo(C - R * 0.82, C - R * 0.5, C - R - 2, C - 2);
    r.fill();
    r.strokeStyle = 'rgba(160,170,200,0.35)'; r.lineWidth = 2;
    for (const k of [-0.55, -0.25, 0.25, 0.55]) { r.beginPath(); r.moveTo(C + R * k * 0.4, C - R * 0.95); r.quadraticCurveTo(C + R * k, C - R * 0.8, C + R * k * 1.3, C - R * 0.6); r.stroke(); }
  });
  // 獠牙
  r.fillStyle = '#fbfbf6'; r.strokeStyle = 'rgba(0,0,0,0.35)'; r.lineWidth = 1;
  for (const s of [-1, 1]) { r.beginPath(); r.moveTo(C + s * 6, C + 52); r.lineTo(C + s * 15, C + 52); r.lineTo(C + s * 11, C + 66); r.closePath(); r.fill(); r.stroke(); }
  // 立領披風（黑面紅裡，從下方兩側翻上來）
  H.inScreen(r, r => {
    for (const s of [-1, 1]) {
      r.fillStyle = '#0b0b0e'; r.beginPath(); r.moveTo(C + s * 125, C + 5); r.lineTo(C + s * 40, C + 125); r.lineTo(C + s * 125, C + 125); r.closePath(); r.fill();
      r.fillStyle = '#8f0d16'; r.beginPath(); r.moveTo(C + s * 118, C + 22); r.lineTo(C + s * 56, C + 125); r.lineTo(C + s * 70, C + 125); r.lineTo(C + s * 121, C + 40); r.closePath(); r.fill();
    }
  });
}, YEL);

add('mad_scientist', '人物', '瘋狂科學家', r => {
  drawDefault(r);
  H.inScreen(r, r => {
    // 炸開的白髮：從頭頂與兩側髮根往外亂翹的髮絲，部分伸出球外
    r.lineCap = 'round';
    for (let k = 0; k < 260; k++) {
      const a = Math.PI * (0.88 + 1.24 * H.hash3(k, 1, 2)), root = R * (0.62 + 0.3 * H.hash3(k, 3, 4));
      const x0 = C + root * Math.cos(a), y0 = C + root * Math.sin(a) * 0.9;
      if (y0 > C - 8) continue;
      const len = 26 + 30 * H.hash3(k, 5, 6), wob = (H.hash3(k, 7, 8) - 0.5) * 1.4;
      const x1 = x0 + len * Math.cos(a + wob), y1 = y0 + len * Math.sin(a + wob);
      const g = 200 + 55 * H.hash3(k, 9, 1);
      r.strokeStyle = `rgba(${g},${g},${g + 4},0.9)`; r.lineWidth = 1.2 + 1.6 * H.hash3(k, 2, 7);
      r.beginPath(); r.moveTo(x0, y0); r.quadraticCurveTo((x0 + x1) / 2 + wob * 14, (y0 + y1) / 2 - 8, x1, y1); r.stroke();
      r.strokeStyle = 'rgba(90,95,105,0.35)'; r.lineWidth = 0.8;
      r.beginPath(); r.moveTo(x0 + 1, y0 + 1); r.quadraticCurveTo((x0 + x1) / 2 + wob * 14 + 1, (y0 + y1) / 2 - 7, x1 + 1, y1 + 1); r.stroke();
    }
  });
  // 掃帚鬍
  r.lineCap = 'round';
  for (let k = 0; k < 70; k++) {
    const t = (k / 69) * 2 - 1, x0 = C + t * 24, y0 = C + 54 + 4 * t * t;
    const g = 205 + 50 * H.hash3(k, 4, 4);
    r.strokeStyle = `rgba(${g},${g},${g},0.95)`; r.lineWidth = 1.6;
    r.beginPath(); r.moveTo(x0, y0); r.lineTo(x0 + t * 10 + (H.hash3(k, 6, 6) - 0.5) * 4, y0 + 12 + 6 * H.hash3(k, 5, 5)); r.stroke();
  }
}, GREEN);

add('pompadour', '人物', '飛機頭', r => {
  drawDefault(r);
  H.inScreen(r, r => {
    const g = r.createLinearGradient(C - R, 0, C + R, C - R);
    g.addColorStop(0, '#0c0c10'); g.addColorStop(0.5, '#2d2d38'); g.addColorStop(1, '#0c0c10');
    r.fillStyle = g; r.beginPath();
    r.moveTo(C - R - 4, C + 10); r.lineTo(C - R - 4, C - 30);
    r.quadraticCurveTo(C - R * 0.9, C - R * 1.05, C - R * 0.1, C - R * 1.08);
    r.quadraticCurveTo(C + R * 0.8, C - R * 1.12, C + R * 0.62, C - R * 0.62);
    r.quadraticCurveTo(C + R * 0.3, C - R * 0.45, C - R * 0.2, C - R * 0.5);
    r.quadraticCurveTo(C - R * 0.7, C - R * 0.45, C - R * 0.86, C + 10);
    r.closePath(); r.fill();
    r.beginPath(); r.moveTo(C + R + 4, C + 10); r.lineTo(C + R + 4, C - 40); r.quadraticCurveTo(C + R * 0.8, C - R * 0.55, C + R * 0.86, C + 10); r.closePath(); r.fill();
    r.strokeStyle = 'rgba(200,210,255,0.4)'; r.lineWidth = 2;
    for (const k of [0, 1, 2]) { r.beginPath(); r.moveTo(C - R * 0.75, C - R * (0.6 + k * 0.12)); r.quadraticCurveTo(C - R * 0.1, C - R * (1.02 - k * 0.06), C + R * (0.5 - k * 0.08), C - R * (0.85 - k * 0.1)); r.stroke(); }
  });
}, GREEN);

// ───────────────────────── 地區（水晶球/魚缸） ─────────────────────────
add('fishbowl', '地區', '海洋魚缸', r => {
  H.inSphere(r, r => {
    const air = r.createLinearGradient(0, C - R, 0, C - 40);
    air.addColorStop(0, '#c9dfe8'); air.addColorStop(1, '#a9cbd9'); r.fillStyle = air; r.fillRect(0, 0, W, W);
    const water = r.createLinearGradient(0, C - 45, 0, C + R);
    water.addColorStop(0, '#3fa7d6'); water.addColorStop(1, '#0c3c6e'); r.fillStyle = water; r.fillRect(0, C - 45, W, W);
    r.strokeStyle = 'rgba(255,255,255,0.5)'; r.lineWidth = 2; r.beginPath(); r.ellipse(C, C - 45, R, 7, 0, 0, P2); r.stroke();
    for (let k = 0; k < 40; k++) { r.fillStyle = `rgba(255,255,255,${0.05 + 0.08 * H.hash3(k, 1, 1)})`; r.beginPath(); r.ellipse(C - R + 2 * R * H.hash3(k, 2, 2), C - 30 + 120 * H.hash3(k, 3, 3), 10, 3, 0.3, 0, P2); r.fill(); }
    for (let k = 0; k < 60; k++) { const x = C - R + 2 * R * H.hash3(k, 4, 4); r.fillStyle = ['#c9a36b', '#8d7a5a', '#e2d2b0', '#6b5b45'][k % 4]; r.beginPath(); r.arc(x, C + 80 + 22 * H.hash3(k, 5, 5), 5, 0, P2); r.fill(); }
    r.strokeStyle = '#2f8f4e'; r.lineWidth = 5; r.lineCap = 'round';
    for (const x of [C - 70, C - 58, C + 62]) { r.beginPath(); r.moveTo(x, C + 95); r.bezierCurveTo(x - 12, C + 60, x + 14, C + 40, x - 4, C + 10); r.stroke(); }
    const fish = (x, y, s, dir) => {
      r.fillStyle = '#ff8a1e'; r.beginPath(); r.ellipse(x, y, 14 * s, 8 * s, 0, 0, P2); r.fill();
      r.beginPath(); r.moveTo(x - dir * 12 * s, y); r.lineTo(x - dir * 24 * s, y - 9 * s); r.lineTo(x - dir * 24 * s, y + 9 * s); r.closePath(); r.fill();
      r.fillStyle = '#111'; r.beginPath(); r.arc(x + dir * 7 * s, y - 2 * s, 1.8 * s, 0, P2); r.fill();
    };
    fish(C - 55, C - 5, 1.1, 1); fish(C + 50, C + 60, 0.9, -1);
    r.strokeStyle = 'rgba(255,255,255,0.7)'; r.lineWidth = 1.2;
    for (const [x, y, s] of [[C + 30, C - 10, 4], [C + 36, C - 24, 3], [C + 33, C - 35, 2.5], [C - 30, C + 30, 3]]) { r.beginPath(); r.arc(x, y, s, 0, P2); r.stroke(); }
  });
  H.gloss(r, 0.5, 0.45); H.rimLine(r, 'rgba(255,255,255,.55)');
}, YEL);

add('snowglobe_mountain', '地區', '高山水晶球', r => {
  H.inSphere(r, r => {
    const sky = r.createLinearGradient(0, C - R, 0, C + 20);
    sky.addColorStop(0, '#5d8fd6'); sky.addColorStop(1, '#cfe3f5'); r.fillStyle = sky; r.fillRect(0, 0, W, W);
    const ridge = (pts, col, snow) => {
      r.fillStyle = col; r.beginPath(); r.moveTo(0, W); for (const [x, y] of pts) r.lineTo(x, y); r.lineTo(W, W); r.closePath(); r.fill();
      if (snow) { r.fillStyle = '#f4f8fc'; for (let i = 1; i < pts.length - 1; i++) { const [x, y] = pts[i]; if (y < pts[i - 1][1] && y < pts[i + 1][1]) { r.beginPath(); r.moveTo(x, y); r.lineTo(x - 14, y + 18); r.lineTo(x - 5, y + 14); r.lineTo(x + 2, y + 20); r.lineTo(x + 13, y + 17); r.closePath(); r.fill(); } } }
    };
    ridge([[0, 120], [40, 75], [75, 105], [120, 52], [165, 98], [200, 70], [240, 110]], '#7d8fa8', true);
    ridge([[0, 160], [50, 130], [100, 150], [150, 120], [200, 145], [240, 135]], '#56708a', true);
    r.fillStyle = '#eef4fa'; r.fillRect(0, 185, W, 60);
    r.fillStyle = '#1f4d3a';
    for (const [x, y, s] of [[45, 190, 1.2], [70, 196, 0.9], [175, 192, 1.1], [198, 198, 0.8]]) { for (let k = 0; k < 3; k++) { r.beginPath(); r.moveTo(x, y - 40 * s + k * 10 * s); r.lineTo(x - (10 + k * 4) * s, y - 20 * s + k * 10 * s); r.lineTo(x + (10 + k * 4) * s, y - 20 * s + k * 10 * s); r.closePath(); r.fill(); } }
    r.fillStyle = 'rgba(255,255,255,0.85)';
    for (let k = 0; k < 70; k++) { r.beginPath(); r.arc(C - R + 2 * R * H.hash3(k, 7, 1), C - R + 2 * R * H.hash3(k, 8, 1), 1 + 1.4 * H.hash3(k, 9, 1), 0, P2); r.fill(); }
  });
  H.gloss(r, 0.5, 0.45); H.rimLine(r, 'rgba(255,255,255,.55)');
}, YEL);

add('globe_desert', '地區', '沙漠水晶球', r => {
  H.inSphere(r, r => {
    const sky = r.createLinearGradient(0, C - R, 0, C + 30);
    sky.addColorStop(0, '#6a4fa3'); sky.addColorStop(0.55, '#f08a5d'); sky.addColorStop(1, '#ffd38a'); r.fillStyle = sky; r.fillRect(0, 0, W, W);
    r.fillStyle = '#fff2b8'; r.beginPath(); r.arc(C + 40, C + 5, 22, 0, P2); r.fill();
    const dune = (y0, amp, ph, c1, c2) => {
      const g = r.createLinearGradient(0, y0 - amp, 0, W); g.addColorStop(0, c1); g.addColorStop(1, c2); r.fillStyle = g;
      r.beginPath(); r.moveTo(0, W); for (let x = 0; x <= W; x += 6) r.lineTo(x, y0 + amp * Math.sin(x / 40 + ph)); r.lineTo(W, W); r.closePath(); r.fill();
    };
    dune(C + 20, 10, 0.5, '#e7a95b', '#b8723a'); dune(C + 55, 14, 2.2, '#f2c27a', '#c98446'); dune(C + 88, 9, 4.0, '#f7d494', '#d59a55');
    r.fillStyle = '#2f5a3a';
    const cx = C - 50, by = C + 80;
    r.beginPath(); r.roundRect(cx - 7, by - 70, 14, 72, 7); r.fill();
    r.beginPath(); r.roundRect(cx - 28, by - 48, 10, 30, 5); r.fill(); r.fillRect(cx - 28, by - 26, 24, 9);
    r.beginPath(); r.roundRect(cx + 17, by - 58, 10, 28, 5); r.fill(); r.fillRect(cx + 4, by - 36, 23, 9);
  });
  H.gloss(r, 0.45, 0.45); H.rimLine(r, 'rgba(255,255,255,.5)');
}, CYAN);

// ───────────────────────── 星球（真實貼圖） ─────────────────────────
add('moon', '星球', '月亮', r => H.wrapTexture(r, TEX.moon, { ambient: 0.5, diffuse: 0.65, spec: 0 }), GREEN);
add('earth', '星球', '地球（台灣朝前）', r => { H.wrapTexture(r, TEX.earth_daymap, { lon0: 121 / 180 * Math.PI, tilt: -0.35, ambient: 0.45, spec: 0.25, shininess: 18 }); H.gloss(r, 0.18); }, YEL);
add('mars', '星球', '火星', r => H.wrapTexture(r, TEX.mars, { lon0: 1.2, ambient: 0.45, spec: 0 }), CYAN);
add('jupiter', '星球', '木星', r => H.wrapTexture(r, TEX.jupiter, { lon0: -1.0, ambient: 0.48, spec: 0 }), CYAN);
add('sun', '星球', '太陽', r => {
  H.inScreen(r, r => { const g = r.createRadialGradient(C, C, R * 0.9, C, C, 120); g.addColorStop(0, 'rgba(255,170,40,0.9)'); g.addColorStop(1, 'rgba(255,90,0,0)'); r.fillStyle = g; r.fillRect(0, 0, W, W); });
  H.wrapTexture(r, TEX.sun, { emit: true });
}, BLUE);
add('saturn', '星球', '土星', r => {
  H.wrapTexture(r, TEX.saturn, { ambient: 0.48, spec: 0 });
  const s = H.sampler(TEX.saturn_ring), tilt = -0.26, k = 0.3, rin = 76, rout = 119;
  const img = r.getImageData(0, 0, W, W), d = img.data, ct = Math.cos(tilt), st = Math.sin(tilt);
  for (let y = 0; y < W; y++) for (let x = 0; x < W; x++) {
    const dx = x + 0.5 - C, dy = y + 0.5 - C;
    if (dx * dx + dy * dy > 119.5 * 119.5) continue;
    const u = dx * ct + dy * st, v = (-dx * st + dy * ct) / k, rad = Math.hypot(u, v);
    if (rad < rin || rad > rout) continue;
    if (v < 0 && dx * dx + dy * dy < R * R) continue;   // 環的後半圈被星球擋住
    const c = s((rad - rin) / (rout - rin), 0.5), a = Math.min(1, c[3] / 255 * 1.7), i = (y * W + x) * 4;
    for (let q = 0; q < 3; q++) d[i + q] = d[i + q] * (1 - a) + Math.min(255, c[q] * 1.2) * a;
  }
  r.putImageData(img, 0, 0);
}, CYAN);

// ───────────────────────── 節慶/吃的/Marvin 本色 ─────────────────────────
add('pumpkin', '節慶', '萬聖節南瓜', r => {
  const cx = C, cy = C;
  r.lineCap = 'round'; r.lineJoin = 'round';
  const sph = r.createRadialGradient(cx - R * 0.34, cy - R * 0.42, R * 0.05, cx, cy, R * 1.07);
  sph.addColorStop(0, '#ffd9a0'); sph.addColorStop(0.3, '#ffa94a'); sph.addColorStop(0.66, '#ec7d16'); sph.addColorStop(0.9, '#c25a08'); sph.addColorStop(1, '#7c3402');
  r.fillStyle = sph; r.beginPath(); r.arc(cx, cy, R, 0, P2); r.fill();
  H.inSphere(r, r => {
    for (const th of [0.42, 0.95, 1.38]) {
      const rx = R * Math.sin(th);
      r.lineWidth = 5; r.strokeStyle = 'rgba(110,40,0,0.30)'; r.beginPath(); r.ellipse(cx, cy, rx, R, 0, 0, P2); r.stroke();
      r.lineWidth = 2; r.strokeStyle = 'rgba(255,220,160,0.18)'; r.beginPath(); r.ellipse(cx, cy, rx + 5, R, 0, 0, P2); r.stroke();
    }
    r.lineWidth = 4; r.strokeStyle = 'rgba(110,40,0,0.22)'; r.beginPath(); r.moveTo(cx, cy - R); r.lineTo(cx, cy + R); r.stroke();
  });
  H.gloss(r, 0.5);
  const sx = cx + 2, sy = cy - R + 16, stem = r.createLinearGradient(sx - 8, 0, sx + 10, 0);
  stem.addColorStop(0, '#3d5a1e'); stem.addColorStop(0.5, '#6b8f2f'); stem.addColorStop(1, '#2c4214');
  r.fillStyle = stem; r.beginPath();
  r.moveTo(sx - 9, sy); r.quadraticCurveTo(sx - 8, sy - 10, sx - 2, sy - 15); r.quadraticCurveTo(sx + 6, sy - 19, sx + 13, sy - 14);
  r.quadraticCurveTo(sx + 7, sy - 11, sx + 5, sy - 6); r.quadraticCurveTo(sx + 5, sy - 1, sx + 8, sy); r.closePath(); r.fill();
}, GREEN);

add('santa', '節慶', '聖誕帽', r => {
  drawDefault(r);
  const cx = C, cy = C, BRIM_Y = cy - R * 0.40;
  H.inSphere(r, r => {
    const red = r.createRadialGradient(cx - R * 0.35, cy - R * 0.8, R * 0.05, cx, cy - R * 0.4, R * 1.0);
    red.addColorStop(0, '#ff6b6b'); red.addColorStop(0.45, '#d42a2a'); red.addColorStop(0.85, '#9a1010'); red.addColorStop(1, '#600606');
    r.fillStyle = red; r.beginPath(); r.moveTo(cx - R - 2, BRIM_Y);
    r.quadraticCurveTo(cx - R * 0.6, cy - R * 1.15, cx + R * 0.25, cy - R * 1.05);
    r.quadraticCurveTo(cx + R * 0.85, cy - R * 0.95, cx + R * 0.70, cy - R * 0.62);
    r.lineTo(cx + R + 2, BRIM_Y); r.closePath(); r.fill();
    r.strokeStyle = 'rgba(60,0,0,0.25)'; r.lineWidth = 3;
    r.beginPath(); r.moveTo(cx + R * 0.05, BRIM_Y - 4); r.quadraticCurveTo(cx + R * 0.25, cy - R * 0.78, cx + R * 0.55, cy - R * 0.82); r.stroke();
    for (let x = cx - R - 6; x <= cx + R + 6; x += 7) puff(r, x, BRIM_Y + Math.sin(x * 0.9) * 1.5, 9, '#ffffff', '#c9d0d6');
  });
  puff(r, cx + R * 0.70, cy - R * 0.60, 13, '#ffffff', '#b9c2c9');
}, GREEN);

add('lantern', '節慶', '紅燈籠', r => {
  H.paintSphere(r, (n, phi) => {
    const rib = Math.abs(Math.sin(phi * 5.5)) < 0.08;
    const glow = 0.85 + 0.35 * n[2];
    return rib ? [120 * glow, 10, 10] : [235 * glow, 40 * glow, 30 * glow];
  }, { ambient: 0.6, diffuse: 0.45, spec: 0.2 });
  const cap = (y0, y1) => H.inSphere(r, r => { const g = r.createLinearGradient(0, y0, 0, y1); g.addColorStop(0, '#f7d36a'); g.addColorStop(1, '#a8761d'); r.fillStyle = g; r.fillRect(0, y0, W, y1 - y0); });
  cap(C - R, C - R + 16); cap(C + R - 16, C + R);
  H.inScreen(r, r => { r.strokeStyle = '#e9b949'; r.lineWidth = 2; for (let k = -3; k <= 3; k++) { r.beginPath(); r.moveTo(C + k * 3, C + R - 2); r.lineTo(C + k * 4, 239); r.stroke(); } });
}, YEL);

add('watermelon', '節慶', '西瓜', r => {
  H.paintSphere(r, n => {
    const m = H.rot(n, 0.3, 0.2), lon = Math.atan2(m[0], m[2]), lat = Math.asin(m[1]);
    const st = Math.sin(lon * 7 + 0.5 * Math.sin(lat * 9)) > 0.25;
    const k = 0.92 + 0.12 * H.noise3(n[0] * 20, n[1] * 20, n[2] * 20);
    return st ? [26 * k, 78 * k, 30 * k] : [118 * k, 176 * k, 78 * k];
  }, { spec: 0.3, shininess: 20 });
}, YEL);

add('astronaut', '變裝', '太空人頭盔', r => {
  H.paintSphere(r, () => [236, 238, 240], { spec: 0.3 });
  H.inSphere(r, r => {
    r.fillStyle = '#9aa3ab'; r.beginPath(); r.ellipse(C, C + 14, 92, 72, 0, 0, P2); r.fill();
    const g = r.createLinearGradient(C - 80, C - 50, C + 80, C + 80);
    g.addColorStop(0, '#ffe08a'); g.addColorStop(0.4, '#c8922c'); g.addColorStop(0.75, '#6b4512'); g.addColorStop(1, '#2b1a06');
    r.fillStyle = g; r.beginPath(); r.ellipse(C, C + 14, 84, 64, 0, 0, P2); r.fill();
    r.fillStyle = 'rgba(255,255,255,0.35)'; r.beginPath(); r.ellipse(C - 40, C - 18, 26, 10, -0.5, 0, P2); r.fill();
    r.fillStyle = '#d94f3d'; r.beginPath(); r.arc(C - 96, C - 10, 4, 0, P2); r.fill(); r.fillStyle = '#4fd96b'; r.beginPath(); r.arc(C + 96, C - 10, 4, 0, P2); r.fill();
  });
  H.gloss(r, 0.4);
}, CYAN);

add('diving', '變裝', '潛水頭盔', r => {
  H.paintSphere(r, n => { const k = 0.9 + 0.15 * H.noise3(n[0] * 12, n[1] * 12, n[2] * 12); return [190 * k, 142 * k, 62 * k]; }, { spec: 0.75, shininess: 22, ambient: 0.45 });
  H.inSphere(r, r => {
    const g = r.createRadialGradient(C - 15, C, 10, C, C + 12, 64); g.addColorStop(0, '#2c6a78'); g.addColorStop(1, '#0d2a33');
    r.fillStyle = '#7a5520'; r.beginPath(); r.arc(C, C + 12, 72, 0, P2); r.fill();
    r.fillStyle = g; r.beginPath(); r.arc(C, C + 12, 62, 0, P2); r.fill();
    r.fillStyle = 'rgba(255,255,255,0.25)'; r.beginPath(); r.ellipse(C - 26, C - 18, 18, 8, -0.6, 0, P2); r.fill();
    for (let k = 0; k < 10; k++) { const a = k / 10 * P2; puff(r, C + 67 * Math.cos(a), C + 12 + 67 * Math.sin(a), 3.2, '#ffe9a8', '#6b4a14'); }
  });
}, CYAN);

add('rusty', '變裝', '生鏽 Marvin', r => {
  H.paintSphere(r, n => {
    const rust = H.fbm(n, 3.5, 5), lat = Math.asin(n[1]), lon = Math.atan2(n[0], n[2]);
    if (Math.abs(lat + 0.62) < 0.012 || Math.abs(Math.abs(lon) - 0.95) < 0.01) return [52, 54, 58];
    const metal = [150, 156, 162], rc = [128, 58, 22];
    return H.mix(metal, rc, H.clamp01((rust - 0.42) * 3.2));
  }, { spec: 0.35, shininess: 18 });
  r.fillStyle = 'rgba(40,40,44,0.7)';
  for (let k = 0; k < 4; k++) for (const s of [-1, 1]) { const [x, y] = H.proj(s * (0.25 + k * 0.33), -0.39); r.beginPath(); r.arc(x, y, 2, 0, P2); r.fill(); }
}, GREEN);

// ───────────────────────── 雪花水晶球擺設（城市縮影/美食/物件） ─────────────────────────
// 擺設畫在透明背景上（透明＝水），透過 H.marble 的水球折射＋磨砂外殼看到。
// 擺設要畫在半徑 ~80 內：折射會放大約 1.3 倍，太外面會被球緣裁掉。
const GLOBE = { frost: 0.3, ri: 0.97, blur: 1.2, feather: 24, depth: -0.3, snow: 50, transparent: true };
function globeGround(g) {   // 擺設底下的雪丘
  const gr = g.createLinearGradient(0, C + 50, 0, C + 95);
  gr.addColorStop(0, '#ffffff'); gr.addColorStop(1, '#b9c9d8');
  g.fillStyle = gr; g.beginPath(); g.ellipse(C, C + 72, 88, 24, 0, 0, P2); g.fill();
}
// k：擺設整體縮放（以雪丘中心為準），太大的擺設會被折射放大到球緣外
const addGlobe = (id, title, scene, k = 1) => add(id, '水晶球', title, r => H.marble(r, g => {
  g.save(); g.translate(C, C + 72); g.scale(k, k); g.translate(-C, -(C + 72)); scene(g); g.restore();
}, GLOBE), GREEN);

addGlobe('globe_taipei101', '台北 101', g => {
  g.fillStyle = 'rgba(120,140,170,0.55)';   // 背景小樓
  for (const [x, w, h] of [[-62, 16, 40], [-44, 14, 58], [40, 16, 50], [58, 14, 34]]) g.fillRect(C + x, C + 62 - h, w, h);
  globeGround(g);
  const glass = g.createLinearGradient(C - 20, 0, C + 20, 0);
  glass.addColorStop(0, '#3d7268'); glass.addColorStop(0.45, '#8cc2b5'); glass.addColorStop(1, '#2a524b');
  g.fillStyle = glass; g.strokeStyle = '#1f3d38'; g.lineWidth = 1;
  g.fillRect(C - 17, C + 46, 34, 20); g.strokeRect(C - 17, C + 46, 34, 20);   // 裙樓
  for (let k = 0; k < 8; k++) {   // 八節竹節，每節上寬下窄
    const yb = C + 46 - k * 13, yt = yb - 13;
    g.beginPath(); g.moveTo(C - 12, yb); g.lineTo(C - 16, yt); g.lineTo(C + 16, yt); g.lineTo(C + 12, yb); g.closePath(); g.fill(); g.stroke();
    g.strokeStyle = 'rgba(220,255,245,0.35)'; g.beginPath(); g.moveTo(C - 13, yb - 6); g.lineTo(C + 13, yb - 6); g.stroke(); g.strokeStyle = '#1f3d38';
  }
  g.fillRect(C - 9, C - 66, 18, 8); g.strokeRect(C - 9, C - 66, 18, 8);
  g.fillRect(C - 6, C - 73, 12, 7); g.strokeRect(C - 6, C - 73, 12, 7);
  g.strokeStyle = '#2a524b'; g.lineWidth = 2; g.beginPath(); g.moveTo(C, C - 73); g.lineTo(C, C - 98); g.stroke();
});

addGlobe('globe_eiffel', '艾菲爾鐵塔', g => {
  globeGround(g);
  const L = [[-46, 64], [-31, 34], [-19, 6], [-11, -24], [-6, -54], [-3, -82], [0, -100]];
  g.save(); g.beginPath(); g.moveTo(C + L[0][0], C + L[0][1]);
  for (const [x, y] of L) g.lineTo(C + x, C + y);
  for (let k = L.length - 1; k >= 0; k--) g.lineTo(C - L[k][0], C + L[k][1]);
  g.closePath(); g.clip();
  g.fillStyle = '#5b4632'; g.fillRect(0, 0, W, W);
  g.strokeStyle = 'rgba(170,140,100,0.8)'; g.lineWidth = 1;   // 鋼架交叉格
  for (let k = -140; k < 140; k += 6) { g.beginPath(); g.moveTo(C + k, C - 110); g.lineTo(C + k + 170, C + 70); g.moveTo(C + k, C - 110); g.lineTo(C + k - 170, C + 70); g.stroke(); }
  g.restore();
  g.save(); g.globalCompositeOperation = 'destination-out';   // 底下的拱門
  g.beginPath(); g.ellipse(C, C + 66, 24, 28, 0, Math.PI, P2); g.fill(); g.restore();
  g.fillStyle = '#3e2f20';
  g.fillRect(C - 30, C + 22, 60, 5); g.fillRect(C - 17, C - 14, 34, 4); g.fillRect(C - 8, C - 58, 16, 4);
});

addGlobe('globe_bubbletea', '珍珠奶茶', g => {
  globeGround(g);
  const top = C - 48, bot = C + 66;
  const cup = () => { g.beginPath(); g.moveTo(C - 36, top); g.lineTo(C + 36, top); g.lineTo(C + 27, bot); g.lineTo(C - 27, bot); g.closePath(); };
  const tea = g.createLinearGradient(0, top, 0, bot); tea.addColorStop(0, '#e2bf93'); tea.addColorStop(1, '#b8875a');
  g.fillStyle = tea; cup(); g.fill();
  g.save(); cup(); g.clip();
  for (let k = 0; k < 30; k++) { g.fillStyle = '#2a170e'; g.beginPath(); g.arc(C - 26 + 52 * H.hash3(k, 1, 9), bot - 6 - 22 * H.hash3(k, 2, 9), 5, 0, P2); g.fill(); }
  g.fillStyle = 'rgba(255,255,255,0.35)'; g.fillRect(C - 26, top, 7, bot - top);
  g.restore();
  g.strokeStyle = 'rgba(80,50,30,0.6)'; g.lineWidth = 1.5; cup(); g.stroke();
  g.fillStyle = 'rgba(235,240,245,0.85)'; g.beginPath(); g.ellipse(C, top, 38, 7, 0, 0, P2); g.fill();   // 封膜
  g.strokeStyle = '#e8455a'; g.lineWidth = 8; g.lineCap = 'round';   // 吸管
  g.beginPath(); g.moveTo(C + 6, top + 30); g.lineTo(C + 18, C - 96); g.stroke();
});

addGlobe('globe_xiaolongbao', '小籠包', g => {
  globeGround(g);
  const bam = g.createLinearGradient(0, C + 10, 0, C + 66); bam.addColorStop(0, '#d9b36f'); bam.addColorStop(1, '#9b7432');
  g.fillStyle = bam; g.fillRect(C - 70, C + 20, 140, 44);
  g.beginPath(); g.ellipse(C, C + 64, 70, 14, 0, 0, Math.PI); g.fill();
  g.strokeStyle = 'rgba(90,60,20,0.5)'; g.lineWidth = 1.5;
  for (const y of [C + 32, C + 46]) { g.beginPath(); g.moveTo(C - 70, y); g.lineTo(C + 70, y); g.stroke(); }
  g.fillStyle = '#c69a52'; g.beginPath(); g.ellipse(C, C + 20, 70, 14, 0, 0, P2); g.fill();
  const bao = (x, y, s) => {
    const sk = g.createRadialGradient(x - 8 * s, y - 10 * s, 2, x, y, 30 * s); sk.addColorStop(0, '#ffffff'); sk.addColorStop(1, '#e6ddd0');
    g.fillStyle = sk; g.beginPath(); g.moveTo(x - 28 * s, y + 10 * s);
    g.quadraticCurveTo(x - 30 * s, y - 14 * s, x, y - 24 * s); g.quadraticCurveTo(x + 30 * s, y - 14 * s, x + 28 * s, y + 10 * s); g.closePath(); g.fill();
    g.strokeStyle = 'rgba(150,130,100,0.6)'; g.lineWidth = 1;
    for (let k = -4; k <= 4; k++) { g.beginPath(); g.moveTo(x, y - 22 * s); g.quadraticCurveTo(x + k * 5 * s, y - 10 * s, x + k * 6.5 * s, y + 8 * s); g.stroke(); }
  };
  bao(C - 34, C + 8, 0.85); bao(C + 34, C + 8, 0.85); bao(C, C - 2, 1);
  g.strokeStyle = 'rgba(255,255,255,0.6)'; g.lineWidth = 2;   // 蒸氣
  for (const x of [C - 20, C + 4, C + 26]) { g.beginPath(); g.moveTo(x, C - 32); g.bezierCurveTo(x - 10, C - 48, x + 10, C - 60, x, C - 78); g.stroke(); }
}, 0.72);

addGlobe('globe_microphone', '復古麥克風', g => {
  globeGround(g);
  g.fillStyle = '#2b2b30'; g.beginPath(); g.ellipse(C, C + 62, 30, 8, 0, 0, P2); g.fill();   // 底座
  g.fillRect(C - 3, C + 6, 6, 56);
  const chrome = g.createLinearGradient(C - 30, 0, C + 30, 0);
  chrome.addColorStop(0, '#6d737a'); chrome.addColorStop(0.35, '#f2f4f6'); chrome.addColorStop(0.7, '#9aa1a8'); chrome.addColorStop(1, '#4b5056');
  g.fillStyle = chrome; g.beginPath(); g.roundRect(C - 26, C - 82, 52, 90, 26); g.fill();
  g.strokeStyle = 'rgba(40,44,50,0.55)'; g.lineWidth = 1.4;   // 網罩橫紋
  for (let y = C - 70; y < C + 2; y += 6) { g.beginPath(); g.moveTo(C - 24, y); g.lineTo(C + 24, y); g.stroke(); }
  g.fillStyle = '#c9a24a'; g.fillRect(C - 26, C - 30, 52, 8);   // 金色腰帶
}, 0.85);
