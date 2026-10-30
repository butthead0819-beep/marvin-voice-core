// face_rt.h — tools/puck_face/face_pixel.js 的 C++ 版，S3 跟 host 共用。
// 支援 240×240 (預設) 與 466×466 自適應（可定義 FACE_RT_W 466）
// 支援 FACE_RT_FAST（預設啟用）：DDA 角度累加 + Tile 中心局部導數凍結 + 座標 DDA
#ifndef FACE_RT_H
#define FACE_RT_H

#include <math.h>
#include <stdint.h>
#include <string.h>

#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC push_options
#pragma GCC optimize("O2", "fast-math")
#endif

#ifndef FACE_RT_W
#define FACE_RT_W 240
#endif

#ifndef FACE_RT_FAST
#if FACE_RT_W == 466
#define FACE_RT_FAST 1
#else
#define FACE_RT_FAST 0
#endif
#endif

#ifdef FACE_RT_STATS
extern uint32_t g_face_rt_full_px;
static uint32_t g_face_rt_linear_px = 0;
#endif

#ifdef FACE_RT_CYCLES
struct FaceRtProfile {
  uint32_t prepare, copy, tiles, pixels, frames, tiles_total, tiles_masked, px_math;
  uint32_t tile_bbox;
  uint32_t tile_center;
  uint32_t tile_corner;
  uint32_t tiles_linear, tiles_exact;
};
extern FaceRtProfile g_face_rt_prof;
#endif

struct FacePose {
  float yaw, pitch, roll, browUp, err;
};

struct FaceRect {
  int16_t x0, y0, x1, y1;
};

constexpr int MAX_COLS = 64;

struct FaceRtWork;

namespace face_rt_detail {

#if FACE_RT_W == 466
constexpr int W = 466, C = 233;
constexpr float R = 209.7f;
constexpr int TILE_SZ = 8;
constexpr float TOL = 2.5f;
constexpr float RIM_W = 209.7f * 0.035f;
constexpr float BROW_W_ = 209.7f * 0.02f;
constexpr float PAD_PX = 8.0f;
#else
constexpr int W = 240, C = 120;
constexpr float R = 108.0f;
constexpr int TILE_SZ = 4;
constexpr float TOL = 1.5f;
constexpr float RIM_W = 108.0f * 0.035f > 2.0f ? 108.0f * 0.035f : 2.0f;
constexpr float BROW_W_ = 108.0f * 0.02f > 1.5f ? 108.0f * 0.02f : 1.5f;
constexpr float PAD_PX = 4.0f;
#endif

constexpr float HALF = 1.5707963267948966f;
constexpr float IDLE[3] = {104, 158, 58};
constexpr float ERR_COL[3] = {230, 90, 55};
constexpr float PHI_C = 0.72f, DW = 0.27f, LAM_C = 0.15f, DH_A = 0.26f;
constexpr float BROW_CURVE = 0.035f;
constexpr float PHI_END = PHI_C + DW + 0.26f;
constexpr float LIMB_MIN = 0.15f;
constexpr float RIM_COL[3] = {8, 10, 9};
constexpr float BROW_COL[3] = {16, 19, 17};
constexpr float BLACK[3] = {0, 0, 0};

inline float clamp01(float x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }
inline float clampH(float v) { return v < -HALF ? -HALF : (v > HALF ? HALF : v); }
inline float hyp(float a, float b) { return sqrtf(a * a + b * b); }

static float ASIN_LUT[257];

inline float fast_asin(float x) {
  float a = x < 0 ? -x : x;
  float sign = x < 0 ? -1.0f : 1.0f;
  if (a <= 0.98f) {
    float t = a * 256.0f;
    int i = (int)t;
    return sign * (ASIN_LUT[i] + (ASIN_LUT[i + 1] - ASIN_LUT[i]) * (t - i));
  }
  return asinf(x);
}

inline void projF(float phi, float lam, float& ox, float& oy) {
  phi = clampH(phi);
  lam = clampH(lam);
  ox = C + R * cosf(lam) * sinf(phi);
  oy = C + R * sinf(lam);
}

struct Edge {
  float nx, ny, c;
};

struct Eye {
  Edge edges[3];
  float rimPx[2], rimPy[2], rimQx[2], rimQy[2];
  float sx, sy, topY;
};

struct BBoxI {
  int16_t x0, y0, x1, y1;
};

inline bool bboxHitsTile(const BBoxI& b, int tx0, int ty0, int tx1, int ty1) {
  return tx0 <= b.x1 && tx1 >= b.x0 && ty0 <= b.y1 && ty1 >= b.y0;
}

inline BBoxI boxFromExtents(float X0, float Y0, float X1, float Y1, float pad) {
  int x0 = (int)floorf(X0 - pad), y0 = (int)floorf(Y0 - pad);
  int x1 = (int)ceilf(X1 + pad), y1 = (int)ceilf(Y1 + pad);
  if (x0 < 0) x0 = 0;
  if (y0 < 0) y0 = 0;
  if (x1 > W - 1) x1 = W - 1;
  if (y1 > W - 1) y1 = W - 1;
  BBoxI b;
  b.x0 = (int16_t)x0; b.y0 = (int16_t)y0; b.x1 = (int16_t)x1; b.y1 = (int16_t)y1;
  return b;
}

struct Prepared {
  float dark45[3], dark1[3], dark42[3], bright[3];
  float cr, sr, yaw, browL0;
  Eye eyes[2];
  FaceRect rect;
  BBoxI eyeBox[2], browBox;
};

inline void mix(float* c, const float* col, float a) {
  c[0] += (col[0] - c[0]) * a;
  c[1] += (col[1] - c[1]) * a;
  c[2] += (col[2] - c[2]) * a;
}

inline void gradColor(const Prepared& F, float t, float* out) {
  t = clamp01(t);
  const float *a, *b;
  float u;
  if (t < 0.4f) {
    a = F.bright; b = F.dark1; u = t / 0.4f;
  } else {
    a = F.dark1; b = F.dark42; u = (t - 0.4f) / 0.6f;
  }
  out[0] = a[0] + (b[0] - a[0]) * u;
  out[1] = a[1] + (b[1] - a[1]) * u;
  out[2] = a[2] + (b[2] - a[2]) * u;
}

struct Jacobian {
  float a, b, d;
};

struct Corner {
  bool ok;
  float Ja, Jb, Jd;
  float dphi_dx, dphi_dy, dlam_dy;
  float phi, lam;
};

static FaceRtWork* s_work = nullptr;

inline float segDistPx(float phi, float lam, float Px, float Py, float Qx, float Qy, const Jacobian& J) {
  float vx = Qx - Px, vy = Qy - Py, wx = phi - Px, wy = lam - Py;
  float Vx = J.a * vx + J.b * vy, Vy = J.d * vy;
  float Wx = J.a * wx + J.b * wy, Wy = J.d * wy;
  float denom = Vx * Vx + Vy * Vy;
  float t = clamp01((Wx * Vx + Wy * Vy) / denom);
  return hyp(Wx - Vx * t, Wy - Vy * t);
}

inline void toScreen(float cr, float sr, float ux, float uy, float& X, float& Y) {
  float dx = ux - C, dy = uy - C;
  X = C + cr * dx - sr * dy;
  Y = C + sr * dx + cr * dy;
}

inline void expandBBox(float& x0, float& y0, float& x1, float& y1, float X, float Y) {
  if (X < x0) x0 = X;
  if (X > x1) x1 = X;
  if (Y < y0) y0 = Y;
  if (Y > y1) y1 = Y;
}

inline void prepare(const FacePose& p, float col[3], Prepared& F) {
  float dark45k = 0.45f, dark1k = 1.0f, dark42k = 0.42f;
  for (int i = 0; i < 3; i++) {
    F.dark45[i] = (float)(int)(col[i] * dark45k);
    F.dark1[i] = (float)(int)(col[i] * dark1k);
    F.dark42[i] = (float)(int)(col[i] * dark42k);
  }
  float br0 = col[0] + 80, br1 = col[1] + 70, br2 = col[2] + 70;
  F.bright[0] = (float)(int)(br0 < 255 ? br0 : 255);
  F.bright[1] = (float)(int)(br1 < 255 ? br1 : 255);
  F.bright[2] = (float)(int)(br2 < 255 ? br2 : 255);
  F.cr = cosf(p.roll);
  F.sr = sinf(p.roll);
  F.yaw = p.yaw;
  F.browL0 = LAM_C + p.pitch - p.browUp;

  float allX0 = W, allY0 = W, allX1 = 0, allY1 = 0;
  const float pad = PAD_PX;

  for (int ei = 0; ei < 2; ei++) {
    float sign = ei == 0 ? -1.0f : 1.0f;
    float p0 = sign * PHI_C + p.yaw, lam0 = LAM_C + p.pitch;
    float Ax = p0 + sign * DW, Ay = lam0 + 0.05f;
    float Bx = p0 - sign * DW, By = lam0;
    float Ccx = p0, Ccy = lam0 + DH_A;
    float ctrx = (Ax + Bx + Ccx) / 3, ctry = (Ay + By + Ccy) / 3;

    float Px[3] = {Ax, Bx, Ccx}, Py[3] = {Ay, By, Ccy};
    float Qx[3] = {Bx, Ccx, Ax}, Qy[3] = {By, Ccy, Ay};
    for (int k = 0; k < 3; k++) {
      float nx = -(Qy[k] - Py[k]), ny = Qx[k] - Px[k];
      float L = hyp(nx, ny);
      nx /= L; ny /= L;
      if (nx * (ctrx - Px[k]) + ny * (ctry - Py[k]) < 0) { nx = -nx; ny = -ny; }
      F.eyes[ei].edges[k].nx = nx;
      F.eyes[ei].edges[k].ny = ny;
      F.eyes[ei].edges[k].c = -(nx * Px[k] + ny * Py[k]);
    }

    F.eyes[ei].rimPx[0] = Ax; F.eyes[ei].rimPy[0] = Ay;
    F.eyes[ei].rimQx[0] = Ccx; F.eyes[ei].rimQy[0] = Ccy;
    F.eyes[ei].rimPx[1] = Ccx; F.eyes[ei].rimPy[1] = Ccy;
    F.eyes[ei].rimQx[1] = Bx; F.eyes[ei].rimQy[1] = By;

    float PBx, PBy, PCx, PCy;
    projF(Bx, By, PBx, PBy);
    projF(Ccx, Ccy, PCx, PCy);
    F.eyes[ei].sx = (PBx + PCx) / 2 + sinf(clampH(p.yaw)) * R * 0.5f;
    F.eyes[ei].sy = (PBy + PCy) / 2;

    float topY = 1e30f;
    float eX0 = W, eY0 = W, eX1 = 0, eY1 = 0;
    for (int seg = 0; seg < 3; seg++) {
      for (int i = 0; i <= 8; i++) {
        float t = i / 8.0f;
        float qx, qy;
        projF(Px[seg] + (Qx[seg] - Px[seg]) * t, Py[seg] + (Qy[seg] - Py[seg]) * t, qx, qy);
        if (qy < topY) topY = qy;
        float sxp, syp;
        toScreen(F.cr, F.sr, qx, qy, sxp, syp);
        expandBBox(allX0, allY0, allX1, allY1, sxp, syp);
        expandBBox(eX0, eY0, eX1, eY1, sxp, syp);
      }
    }
    F.eyes[ei].topY = topY;
    F.eyeBox[ei] = boxFromExtents(eX0, eY0, eX1, eY1, pad);
  }

  {
    float brX0 = W, brY0 = W, brX1 = 0, brY1 = 0;
    for (int i = 0; i <= 24; i++) {
      float s = -1 + i / 12.0f;
      float qx, qy;
      projF(p.yaw + s * PHI_END, F.browL0 + BROW_CURVE * s * s, qx, qy);
      float sxp, syp;
      toScreen(F.cr, F.sr, qx, qy, sxp, syp);
      expandBBox(allX0, allY0, allX1, allY1, sxp, syp);
      expandBBox(brX0, brY0, brX1, brY1, sxp, syp);
    }
    F.browBox = boxFromExtents(brX0, brY0, brX1, brY1, pad);
  }

  int x0 = (int)floorf(allX0 - pad), y0 = (int)floorf(allY0 - pad);
  int x1 = (int)ceilf(allX1 + pad), y1 = (int)ceilf(allY1 + pad);
  if (x0 < 0) x0 = 0;
  if (y0 < 0) y0 = 0;
  if (x1 > W - 1) x1 = W - 1;
  if (y1 > W - 1) y1 = W - 1;
  F.rect.x0 = (int16_t)x0;
  F.rect.y0 = (int16_t)y0;
  F.rect.x1 = (int16_t)x1;
  F.rect.y1 = (int16_t)y1;
}

}  // namespace face_rt_detail

struct FaceRtWork {
  face_rt_detail::Corner cornerBuf[2][MAX_COLS];
  int16_t curX0[face_rt_detail::W], curX1[face_rt_detail::W];
  int16_t prevX0[face_rt_detail::W], prevX1[face_rt_detail::W];
};

inline void face_rt_init(FaceRtWork* work) {
  for (int i = 0; i <= 256; i++) {
    face_rt_detail::ASIN_LUT[i] = asinf(i / 256.0f);
  }
  for (int y = 0; y < face_rt_detail::W; y++) {
    work->curX0[y] = work->prevX0[y] = face_rt_detail::W;
    work->curX1[y] = work->prevX1[y] = -1;
  }
  face_rt_detail::s_work = work;
}

#ifndef FACE_RT_ATTR
#define FACE_RT_ATTR
#endif

FACE_RT_ATTR inline FaceRect face_rt_render(const FacePose& p, const uint8_t* base888, uint8_t* out888) {
  using namespace face_rt_detail;

#ifdef FACE_RT_CYCLES
  uint32_t _tc0 = FACE_RT_CYCLES();
#endif
  {
    int16_t (&curX0)[W] = s_work->curX0;
    int16_t (&curX1)[W] = s_work->curX1;
    int16_t (&prevX0)[W] = s_work->prevX0;
    int16_t (&prevX1)[W] = s_work->prevX1;
    for (int y = 0; y < W; y++) {
      prevX0[y] = curX0[y];
      prevX1[y] = curX1[y];
      if (prevX1[y] >= prevX0[y]) {
        size_t off = (size_t)(y * W + prevX0[y]) * 3;
        memcpy(out888 + off, base888 + off, (size_t)(prevX1[y] - prevX0[y] + 1) * 3);
      }
      curX0[y] = W;
      curX1[y] = -1;
    }
  }
#ifdef FACE_RT_CYCLES
  g_face_rt_prof.copy += FACE_RT_CYCLES() - _tc0;
#endif

  float e = clamp01(p.err);
  float col[3] = {
    IDLE[0] + (ERR_COL[0] - IDLE[0]) * e,
    IDLE[1] + (ERR_COL[1] - IDLE[1]) * e,
    IDLE[2] + (ERR_COL[2] - IDLE[2]) * e,
  };

  Prepared F;
#ifdef FACE_RT_CYCLES
  uint32_t _t0 = FACE_RT_CYCLES();
#endif
  prepare(p, col, F);
#ifdef FACE_RT_CYCLES
  uint32_t _t1 = FACE_RT_CYCLES();
  g_face_rt_prof.prepare += _t1 - _t0;
#endif

  const int x0 = F.rect.x0, y0 = F.rect.y0, x1 = F.rect.x1, y1 = F.rect.y1;

#ifdef FACE_RT_CYCLES
  g_face_rt_prof.frames++;
#endif

  Corner (&s_cornerBuf)[2][MAX_COLS] = s_work->cornerBuf;
  uint64_t computedBit[2] = {0, 0};
  int cornerTop = 0, cornerBot = 1;
  bool firstTileRow = true;

  auto getCorner = [&](int col, int buf, float xc, float yc) -> Corner {
    uint64_t bit = 1ull << col;
    if (!(computedBit[buf] & bit)) {
      computedBit[buf] |= bit;
      Corner cr;
      cr.ok = true;
      cr.Ja = cr.Jb = cr.Jd = 0.0f;
      cr.dphi_dx = cr.dphi_dy = cr.dlam_dy = 0.0f;
      cr.phi = cr.lam = 0.0f;
      float dx = xc - C, dy = yc - C;
      float ux = F.cr * dx + F.sr * dy, uy = -F.sr * dx + F.cr * dy;
      float sl = uy / R;
      if (sl <= -1.0f || sl >= 1.0f) {
        cr.ok = false;
      } else {
        float cl = sqrtf(1 - sl * sl);
        float u = ux / (R * cl);
        if (u <= -1.0f || u >= 1.0f) {
          cr.ok = false;
        } else {
          float cp = sqrtf(1 - u * u);
#if FACE_RT_FAST
          const float limbThresh = 0.22f;
#else
          const float limbThresh = 0.35f;
#endif
          if (cl < limbThresh || cp < limbThresh) {
            cr.ok = false;
          } else {
            float clg = cl > LIMB_MIN ? cl : LIMB_MIN, cpg = cp > LIMB_MIN ? cp : LIMB_MIN;
            cr.dphi_dx = 1.0f / (R * clg * cpg);
            cr.dphi_dy = ux * sl / (R * R * clg * clg * clg * cpg);
            cr.dlam_dy = 1.0f / (R * clg);
            cr.Ja = R * clg * cpg;
            cr.Jb = -R * sl * u;
            cr.Jd = R * clg;
            cr.phi = fast_asin(u);
            cr.lam = fast_asin(sl);
          }
        }
      }
      s_cornerBuf[buf][col] = cr;
    }
    return s_cornerBuf[buf][col];
  };

  for (int tiley = y0; tiley <= y1; tiley += TILE_SZ) {
    int tiley1 = tiley + (TILE_SZ - 1) < y1 ? tiley + (TILE_SZ - 1) : y1;

    if (!firstTileRow) {
      int tmp = cornerTop; cornerTop = cornerBot; cornerBot = tmp;
      computedBit[cornerBot] = 0;
    }
    firstTileRow = false;

    int colIdx = 0;
    for (int tilex = x0; tilex <= x1; tilex += TILE_SZ, colIdx++) {
      int tilex1 = tilex + (TILE_SZ - 1) < x1 ? tilex + (TILE_SZ - 1) : x1;

#ifdef FACE_RT_CYCLES
      g_face_rt_prof.tiles_total++;
      uint32_t _tt0 = FACE_RT_CYCLES();
#endif

      int bboxMask = 0;
      if (bboxHitsTile(F.eyeBox[0], tilex, tiley, tilex1, tiley1)) bboxMask |= 1;
      if (bboxHitsTile(F.eyeBox[1], tilex, tiley, tilex1, tiley1)) bboxMask |= 2;
      if (bboxHitsTile(F.browBox, tilex, tiley, tilex1, tiley1)) bboxMask |= 4;

#ifdef FACE_RT_CYCLES
      uint32_t _tbbox1 = FACE_RT_CYCLES();
      g_face_rt_prof.tile_bbox += _tbbox1 - _tt0;
#endif

      if (bboxMask == 0) {
#ifdef FACE_RT_CYCLES
        g_face_rt_prof.tiles += FACE_RT_CYCLES() - _tt0;
#endif
        continue;
      }

      float tw = (float)(tilex1 - tilex + 1), th = (float)(tiley1 - tiley + 1);
      float tcx = (tilex + tilex1 + 1) * 0.5f, tcy = (tiley + tiley1 + 1) * 0.5f;
      float hd = 0.5f * sqrtf(tw * tw + th * th);

      float cdx = tcx - C, cdy = tcy - C;
      float cux = F.cr * cdx + F.sr * cdy, cuy = -F.sr * cdx + F.cr * cdy;
      float centerDist = hyp(cux, cuy);
      if (centerDist > R + hd + 1) {
#ifdef FACE_RT_CYCLES
        uint32_t _now = FACE_RT_CYCLES();
        g_face_rt_prof.tile_center += _now - _tbbox1;
        g_face_rt_prof.tiles += _now - _tt0;
#endif
        continue;
      }

      bool centerLimb = false;
      float cphi = 0.0f, clam = 0.0f;
      float cdphi_dx = 0.0f, cdphi_dy = 0.0f, cdlam_dy = 0.0f;
      Jacobian cJ{0.0f, 0.0f, 0.0f};
      int mask;

      float csl = cuy / R;
      if (csl <= -1 || csl >= 1) {
        mask = bboxMask;
        centerLimb = true;
      } else {
        float clamv = fast_asin(csl), ccl = sqrtf(1 - csl * csl);
        float cu = cux / (R * ccl);
        if (cu <= -1 || cu >= 1) {
          mask = bboxMask;
          centerLimb = true;
        } else {
          float cphiv = fast_asin(cu), ccp = sqrtf(1 - cu * cu);
#if FACE_RT_FAST
          const float limbThresh = 0.22f;
#else
          const float limbThresh = 0.35f;
#endif
          if (ccl < limbThresh || ccp < limbThresh) {
            mask = bboxMask;
            centerLimb = true;
          } else {
            clam = clamv;
            cphi = cphiv;
            float csp = cu;
            float cclg = ccl > LIMB_MIN ? ccl : LIMB_MIN, ccpg = ccp > LIMB_MIN ? ccp : LIMB_MIN;
            cdphi_dx = 1.0f / (R * cclg * ccpg);
            cdphi_dy = cux * csl / (R * R * cclg * cclg * cclg * ccpg);
            cdlam_dy = 1.0f / (R * cclg);
            cJ.a = R * cclg * ccpg;
            cJ.b = -R * csl * csp;
            cJ.d = R * cclg;

            mask = 0;
            for (int ei = 0; ei < 2; ei++) {
              if (!(bboxMask & (1 << ei))) continue;
              const Eye& E = F.eyes[ei];
              float dfill = -1e30f;
              for (int k = 0; k < 3; k++) {
                const Edge& ed = E.edges[k];
                float v = ed.nx * cphi + ed.ny * clam + ed.c;
                float gx = ed.nx * cdphi_dx, gy = ed.nx * cdphi_dy + ed.ny * cdlam_dy;
                float d = -v / hyp(gx, gy);
                if (d > dfill) dfill = d;
              }
              float drim = 1e30f;
              for (int r = 0; r < 2; r++) {
                float d = segDistPx(cphi, clam, E.rimPx[r], E.rimPy[r], E.rimQx[r], E.rimQy[r], cJ);
                if (d < drim) drim = d;
              }
              if (dfill < hd + 0.5f + TOL || drim < hd + RIM_W / 2 + 0.5f + TOL) {
                mask |= (1 << ei);
              }
            }

            if (bboxMask & 4) {
              float cs = (cphi - F.yaw) / PHI_END;
              float cbd;
              if (cs < -1 || cs > 1) {
                float se = cs < 0 ? -1.0f : 1.0f;
                float dp = cphi - (F.yaw + se * PHI_END), dl = clam - (F.browL0 + BROW_CURVE);
                cbd = hyp(cJ.a * dp + cJ.b * dl, cJ.d * dl);
              } else {
                float m = 2 * BROW_CURVE * cs / PHI_END;
                float dl = clam - (F.browL0 + BROW_CURVE * cs * cs);
                float k = dl / (1 + m * m);
                cbd = hyp(cJ.a * (-m * k) + cJ.b * k, cJ.d * k);
              }
              if (cbd < hd + BROW_W_ / 2 + 0.5f + TOL) {
                mask |= 4;
              }
            }
          }
        }
      }

#ifdef FACE_RT_CYCLES
      uint32_t _tcenter1 = FACE_RT_CYCLES();
      g_face_rt_prof.tile_center += _tcenter1 - _tbbox1;
      if (mask != 0) g_face_rt_prof.tiles_masked++;
#endif

      if (mask == 0) {
#ifdef FACE_RT_CYCLES
        g_face_rt_prof.tiles += _tcenter1 - _tt0;
#endif
        continue;
      }

      {
        int16_t (&curX0)[W] = s_work->curX0;
        int16_t (&curX1)[W] = s_work->curX1;
        for (int y = tiley; y <= tiley1; y++) {
          if (tilex < curX0[y]) curX0[y] = (int16_t)tilex;
          if (tilex1 > curX1[y]) curX1[y] = (int16_t)tilex1;
        }
      }

      bool exact = centerLimb;
      Corner cTL{}, cTR{}, cBL{}, cBR{};
      if (!exact) {
        cTL = getCorner(colIdx, cornerTop, (float)tilex, (float)tiley);
        cTR = getCorner(colIdx + 1, cornerTop, (float)(tilex1 + 1), (float)tiley);
        cBL = getCorner(colIdx, cornerBot, (float)tilex, (float)(tiley1 + 1));
        cBR = getCorner(colIdx + 1, cornerBot, (float)(tilex1 + 1), (float)(tiley1 + 1));
        if (!cTL.ok || !cTR.ok || !cBL.ok || !cBR.ok) exact = true;
      }

      float invgC[4][2][3];
      float rimVxC[4][2][2], rimVyC[4][2][2], rimInvVVC[4][2][2];
      float invtw = 0.0f, invth = 0.0f;
#if FACE_RT_FAST
      float Ja_mean = 0, Jb_mean = 0, Jd_mean = 0;
      float mean_invg[2][3];
      float mean_Vx[2][2], mean_Vy[2][2], mean_invVV[2][2];
      bool tileAllInSphere = (centerDist + hd < R - 1.0f);
#endif

      if (!exact) {
        invtw = 1.0f / tw;
        invth = 1.0f / th;
        const Corner* corners[4] = {&cTL, &cTR, &cBL, &cBR};
        for (int ci = 0; ci < 4; ci++) {
          const Corner& cc = *corners[ci];
          for (int ei = 0; ei < 2; ei++) {
            if (!(mask & (1 << ei))) continue;
            const Eye& E = F.eyes[ei];
            for (int k = 0; k < 3; k++) {
              const Edge& ed = E.edges[k];
              float gx = ed.nx * cc.dphi_dx, gy = ed.nx * cc.dphi_dy + ed.ny * cc.dlam_dy;
              invgC[ci][ei][k] = 1.0f / sqrtf(gx * gx + gy * gy);
            }
            for (int r = 0; r < 2; r++) {
              float vx = E.rimQx[r] - E.rimPx[r], vy = E.rimQy[r] - E.rimPy[r];
              float Vx = cc.Ja * vx + cc.Jb * vy, Vy = cc.Jd * vy;
              rimVxC[ci][ei][r] = Vx;
              rimVyC[ci][ei][r] = Vy;
              rimInvVVC[ci][ei][r] = 1.0f / (Vx * Vx + Vy * Vy);
            }
          }
        }

#if FACE_RT_FAST
        Ja_mean = (cTL.Ja + cTR.Ja + cBL.Ja + cBR.Ja) * 0.25f;
        Jb_mean = (cTL.Jb + cTR.Jb + cBL.Jb + cBR.Jb) * 0.25f;
        Jd_mean = (cTL.Jd + cTR.Jd + cBL.Jd + cBR.Jd) * 0.25f;
        for (int ei = 0; ei < 2; ei++) {
          if (!(mask & (1 << ei))) continue;
          for (int k = 0; k < 3; k++) {
            mean_invg[ei][k] = (invgC[0][ei][k] + invgC[1][ei][k] + invgC[2][ei][k] + invgC[3][ei][k]) * 0.25f;
          }
          for (int r = 0; r < 2; r++) {
            mean_Vx[ei][r] = (rimVxC[0][ei][r] + rimVxC[1][ei][r] + rimVxC[2][ei][r] + rimVxC[3][ei][r]) * 0.25f;
            mean_Vy[ei][r] = (rimVyC[0][ei][r] + rimVyC[1][ei][r] + rimVyC[2][ei][r] + rimVyC[3][ei][r]) * 0.25f;
            mean_invVV[ei][r] = (rimInvVVC[0][ei][r] + rimInvVVC[1][ei][r] + rimInvVVC[2][ei][r] + rimInvVVC[3][ei][r]) * 0.25f;
          }
        }
#endif
      }

#ifdef FACE_RT_CYCLES
      uint32_t _tp0 = FACE_RT_CYCLES();
      g_face_rt_prof.tile_corner += _tp0 - _tcenter1;
      g_face_rt_prof.tiles += _tp0 - _tt0;
      if (exact) g_face_rt_prof.tiles_exact++; else g_face_rt_prof.tiles_linear++;
#endif

      if (exact) {
        for (int y = tiley; y <= tiley1; y++) {
          for (int x = tilex; x <= tilex1; x++) {
            float px = x + 0.5f, py = y + 0.5f;
            float dx = px - C, dy = py - C;
            float ux = F.cr * dx + F.sr * dy, uy = -F.sr * dx + F.cr * dy;
            float clip = clamp01(R - 0.5f - hyp(ux, uy) + 0.5f);
            if (clip <= 0) continue;
            float sl = uy / R;
            if (sl <= -1 || sl >= 1) continue;
            float lam = fast_asin(sl), cl = sqrtf(1 - sl * sl), u = ux / (R * cl);
            if (u <= -1 || u >= 1) continue;
            float phi = fast_asin(u), cp = sqrtf(1 - u * u), sp = u;

#ifdef FACE_RT_STATS
            g_face_rt_full_px++;
#endif
#ifdef FACE_RT_CYCLES
            g_face_rt_prof.px_math++;
#endif

            float clg = cl > LIMB_MIN ? cl : LIMB_MIN, cpg = cp > LIMB_MIN ? cp : LIMB_MIN;
            float dphi_dx = 1.0f / (R * clg * cpg);
            float dphi_dy = ux * sl / (R * R * clg * clg * clg * cpg);
            float dlam_dy = 1.0f / (R * clg);
            Jacobian J;
            J.a = R * clg * cpg;
            J.b = -R * sl * sp;
            J.d = R * clg;

            size_t i = (size_t)(y * W + x) * 3;
            float c[3] = {(float)base888[i], (float)base888[i + 1], (float)base888[i + 2]};

            for (int ei = 0; ei < 2; ei++) {
              if (!(mask & (1 << ei))) continue;
              const Eye& E = F.eyes[ei];
              float cov = 1;
              for (int k = 0; k < 3; k++) {
                const Edge& ed = E.edges[k];
                float v = ed.nx * phi + ed.ny * lam + ed.c;
                float gx = ed.nx * dphi_dx, gy = ed.nx * dphi_dy + ed.ny * dlam_dy;
                float g = clamp01(0.5f + v / hyp(gx, gy));
                cov = cov < g ? cov : g;
                if (cov <= 0) break;
              }
              if (cov > 0) {
                float k = cov * clip;
                mix(c, F.dark45, 0.98f * k);
                float g[3];
                gradColor(F, hyp(ux + C - E.sx, uy + C - E.sy) / (R * 0.46f), g);
                mix(c, g, 0.98f * k);
                float ty = (uy + C - (E.topY - R * 0.01f)) / (R * 0.17f);
                mix(c, BLACK, 0.5f * (1 - clamp01(ty)) * k);
              }
              float dmin = 1e30f;
              for (int r = 0; r < 2; r++) {
                float d = segDistPx(phi, lam, E.rimPx[r], E.rimPy[r], E.rimQx[r], E.rimQy[r], J);
                if (d < dmin) dmin = d;
              }
              float rc = clamp01(RIM_W / 2 - dmin + 0.5f);
              if (rc > 0) mix(c, RIM_COL, 0.96f * rc * clip);
            }

            if (mask & 4) {
              float s = (phi - F.yaw) / PHI_END;
              float bd;
              if (s < -1 || s > 1) {
                float se = s < 0 ? -1.0f : 1.0f;
                float dp = phi - (F.yaw + se * PHI_END), dl = lam - (F.browL0 + BROW_CURVE);
                bd = hyp(J.a * dp + J.b * dl, J.d * dl);
              } else {
                float m = 2 * BROW_CURVE * s / PHI_END;
                float dl = lam - (F.browL0 + BROW_CURVE * s * s);
                float k = dl / (1 + m * m);
                bd = hyp(J.a * (-m * k) + J.b * k, J.d * k);
              }
              float bc = clamp01(BROW_W_ / 2 - bd + 0.5f);
              if (bc > 0) mix(c, BROW_COL, 0.92f * bc * clip);
            }

            out888[i] = (uint8_t)(int)(c[0] + 0.5f);
            out888[i + 1] = (uint8_t)(int)(c[1] + 0.5f);
            out888[i + 2] = (uint8_t)(int)(c[2] + 0.5f);
          }
        }
      } else {
        // 線性路徑
        for (int y = tiley; y <= tiley1; y++) {
          float py = y + 0.5f;
          float fy = (py - tiley) * invth;

#if FACE_RT_FAST
          float phi_left = cTL.phi + (cBL.phi - cTL.phi) * fy;
          float phi_right = cTR.phi + (cBR.phi - cTR.phi) * fy;
          float lam_left = cTL.lam + (cBL.lam - cTL.lam) * fy;
          float lam_right = cTR.lam + (cBR.lam - cTR.lam) * fy;
          float dphi_step = (phi_right - phi_left) * invtw;
          float dlam_step = (lam_right - lam_left) * invtw;
          float cur_phi = phi_left + (0.5f * invtw) * (phi_right - phi_left);
          float cur_lam = lam_left + (0.5f * invtw) * (lam_right - lam_left);

          float dx0 = (tilex + 0.5f) - C;
          float dy0 = py - C;
          float cur_ux = F.cr * dx0 + F.sr * dy0;
          float cur_uy = -F.sr * dx0 + F.cr * dy0;
#endif

          for (int x = tilex; x <= tilex1; x++) {
            float px = x + 0.5f;

#if FACE_RT_FAST
            float Ja = Ja_mean, Jb = Jb_mean, Jd = Jd_mean;
            float ux = cur_ux, uy = cur_uy;
            cur_ux += F.cr; cur_uy -= F.sr;
            float phi = cur_phi, lam = cur_lam;
            cur_phi += dphi_step; cur_lam += dlam_step;

            float clip = 1.0f;
            if (!tileAllInSphere) {
              float r2 = ux * ux + uy * uy;
              if (r2 > (R - 1.0f) * (R - 1.0f)) {
                clip = clamp01(R - 0.5f - sqrtf(r2) + 0.5f);
                if (clip <= 0) continue;
              }
            }
#else
            float fx = (px - tilex) * invtw;
            float w00 = (1 - fx) * (1 - fy), w10 = fx * (1 - fy), w01 = (1 - fx) * fy, w11 = fx * fy;
            float Ja = cTL.Ja * w00 + cTR.Ja * w10 + cBL.Ja * w01 + cBR.Ja * w11;
            float Jb = cTL.Jb * w00 + cTR.Jb * w10 + cBL.Jb * w01 + cBR.Jb * w11;
            float Jd = cTL.Jd * w00 + cTR.Jd * w10 + cBL.Jd * w01 + cBR.Jd * w11;

            float dx = px - C, dy = py - C;
            float ux = F.cr * dx + F.sr * dy, uy = -F.sr * dx + F.cr * dy;
            float sl = uy * (1.0f / R);
            if (sl <= -1 || sl >= 1) continue;
            float lam = fast_asin(sl), cl = sqrtf(1 - sl * sl), u = ux / (R * cl);
            if (u <= -1 || u >= 1) continue;
            float phi = fast_asin(u);
            float r2 = ux * ux + uy * uy;
            float clip;
            if (r2 <= (R - 1.0f) * (R - 1.0f)) {
              clip = 1.0f;
            } else {
              clip = clamp01(R - 0.5f - sqrtf(r2) + 0.5f);
            }
            if (clip <= 0) continue;
#endif

#ifdef FACE_RT_STATS
            g_face_rt_full_px++;
            g_face_rt_linear_px++;
#endif
#ifdef FACE_RT_CYCLES
            g_face_rt_prof.px_math++;
#endif

            size_t i = (size_t)(y * W + x) * 3;
            float c[3] = {(float)base888[i], (float)base888[i + 1], (float)base888[i + 2]};

            for (int ei = 0; ei < 2; ei++) {
              if (!(mask & (1 << ei))) continue;
              const Eye& E = F.eyes[ei];
              float cov = 1;
              for (int k = 0; k < 3; k++) {
                const Edge& ed = E.edges[k];
                float v = ed.nx * phi + ed.ny * lam + ed.c;
#if FACE_RT_FAST
                float invg = mean_invg[ei][k];
#else
                float invg = invgC[0][ei][k] * w00 + invgC[1][ei][k] * w10 +
                             invgC[2][ei][k] * w01 + invgC[3][ei][k] * w11;
#endif
                float g = clamp01(0.5f + v * invg);
                cov = cov < g ? cov : g;
                if (cov <= 0) break;
              }
              if (cov > 0) {
                float k = cov * clip;
                mix(c, F.dark45, 0.98f * k);
                float g[3];
                gradColor(F, hyp(ux + C - E.sx, uy + C - E.sy) / (R * 0.46f), g);
                mix(c, g, 0.98f * k);
                float ty = (uy + C - (E.topY - R * 0.01f)) / (R * 0.17f);
                mix(c, BLACK, 0.5f * (1 - clamp01(ty)) * k);
              }

              float dmin2 = 1e30f;
              for (int r = 0; r < 2; r++) {
                float wx = phi - E.rimPx[r], wy = lam - E.rimPy[r];
                float Wx = Ja * wx + Jb * wy, Wy = Jd * wy;
#if FACE_RT_FAST
                float Vx = mean_Vx[ei][r];
                float Vy = mean_Vy[ei][r];
                float invVV = mean_invVV[ei][r];
#else
                float Vx = rimVxC[0][ei][r] * w00 + rimVxC[1][ei][r] * w10 +
                           rimVxC[2][ei][r] * w01 + rimVxC[3][ei][r] * w11;
                float Vy = rimVyC[0][ei][r] * w00 + rimVyC[1][ei][r] * w10 +
                           rimVyC[2][ei][r] * w01 + rimVyC[3][ei][r] * w11;
                float invVV = rimInvVVC[0][ei][r] * w00 + rimInvVVC[1][ei][r] * w10 +
                              rimInvVVC[2][ei][r] * w01 + rimInvVVC[3][ei][r] * w11;
#endif
                float t = clamp01((Wx * Vx + Wy * Vy) * invVV);
                float ddx = Wx - Vx * t, ddy = Wy - Vy * t;
                float d2 = ddx * ddx + ddy * ddy;
                if (d2 < dmin2) dmin2 = d2;
              }
              float T = RIM_W / 2 + 0.5f;
              if (dmin2 < T * T) {
                float d = sqrtf(dmin2);
                float rc = clamp01(RIM_W / 2 - d + 0.5f);
                if (rc > 0) mix(c, RIM_COL, 0.96f * rc * clip);
              }
            }

            if (mask & 4) {
              float s = (phi - F.yaw) / PHI_END;
              float Wx, Wy;
              if (s < -1 || s > 1) {
                float se = s < 0 ? -1.0f : 1.0f;
                float dp = phi - (F.yaw + se * PHI_END), dl = lam - (F.browL0 + BROW_CURVE);
                Wx = Ja * dp + Jb * dl;
                Wy = Jd * dl;
              } else {
                float m = 2 * BROW_CURVE * s / PHI_END;
                float dl = lam - (F.browL0 + BROW_CURVE * s * s);
                float k = dl / (1 + m * m);
                Wx = Ja * (-m * k) + Jb * k;
                Wy = Jd * k;
              }
              float d2 = Wx * Wx + Wy * Wy;
              float TB = BROW_W_ / 2 + 0.5f;
              if (d2 < TB * TB) {
                float d = sqrtf(d2);
                float bc = clamp01(BROW_W_ / 2 - d + 0.5f);
                if (bc > 0) mix(c, BROW_COL, 0.92f * bc * clip);
              }
            }

            out888[i] = (uint8_t)(int)(c[0] + 0.5f);
            out888[i + 1] = (uint8_t)(int)(c[1] + 0.5f);
            out888[i + 2] = (uint8_t)(int)(c[2] + 0.5f);
          }
        }
      }
#ifdef FACE_RT_CYCLES
      g_face_rt_prof.pixels += FACE_RT_CYCLES() - _tp0;
#endif
    }
  }

  return F.rect;
}

#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC pop_options
#endif

#endif  // FACE_RT_H
