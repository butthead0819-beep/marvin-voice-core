// face_rt.h — tools/puck_face/face_pixel.js（本身是 tools/puck_face/pixel_mockup.html
// 「2. 逐像素版」那段的移植）的 C++ 版，S3 跟 host 共用，規格以 JS 為準。
//
// 允許的優化（都只是跳過不會畫到東西的計算，不改變任何像素的最終結果）：
// 1. 4×4 tile 剔除——取代逐像素子框跳過。每個 tile 先用中心點估算到各筆畫的螢幕距離，
//    距離夠遠就整個 tile 跳過數學；夠近或在球緣（一階近似不可靠）才逐像素算，且只算
//    mask 裡標到的筆畫。
// 2. asin 查表＋cos 改 sqrt（數學上等價：cos(asin(x)) = sqrt(1-x^2)）。
//
// 使用前必須先呼叫一次 face_rt_init()（填 asin 查表），否則查表是全 0。
#ifndef FACE_RT_H
#define FACE_RT_H

#include <math.h>
#include <stdint.h>
#include <string.h>

// S3 上 Arduino 預設 -Os，sqrtf/除法走慢路徑；這個檔案單獨改用 O2＋fast-math
// （host 測試證實輸出跟 JS 標準答案仍在門檻內）。clang（Mac host 測試）不認這個 pragma，跳過。
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC push_options
#pragma GCC optimize("O2", "fast-math")
#endif

#ifdef FACE_RT_STATS
// 測試用：這一輪 render 裡實際做了完整數學（tile mask 非 0、且通過 clip/sl/u 檢查）的像素數。
extern uint32_t g_face_rt_full_px;
#endif

struct FacePose {
  float yaw, pitch, roll, browUp, err;
};

struct FaceRect {
  int16_t x0, y0, x1, y1;  // 含端點
};

namespace face_rt_detail {

constexpr int W = 240, C = 120, R = 108;
constexpr float HALF = 1.5707963267948966f;  // M_PI/2
constexpr float IDLE[3] = {104, 158, 58};
constexpr float ERR_COL[3] = {230, 90, 55};
constexpr float PHI_C = 0.72f, DW = 0.27f, LAM_C = 0.15f, DH_A = 0.26f;
constexpr float BROW_CURVE = 0.035f;
constexpr float PHI_END = PHI_C + DW + 0.26f;
constexpr float RIM_W = 108 * 0.035f > 2.0f ? 108 * 0.035f : 2.0f;
constexpr float BROW_W_ = 108 * 0.02f > 1.5f ? 108 * 0.02f : 1.5f;
constexpr float LIMB_MIN = 0.15f;
constexpr float RIM_COL[3] = {8, 10, 9};
constexpr float BROW_COL[3] = {16, 19, 17};
constexpr float BLACK[3] = {0, 0, 0};

inline float clamp01(float x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }
inline float clampH(float v) { return v < -HALF ? -HALF : (v > HALF ? HALF : v); }
inline float hyp(float a, float b) { return sqrtf(a * a + b * b); }

// asin 查表：i/256 → asinf(i/256)，i=0..256。face_rt_init() 填值，用前必須呼叫。
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
  float rimPx[2], rimPy[2], rimQx[2], rimQy[2];  // rim[0]=(A,Cc), rim[1]=(Cc,B)
  float sx, sy, topY;
};

struct Prepared {
  float dark45[3], dark1[3], dark42[3], bright[3];
  float cr, sr, yaw, browL0;
  Eye eyes[2];
  FaceRect rect;
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

inline float segDistPx(float phi, float lam, float Px, float Py, float Qx, float Qy, const Jacobian& J) {
  float vx = Qx - Px, vy = Qy - Py, wx = phi - Px, wy = lam - Py;
  float Vx = J.a * vx + J.b * vy, Vy = J.d * vy;
  float Wx = J.a * wx + J.b * wy, Wy = J.d * wy;
  float denom = Vx * Vx + Vy * Vy;
  float t = clamp01((Wx * Vx + Wy * Vy) / denom);
  return hyp(Wx - Vx * t, Wy - Vy * t);
}

// 把未旋轉座標 (ux,uy) 轉成螢幕座標（繞 (C,C) 轉 roll）
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
  const float pad = 4;

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
    // rim: [A,Cc], [Cc,B]
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
    for (int seg = 0; seg < 3; seg++) {
      for (int i = 0; i <= 8; i++) {
        float t = i / 8.0f;
        float qx, qy;
        projF(Px[seg] + (Qx[seg] - Px[seg]) * t, Py[seg] + (Qy[seg] - Py[seg]) * t, qx, qy);
        if (qy < topY) topY = qy;
        float sxp, syp;
        toScreen(F.cr, F.sr, qx, qy, sxp, syp);
        expandBBox(allX0, allY0, allX1, allY1, sxp, syp);
      }
    }
    F.eyes[ei].topY = topY;
  }

  {
    for (int i = 0; i <= 24; i++) {
      float s = -1 + i / 12.0f;
      float qx, qy;
      projF(p.yaw + s * PHI_END, F.browL0 + BROW_CURVE * s * s, qx, qy);
      float sxp, syp;
      toScreen(F.cr, F.sr, qx, qy, sxp, syp);
      expandBBox(allX0, allY0, allX1, allY1, sxp, syp);
    }
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

// 用前必須呼叫一次：填 asin 查表。
inline void face_rt_init() {
  for (int i = 0; i <= 256; i++) {
    face_rt_detail::ASIN_LUT[i] = asinf(i / 256.0f);
  }
}

inline FaceRect face_rt_render(const FacePose& p, const uint8_t* base888, uint8_t* out888) {
  using namespace face_rt_detail;

  float e = clamp01(p.err);
  float col[3] = {
    IDLE[0] + (ERR_COL[0] - IDLE[0]) * e,
    IDLE[1] + (ERR_COL[1] - IDLE[1]) * e,
    IDLE[2] + (ERR_COL[2] - IDLE[2]) * e,
  };

  Prepared F;
  prepare(p, col, F);

  const int x0 = F.rect.x0, y0 = F.rect.y0, x1 = F.rect.x1, y1 = F.rect.y1;

  for (int y = y0; y <= y1; y++) {
    memcpy(out888 + (size_t)(y * W + x0) * 3, base888 + (size_t)(y * W + x0) * 3, (size_t)(x1 - x0 + 1) * 3);
  }

  constexpr float TOL = 1.5f;  // 一階近似誤差餘裕

  for (int tiley = y0; tiley <= y1; tiley += 4) {
    int tiley1 = tiley + 3 < y1 ? tiley + 3 : y1;
    for (int tilex = x0; tilex <= x1; tilex += 4) {
      int tilex1 = tilex + 3 < x1 ? tilex + 3 : x1;

      float tw = (float)(tilex1 - tilex + 1), th = (float)(tiley1 - tiley + 1);
      float tcx = (tilex + tilex1 + 1) * 0.5f, tcy = (tiley + tiley1 + 1) * 0.5f;
      float hd = 0.5f * sqrtf(tw * tw + th * th);

      float cdx = tcx - C, cdy = tcy - C;
      float cux = F.cr * cdx + F.sr * cdy, cuy = -F.sr * cdx + F.cr * cdy;
      if (hyp(cux, cuy) > R + hd + 1) continue;  // tile 必定在球外，逐像素一定被 clip 掉

      int mask;
      float csl = cuy / R;
      if (csl <= -1 || csl >= 1) {
        mask = 7;
      } else {
        float clam = fast_asin(csl), ccl = sqrtf(1 - csl * csl);
        float cu = cux / (R * ccl);
        if (cu <= -1 || cu >= 1) {
          mask = 7;
        } else {
          float cphi = fast_asin(cu), ccp = sqrtf(1 - cu * cu);
          if (ccl < 0.35f || ccp < 0.35f) {
            mask = 7;  // 靠近球緣，一階近似不可靠，不剔除
          } else {
            float csp = cu;
            float cclg = ccl > LIMB_MIN ? ccl : LIMB_MIN, ccpg = ccp > LIMB_MIN ? ccp : LIMB_MIN;
            float cdphi_dx = 1.0f / (R * cclg * ccpg);
            float cdphi_dy = cux * csl / (R * R * cclg * cclg * cclg * ccpg);
            float cdlam_dy = 1.0f / (R * cclg);
            Jacobian cJ;
            cJ.a = R * cclg * ccpg;
            cJ.b = -R * csl * csp;
            cJ.d = R * cclg;

            mask = 0;
            for (int ei = 0; ei < 2; ei++) {
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

      if (mask == 0) continue;  // tile 內所有像素都維持 base

      for (int y = tiley; y <= tiley1; y++) {
        for (int x = tilex; x <= tilex1; x++) {
          float px = x + 0.5f, py = y + 0.5f;

          // 像素中心 → 反轉 roll → 未旋轉座標
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

          // 螢幕 → 角度的偏微分（算邊緣平滑用）
          // 球緣 cos→0 時一階近似會失準（遠處的形狀被算成 0 距離，在球緣留下一圈色暈），所以 cos 設下限
          float clg = cl > LIMB_MIN ? cl : LIMB_MIN, cpg = cp > LIMB_MIN ? cp : LIMB_MIN;
          float dphi_dx = 1.0f / (R * clg * cpg);
          float dphi_dy = ux * sl / (R * R * clg * clg * clg * cpg);
          float dlam_dy = 1.0f / (R * clg);
          // 角度 → 螢幕（算描邊距離用）：dX=a·dφ+b·dλ, dY=d·dλ
          Jacobian J;
          J.a = R * clg * cpg;
          J.b = -R * sl * sp;
          J.d = R * clg;

          size_t i = (size_t)(y * W + x) * 3;
          float c[3] = {(float)out888[i], (float)out888[i + 1], (float)out888[i + 2]};

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
            // 眉線：角度空間的拋物線 λ = L0 + curve·s²，s=(φ-yaw)/PHI_END
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
    }
  }

  return F.rect;
}

#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC pop_options
#endif

#endif  // FACE_RT_H
