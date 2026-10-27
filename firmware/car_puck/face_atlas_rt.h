// face_atlas_rt.h — tools/puck_face/face_atlas.js 的 recallHybrid（sdf 圖集、bilinear 格點內插）的 C++ 版，
// S3 跟 host 共用，規格以 JS 為準。圖集是 tools/puck_face/atlas_export.js 匯出的 MFA1 二進位（格式見該檔檔頭），
// 燒在 flash 或複製到 PSRAM 都行；這裡只解析檔頭、不複製資料，小圖資料用到才讀。
//
// 用法：
//   FaceAtlasView at;
//   if (!face_atlas_open(blob, len, &at)) { /* magic、長度、小圖範圍不對 */ }
//   face3d_init(&work, at.W);                       // work 同 face3d_render：Face3dWork，呼叫端配置
//   face_atlas_render(FACE3D_MARVIN, at, pose, band, base888, out888, &work, &exactPx);
//   band < 0：純查表；band >= 0：混合式（離筆畫邊緣 < band 像素的像素改用 face_rt3d.h 的精確算法）。
//
// 原則：
//   - 重用 face_rt3d.h 的 prepare / mix / gradColor / stroke_fields，不複製一份；內部符號放 face_atlas_detail。
//   - 像素範圍＝prepare 的 rect 全掃，但每筆畫先用「小圖 texel 範圍聯集外框（外擴一個 s）」粗篩：局部座標落在外框外
//     （或該 Plan 沒有非空小圖）時取樣結果必定全是 far，直接給 far 值、不呼叫 sample，輸出不變。只有實際畫到（cov>0 或 rc>0 或 bc>0）的像素才寫 out888、擴張 cur；
//     work 的 prev/cur 與 out888 不變式同 face3d_render（見 face_rt3d.h 檔頭）。
//   - 取樣：先把 u8 解碼成像素距離再雙線性，小圖外用 far；覆蓋率 = clamp01(0.5+dFill)、clamp01(RIM_W/2-dRim+0.5)、clamp01(BROW_W/2-dBrow+0.5)。
//   - 全部 float。
#ifndef FACE_ATLAS_RT_H
#define FACE_ATLAS_RT_H

#include "face_rt3d.h"

// 同 face_rt3d.h：S3 上 Arduino 預設 -Os，這個檔案單獨改用 O2＋fast-math；clang 不認，跳過。
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC push_options
#pragma GCC optimize("O2", "fast-math")
#endif

constexpr uint32_t FACE_ATLAS_MAGIC = 0x3141464Du;  // "MFA1"

// 一個格子（眼或眉線）：格點數、範圍、第一張小圖在總表裡的索引（小圖索引 = base + i*n1 + j）
struct FaceAtlasGrid {
  int n0, n1;
  float lo0, lo1, hi0, hi1;
  uint32_t base;
};

// 解析 MFA1 後的指標與欄位（不複製資料；blob 必須活得比 view 久）
struct FaceAtlasView {
  const uint8_t* blob;
  uint32_t len;
  int W;
  int s;                 // texel 間距（像素）
  bool hasBrow;
  float D;               // 距離量化範圍（像素）
  FaceAtlasGrid eye, brow;
  uint32_t nSprites;
  const uint8_t* offsets;  // nSprites × u32（little-endian，從 blob 開頭算）
};

namespace face_atlas_detail {

inline uint16_t rd16(const uint8_t* p) { return (uint16_t)(p[0] | (p[1] << 8)); }
inline uint32_t rd32(const uint8_t* p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }
inline float rdf(const uint8_t* p) { uint32_t u = rd32(p); float v; memcpy(&v, &u, 4); return v; }

constexpr uint32_t HEADER_BYTES = 56;   // magic .. nSprites
constexpr uint32_t SPRITE_HEAD = 16;    // w,h,ox,oy,nch,far[3]

// 解開的小圖檔頭，data 指向 dense 資料（w*h*nch bytes）
struct Spr {
  int w, h, nch;
  float ox, oy;
  const uint8_t* far;
  const uint8_t* data;
};

inline void get_sprite(const FaceAtlasView& at, uint32_t idx, Spr& sp) {
  const uint8_t* p = at.blob + rd32(at.offsets + 4 * idx);
  sp.w = rd16(p); sp.h = rd16(p + 2);
  sp.ox = rdf(p + 4); sp.oy = rdf(p + 8);
  sp.nch = p[12];
  sp.far = p + 13;
  sp.data = p + SPRITE_HEAD;
}

inline bool read_grid(const uint8_t* p, FaceAtlasGrid* g) {
  g->n0 = rd16(p); g->n1 = rd16(p + 2);
  g->lo0 = rdf(p + 4); g->lo1 = rdf(p + 8); g->hi0 = rdf(p + 12); g->hi1 = rdf(p + 16);
  return true;
}

inline float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

// u8 → 像素距離。nch==2 的通道 0 是有號 fill，其餘無號（同 face_atlas.js sdfDecode）
inline float decode(uint8_t u, int k, int nch, float D) {
  return (nch == 2 && k == 0) ? u / 255.0f * 2 * D - D : u / 255.0f * D;
}

struct Quad { Spr sp; float w; };
struct Plan {
  float sgn, ax, ay;   // 眼：sgn = 鏡像符號（左眼 -1、右眼 +1）；眉線 sgn = 1
  Quad q[4];
  int n;
};

// 一個座標落在哪些格點、各佔多少權重（bilinear，超出範圍 clamp）；同 face_atlas.js gridPlan
inline void grid_plan(const FaceAtlasView& at, const FaceAtlasGrid& g, float a, float b, Plan& pl) {
  const float g0 = clampf((a - g.lo0) / (g.hi0 - g.lo0) * (g.n0 - 1), 0.0f, (float)(g.n0 - 1));
  const float g1 = clampf((b - g.lo1) / (g.hi1 - g.lo1) * (g.n1 - 1), 0.0f, (float)(g.n1 - 1));
  int i0 = (int)floorf(g0), j0 = (int)floorf(g1);
  if (i0 > g.n0 - 2) i0 = g.n0 - 2;
  if (j0 > g.n1 - 2) j0 = g.n1 - 2;
  const float fi = g0 - i0, fj = g1 - j0;
  pl.n = 0;
  for (int di = 0; di < 2; di++) {
    for (int dj = 0; dj < 2; dj++) {
      const float w = (di ? fi : 1 - fi) * (dj ? fj : 1 - fj);
      if (w > 0) {
        Quad& q = pl.q[pl.n++];
        get_sprite(at, g.base + (uint32_t)((i0 + di) * g.n1 + (j0 + dj)), q.sp);
        q.w = w;
      }
    }
  }
}

// Plan 那幾張非空小圖的 texel 範圍在局部座標的聯集外框（各邊外擴一個 texel 間距 s）；框外的取樣必定全是 far。
struct Box { float x0, y0, x1, y1; bool empty; };
inline Box plan_box(const Plan& pl, float s) {
  Box b = {0, 0, 0, 0, true};
  for (int k = 0; k < pl.n; k++) {
    const Spr& sp = pl.q[k].sp;
    if (sp.w <= 0 || sp.h <= 0) continue;
    const float bx0 = sp.ox - s, bx1 = sp.ox + (sp.w - 1) * s + s;
    const float by0 = sp.oy - s, by1 = sp.oy + (sp.h - 1) * s + s;
    if (b.empty) { b.x0 = bx0; b.y0 = by0; b.x1 = bx1; b.y1 = by1; b.empty = false; continue; }
    if (bx0 < b.x0) b.x0 = bx0;
    if (by0 < b.y0) b.y0 = by0;
    if (bx1 > b.x1) b.x1 = bx1;
    if (by1 > b.y1) b.y1 = by1;
  }
  return b;
}
inline bool box_out(const Box& b, float lx, float ly) { return b.empty || lx < b.x0 || lx > b.x1 || ly < b.y0 || ly > b.y1; }

// 在小圖的 (fx, fy)（texel 座標，texel 中心在整數）先解碼成距離再雙線性，乘 w 累加進 v[nch]；小圖外用 far
inline void sample(const Spr& sp, float D, float fx, float fy, float w, float* v) {
  const int x0 = (int)floorf(fx), y0 = (int)floorf(fy);
  const float tx = fx - x0, ty = fy - y0;
  for (int dj = 0; dj < 2; dj++) {
    for (int di = 0; di < 2; di++) {
      const float ww = w * (di ? tx : 1 - tx) * (dj ? ty : 1 - ty);
      if (ww == 0) continue;
      const int x = x0 + di, y = y0 + dj;
      const bool inside = x >= 0 && x < sp.w && y >= 0 && y < sp.h;
      const uint8_t* px = inside ? sp.data + ((size_t)y * sp.w + x) * sp.nch : sp.far;
      for (int k = 0; k < sp.nch; k++) v[k] += ww * decode(px[k], k, sp.nch, D);
    }
  }
}

}  // namespace face_atlas_detail

// 解析 MFA1。magic / 長度 / 格點 / 小圖範圍不對回傳 false（v 內容不可用）。
inline bool face_atlas_open(const uint8_t* blob, uint32_t len, FaceAtlasView* v) {
  using namespace face_atlas_detail;
  if (!blob || len < HEADER_BYTES) return false;
  if (rd32(blob) != FACE_ATLAS_MAGIC) return false;
  v->blob = blob;
  v->len = len;
  v->W = rd16(blob + 4);
  v->s = blob[6];
  v->hasBrow = (blob[7] & 1) != 0;
  v->D = rdf(blob + 8);
  read_grid(blob + 12, &v->eye);
  read_grid(blob + 32, &v->brow);
  v->nSprites = rd32(blob + 52);
  if (v->W <= 0 || v->W > FACE3D_MAX_W || v->s < 1) return false;
  if (v->eye.n0 < 2 || v->eye.n1 < 2 || v->eye.hi0 <= v->eye.lo0 || v->eye.hi1 <= v->eye.lo1) return false;
  const uint32_t nEye = (uint32_t)v->eye.n0 * v->eye.n1;
  uint32_t nBrow = 0;
  if (v->hasBrow) {
    if (v->brow.n0 < 2 || v->brow.n1 < 2 || v->brow.hi0 <= v->brow.lo0 || v->brow.hi1 <= v->brow.lo1) return false;
    nBrow = (uint32_t)v->brow.n0 * v->brow.n1;
  }
  if (v->nSprites != nEye + nBrow) return false;
  if ((uint64_t)HEADER_BYTES + 4ull * v->nSprites > len) return false;
  v->eye.base = 0;
  v->brow.base = nEye;
  v->offsets = blob + HEADER_BYTES;
  for (uint32_t i = 0; i < v->nSprites; i++) {   // 每張小圖的檔頭與資料都必須在檔案範圍內、通道數合理
    const uint32_t off = rd32(v->offsets + 4 * i);
    if ((uint64_t)off + SPRITE_HEAD > len) return false;
    Spr sp;
    get_sprite(*v, i, sp);
    const bool isBrow = i >= nEye;
    if (sp.nch != (isBrow ? 1 : 2)) return false;
    if ((uint64_t)off + SPRITE_HEAD + (uint64_t)sp.w * sp.h * sp.nch > len) return false;
  }
  return true;
}

inline FaceRect face_atlas_render(const Face3dChar& ch, const FaceAtlasView& at, const FacePose& p, float band,
                                  const uint8_t* base888, uint8_t* out888, Face3dWork* work, uint32_t* exactPx) {
  using namespace face_rt3d_detail;
  using namespace face_atlas_detail;
  const int W = work->W;
  const bool hybrid = band >= 0;
  uint32_t nExact = 0;

  // 把上一格的畫過範圍（cur）存成 prev，再用 base 還原那些範圍；cur 清空，等下面逐像素填回（同 face3d_render）。
  for (int y = 0; y < W; y++) {
    work->prevX0[y] = work->curX0[y];
    work->prevX1[y] = work->curX1[y];
    if (work->prevX1[y] >= work->prevX0[y]) {
      size_t off = (size_t)(y * W + work->prevX0[y]) * 3;
      memcpy(out888 + off, base888 + off, (size_t)(work->prevX1[y] - work->prevX0[y] + 1) * 3);
    }
    work->curX0[y] = (int16_t)W;
    work->curX1[y] = -1;
  }

  const float C = W * 0.5f, R = W * 0.45f;
  const float RIM_W = R * 0.035f > 2.0f ? R * 0.035f : 2.0f;
  const float BROW_W = R * 0.02f > 1.5f ? R * 0.02f : 1.5f;
  const int pad = 4 + (int)ceilf((RIM_W > BROW_W ? RIM_W : BROW_W) / 2);
  const float D = at.D, sf = (float)at.s;

  const float e = clamp01(p.err);
  float col[3] = {
    ch.idle[0] + (ch.err[0] - ch.idle[0]) * e,
    ch.idle[1] + (ch.err[1] - ch.idle[1]) * e,
    ch.idle[2] + (ch.err[2] - ch.idle[2]) * e,
  };

  Prepared F;
  prepare(ch, p, W, col, pad, F);
  const int x0 = F.rect.x0, y0 = F.rect.y0, x1 = F.rect.x1, y1 = F.rect.y1;

  // 每隻眼每格一次：錨點（clamp 後）、要取的小圖與權重（idx 0 = 左眼 sgn -1，同 F.eyes 順序）
  const FaceAtlasGrid& A = at.eye;
  Plan eyePlan[2];
  for (int idx = 0; idx < 2; idx++) {
    const float sgn = idx == 0 ? -1.0f : 1.0f;
    const float phiR = clampf(ch.cx + sgn * p.yaw, A.lo0, A.hi0), lamR = clampf(ch.cy + p.pitch, A.lo1, A.hi1);
    const V3 P = v3(phiR, lamR);
    Plan& pl = eyePlan[idx];
    pl.sgn = sgn; pl.ax = sgn * R * P.x; pl.ay = R * P.y;
    grid_plan(at, A, phiR, lamR, pl);
  }
  Plan browPlan;
  bool hasBrowPlan = false;
  if (F.hasBrow && at.hasBrow) {
    const FaceAtlasGrid& B = at.brow;
    const float yaw = clampf(p.yaw, B.lo0, B.hi0), L0 = clampf(ch.browLam + p.pitch - p.browUp, B.lo1, B.hi1);
    const V3 P = v3(yaw, L0);
    browPlan.sgn = 1; browPlan.ax = R * P.x; browPlan.ay = R * P.y;
    grid_plan(at, B, yaw, L0, browPlan);
    hasBrowPlan = true;
  }
  const Box eyeBox[2] = {plan_box(eyePlan[0], sf), plan_box(eyePlan[1], sf)};
  const Box browBox = hasBrowPlan ? plan_box(browPlan, sf) : Box{0, 0, 0, 0, true};

  static const bool ACT_ALL[3] = {true, true, true};
  static const float BLACK3[3] = {0, 0, 0};
  float c[3], g[3];
  for (int y = y0; y <= y1; y++) {
    for (int x = x0; x <= x1; x++) {
      const float dx = x + 0.5f - C, dy = y + 0.5f - C;
      const float ux = F.cr * dx + F.sr * dy, uy = -F.sr * dx + F.cr * dy;
      const float clip = clamp01(R - 0.5f - hyp(ux, uy) + 0.5f);
      if (clip <= 0) continue;
      const float nx = ux / R, ny = uy / R;
      if (nx * nx + ny * ny >= 1) continue;

      // 查表：兩眼的 dFill / dRim、眉線的 dBrow → 覆蓋率
      float cov[2], rc[2], dF[2], dR[2], bc = 0, dB = BIG;
      for (int idx = 0; idx < 2; idx++) {
        const Plan& pl = eyePlan[idx];
        const float lx = pl.sgn * (ux - pl.ax), ly = uy - pl.ay;
        float v[2] = {0, 0};
        if (box_out(eyeBox[idx], lx, ly)) {
          v[0] = -D; v[1] = D;
        } else {
          for (int k = 0; k < pl.n; k++) {
            const Quad& q = pl.q[k];
            sample(q.sp, D, (lx - q.sp.ox) / sf, (ly - q.sp.oy) / sf, q.w, v);
          }
        }
        dF[idx] = v[0]; dR[idx] = v[1];
        cov[idx] = clamp01(0.5f + v[0]);
        rc[idx] = clamp01(RIM_W / 2 - v[1] + 0.5f);
      }
      if (hasBrowPlan) {
        const float lx = ux - browPlan.ax, ly = uy - browPlan.ay;
        float v[1] = {0};
        if (box_out(browBox, lx, ly)) {
          v[0] = D;
        } else {
          for (int k = 0; k < browPlan.n; k++) {
            const Quad& q = browPlan.q[k];
            sample(q.sp, D, (lx - q.sp.ox) / sf, (ly - q.sp.oy) / sf, q.w, v);
          }
        }
        dB = v[0];
        bc = clamp01(BROW_W / 2 - v[0] + 0.5f);
      }

      // 混合式：任一筆畫離邊緣 < band 的像素改用精確值（三筆畫一起換）
      if (hybrid) {
        bool near = fabsf(dB - BROW_W / 2) < band;
        for (int idx = 0; idx < 2 && !near; idx++) near = fabsf(dF[idx]) < band || fabsf(dR[idx] - RIM_W / 2) < band;
        if (near) {
          Face3dFields fl;
          if (stroke_fields(F, R, RIM_W, BROW_W, ux, uy, ACT_ALL, fl)) {
            cov[0] = fl.cov[0]; cov[1] = fl.cov[1]; rc[0] = fl.rc[0]; rc[1] = fl.rc[1]; bc = fl.bc;
            nExact++;
          }
        }
      }

      if (!(cov[0] > 0 || rc[0] > 0 || cov[1] > 0 || rc[1] > 0 || bc > 0)) continue;   // 沒畫到：不寫 out888、不計入 cur

      // 疊色（查表值與精確值共用，順序同 face3d_render：眼 0、眼 1、眉線）
      const size_t i = (size_t)(y * W + x) * 3;
      c[0] = out888[i]; c[1] = out888[i + 1]; c[2] = out888[i + 2];
      for (int idx = 0; idx < 2; idx++) {
        const Eye& E = F.eyes[idx];
        if (cov[idx] > 0) {
          const float kk = cov[idx] * clip;
          mix(c, F.dark45, 0.98f * kk);
          gradColor(F, hyp(ux + C - E.sx, uy + C - E.sy) / (R * 0.46f), g);
          mix(c, g, 0.98f * kk);
          const float ty = (uy + C - (E.topY - R * 0.01f)) / (R * 0.17f);
          mix(c, BLACK3, 0.5f * (1 - clamp01(ty)) * kk);
        }
        if (rc[idx] > 0) mix(c, RIM_COL, 0.96f * rc[idx] * clip);
      }
      if (bc > 0) mix(c, BROW_COL, 0.92f * bc * clip);
      out888[i] = (uint8_t)(c[0] + 0.5f);
      out888[i + 1] = (uint8_t)(c[1] + 0.5f);
      out888[i + 2] = (uint8_t)(c[2] + 0.5f);
      if (x < work->curX0[y]) work->curX0[y] = (int16_t)x;
      if (x > work->curX1[y]) work->curX1[y] = (int16_t)x;
    }
  }
  if (exactPx) *exactPx = nExact;
  return F.rect;
}

#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC pop_options
#endif

#endif  // FACE_ATLAS_RT_H
