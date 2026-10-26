// atlas_export.js — 把 face_atlas.js 烤好的 sdf 圖集匯出成 S3 能直接查表的二進位檔（MFA1），
// 另可匯出 C header（燒進 flash）與 golden（給 firmware/car_puck/test/test_face_atlas_rt.cpp 比對 face_atlas_rt.h）。
//
// 用法：
//   node tools/puck_face/atlas_export.js --W 466 --step 0.1 --mode sdf2 --out <檔.bin>
//           [--header <檔.h> --name <C 陣列名>]
//           [--golden <檔.bin> [--band 2.5]]
//   --header：同一份二進位內容輸出成 C header（static const uint8_t <name>[] 與 <name>_LEN）。
//   --golden：用這份圖集對 gen_3d_golden.js 同一份 248 姿勢跑 recallHybrid（bilinear）；
//             沒給 --band 就是 -1（純查表）。
//
// 角色固定 MARVIN（之後客製角色再加參數）。只支援 sdf1 / sdf2（S3 端只做 sdf 查表）。
//
// MFA1 格式（little-endian；每個區段起點對齊 4 bytes）：
//   檔頭：magic u32 = 0x3141464D（"MFA1"）、W u16、s u8（texel 間距）、flags u8（bit0 = 有眉線）、D f32（= 4*s+2，量化範圍）、
//         眼格子：n0 u16, n1 u16, lo0 f32, lo1 f32, hi0 f32, hi1 f32；眉線格子同格式（沒眉線全 0）、
//         nSprites u32（= 眼小圖數 + 眉線小圖數，順序：先眼後眉，各自依 i*n1+j）、
//         offset u32 × nSprites（每張小圖從檔案開頭算的位元組位移）。
//   每張小圖：w u16, h u16, ox f32, oy f32, nch u8, far[3] u8（只用前 nch 個）、接著 dense 資料 w*h*nch bytes
//         （不做列跨距壓縮，S3 隨機存取用；texel (i,j) = data[(j*w+i)*nch + k]），結尾補齊到 4 bytes。
//   通道編碼、far 值、小圖座標定位見 face_atlas.js 檔頭 sdf 段。
//
// golden 格式（little-endian）：magic u32 = 0x4C544147（"GATL"）、W u32、count u32、band f32；
//   每筆：yaw,pitch,roll,browUp,err（f32×5）、rect x0,y0,x1,y1（i16×4）、exactPx u32、frame（W*W*3 bytes，RGB888）。

const fs = require('fs');
const { MARVIN } = require('./face_3d.js');
const { bake, recallHybrid } = require('./face_atlas.js');

const arg = (name, def) => { const i = process.argv.indexOf('--' + name); return i > 0 ? process.argv[i + 1] : def; };
const W = parseInt(arg('W', ''), 10), step = parseFloat(arg('step', '')), mode = arg('mode', '');
const outPath = arg('out', ''), headerPath = arg('header', ''), name = arg('name', ''), goldenPath = arg('golden', '');
const band = parseFloat(arg('band', '-1'));
if (!(W > 0) || !(step > 0) || (mode !== 'sdf1' && mode !== 'sdf2') || !outPath || (headerPath && !name) || Number.isNaN(band)) {
  console.error('usage: node atlas_export.js --W 466 --step 0.1 --mode sdf1|sdf2 --out <檔.bin> [--header <檔.h> --name <C 陣列名>] [--golden <檔.bin> [--band 2.5]]');
  process.exit(1);
}
const ch = MARVIN;
const s = mode === 'sdf1' ? 1 : 2, D = 4 * s + 2;

const atlas = bake(W, ch, step, mode);

// ---- MFA1 ----
const sprites = atlas.eye.sprites.concat(atlas.brow ? atlas.brow.sprites : []);
const align4 = n => (n + 3) & ~3;
const spriteBytes = sp => align4(16 + sp.w * sp.h * sp.nch);
const tableOff = 56, offsets = [];
let total = tableOff + 4 * sprites.length;
for (const sp of sprites) { total = align4(total); offsets.push(total); total += spriteBytes(sp); }
total = align4(total);

const buf = Buffer.alloc(total);
buf.writeUInt32LE(0x3141464D, 0);
buf.writeUInt16LE(W, 4);
buf.writeUInt8(s, 6);
buf.writeUInt8(atlas.brow ? 1 : 0, 7);
buf.writeFloatLE(D, 8);
const writeGrid = (g, p) => {
  if (!g) return;   // 沒眉線：全 0（Buffer.alloc 已清零）
  buf.writeUInt16LE(g.n[0], p); buf.writeUInt16LE(g.n[1], p + 2);
  buf.writeFloatLE(g.lo[0], p + 4); buf.writeFloatLE(g.lo[1], p + 8);
  buf.writeFloatLE(g.hi[0], p + 12); buf.writeFloatLE(g.hi[1], p + 16);
};
writeGrid(atlas.eye, 12);
writeGrid(atlas.brow, 32);
buf.writeUInt32LE(sprites.length, 52);
sprites.forEach((sp, k) => {
  buf.writeUInt32LE(offsets[k], tableOff + 4 * k);
  let p = offsets[k];
  buf.writeUInt16LE(sp.w, p); buf.writeUInt16LE(sp.h, p + 2);
  buf.writeFloatLE(sp.ox, p + 4); buf.writeFloatLE(sp.oy, p + 8);
  buf.writeUInt8(sp.nch, p + 12);
  for (let k2 = 0; k2 < sp.nch; k2++) buf.writeUInt8(sp.far[k2], p + 13 + k2);
  Buffer.from(sp.data.buffer, sp.data.byteOffset, sp.data.length).copy(buf, p + 16);
});
fs.writeFileSync(outPath, buf);
console.log(`wrote ${outPath}: ${buf.length} bytes (${(buf.length / 1024).toFixed(1)} KB), ${sprites.length} sprites`);

// ---- C header ----
if (headerPath) {
  const lines = [];
  for (let i = 0; i < buf.length; i += 16) lines.push('  ' + Array.from(buf.subarray(i, i + 16), b => '0x' + b.toString(16).padStart(2, '0')).join(', ') + ',');
  const cmd = 'node tools/puck_face/atlas_export.js ' + process.argv.slice(2).join(' ');
  const text = `// 自動產生，不要手改。指令：${cmd}\n` +
    `// 參數：W=${W} step=${step} mode=${mode} 角色=MARVIN，格式 MFA1（見 tools/puck_face/atlas_export.js 檔頭），${buf.length} bytes、${sprites.length} 張小圖。\n` +
    `static const uint8_t ${name}[] __attribute__((aligned(4))) = {\n${lines.join('\n')}\n};\n` +
    `static const uint32_t ${name}_LEN = ${buf.length};\n`;
  fs.writeFileSync(headerPath, text);
  console.log(`wrote ${headerPath}: ${name}[${buf.length}]`);
}

// ---- golden ----
if (goldenPath) {
  const base = makeBase(W), poses = buildPoses(), FRAME_BYTES = W * W * 3;
  const recordBytes = 5 * 4 + 4 * 2 + 4 + FRAME_BYTES;
  const g = Buffer.alloc(16 + poses.length * recordBytes);
  let off = 0;
  g.writeUInt32LE(0x4C544147, off); off += 4;   // "GATL"
  g.writeUInt32LE(W, off); off += 4;
  g.writeUInt32LE(poses.length, off); off += 4;
  g.writeFloatLE(band, off); off += 4;
  const out = new Uint8Array(FRAME_BYTES);
  for (const p of poses) {
    const stats = { exact: 0 };
    const rect = recallHybrid(out, base, W, p, ch, atlas, 'bilinear', band, stats);   // 開頭 out.set(base)，每個姿勢互相獨立
    for (const v of [p.yaw, p.pitch, p.roll, p.browUp, p.err]) { g.writeFloatLE(v, off); off += 4; }
    for (const v of [rect.x0, rect.y0, rect.x1, rect.y1]) { g.writeInt16LE(v, off); off += 2; }
    g.writeUInt32LE(stats.exact, off); off += 4;
    Buffer.from(out.buffer, out.byteOffset, FRAME_BYTES).copy(g, off); off += FRAME_BYTES;
  }
  fs.writeFileSync(goldenPath, g);
  console.log(`wrote ${goldenPath}: ${poses.length} poses, band=${band}, ${g.length} bytes`);
}

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

// 姿勢清單：跟 gen_pixel_golden.js、gen_3d_golden.js、atlas_eval.js 同一份清單（第四份，同 seed 12345、同 248 個姿勢），改一邊要改另外三邊。
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
