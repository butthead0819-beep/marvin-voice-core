// gen_pixel_golden.js — 用 face_pixel.js（JS 標準答案）產生固定姿勢清單的渲染結果，
// 給 firmware/car_puck/test/test_face_rt.cpp 比對 C++ 移植版 face_rt.h。
//
// 用法：node tools/puck_face/gen_pixel_golden.js <輸出檔路徑>

const fs = require('fs');
const { renderPixel, W } = require('./face_pixel.js');

const outPath = process.argv[2];
if (!outPath) {
  console.error('usage: node gen_pixel_golden.js <output path>');
  process.exit(1);
}

const FRAME_BYTES = W * W * 3;

function makeBase() {
  const base = new Uint8Array(FRAME_BYTES);
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

const base = makeBase();
const poses = buildPoses();

const headerBytes = 4 + 4;
const recordBytes = 5 * 4 + 4 * 2 + FRAME_BYTES;
const buf = Buffer.alloc(headerBytes + poses.length * recordBytes);

let off = 0;
buf.writeUInt32LE(0x46505847, off); off += 4; // "GXPF"
buf.writeUInt32LE(poses.length, off); off += 4;

const out = new Uint8Array(FRAME_BYTES);
for (const p of poses) {
  const rect = renderPixel(out, base, p);

  buf.writeFloatLE(p.yaw, off); off += 4;
  buf.writeFloatLE(p.pitch, off); off += 4;
  buf.writeFloatLE(p.roll, off); off += 4;
  buf.writeFloatLE(p.browUp, off); off += 4;
  buf.writeFloatLE(p.err, off); off += 4;
  buf.writeInt16LE(rect.x0, off); off += 2;
  buf.writeInt16LE(rect.y0, off); off += 2;
  buf.writeInt16LE(rect.x1, off); off += 2;
  buf.writeInt16LE(rect.y1, off); off += 2;

  Buffer.from(out.buffer, out.byteOffset, FRAME_BYTES).copy(buf, off);
  off += FRAME_BYTES;
}

fs.writeFileSync(outPath, buf);
console.log(`wrote ${poses.length} poses, ${buf.length} bytes -> ${outPath}`);
