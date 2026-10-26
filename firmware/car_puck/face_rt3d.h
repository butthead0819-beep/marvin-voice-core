// face_rt3d.h — tools/puck_face/face_3d.js（本身是 tools/puck_face/sphere3d_mockup.html
// 「2b. 3D 內積版」的 Node 版）的 C++ 版，S3 跟 host 共用，規格以 JS 為準。
//
// 跟 face_rt.h 的差別：角色是純資料（Face3dChar），算法是球面 3D 內積（每像素一次 sqrt 求球面 z、
// 每條邊一次內積加一次 hypot），沒有 asin 查表、沒有 tile 線性化。只借 face_rt.h 的 FacePose／FaceRect
// 型別（不呼叫它的任何東西）；內部符號全放 face_rt3d_detail，不跟 face_rt.h 撞名。
//
// 唯一允許的優化（只跳過不會畫到東西的計算，不改變任何像素的最終結果）：
// 每筆畫（眼 0、眼 1、眉線）各自的螢幕外框（bbox）粗篩——prepare 時用該筆畫自己的取樣點算外框，
// 外擴 pad = 4 + ceil(max(RIM_W,BROW_W)/2)，再跟整體 rect 取交集。像素迴圈逐列、逐像素先看有哪些
// 筆畫的外框涵蓋，不在外框內的筆畫整個跳過；三個都不涵蓋的像素連 sqrt 都不算。
// 其他（查表、tile、近似 rsqrt）一律不加。
//
// 使用前必須先呼叫一次 face3d_init(&work, W)（記住 W、清空每列範圍）。
// work 是一塊 sizeof(Face3dWork) 大小、生命週期涵蓋所有 face3d_render() 呼叫的記憶體，
// 由呼叫端配置（S3 建議 PSRAM；face_rt3d.h 本身不配置 heap）。W 最大 FACE3D_MAX_W。
//
// work->curX0/curX1/prevX0/prevX1：render 完後，每列實際被畫到的像素範圍（這一格／上一格）。
// 呼叫端只要推「prev 聯集 cur」每列範圍，就能讓螢幕畫面跟 out888 一致（因為 render 開頭
// 會把上一格畫過、這一格沒碰到的像素還原成 base，那些還原也要推上螢幕）。
//
// 不變式（呼叫端必須保證）：第一次呼叫 face3d_render() 之前，out888 整張等於 base888。
// 之後 face3d_render() 自己維持「out888 只有 cur 範圍內可能不等於 base，其餘都等於 base」：
// 開頭先把 prev 範圍還原成 base，然後只寫被畫到的像素。沒畫到的像素不寫 out888。
#ifndef FACE_RT3D_H
#define FACE_RT3D_H

#include <math.h>
#include <stdint.h>
#include <string.h>

#include "face_rt.h"

// 同 face_rt.h：S3 上 Arduino 預設 -Os，sqrtf/除法走慢路徑，這個檔案單獨改用 O2＋fast-math。
// clang（Mac host 測試）不認這個 pragma，跳過。
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC push_options
#pragma GCC optimize("O2", "fast-math")
#endif

#ifdef FACE_RT3D_STATS
// 測試用：這一輪 render 裡通過 clip 檢查的像素數（由測試檔定義）。
extern uint32_t g_face_rt3d_px;
#endif

constexpr int FACE3D_MAX_W = 466;

// 角色資料，欄位意義同 face_3d.js 檔頭：
//   poly 眼：頂點 k 的經緯度 = [sign*cx + yaw + sign*verts[k][0], cy + pitch + verts[k][1]]，rim 是要描邊的邊（頂點索引對）
//   cap 眼：中心 = [sign*cx + yaw, cy + pitch]，radius 弧度，capRim 整圈描邊
//   眉線：L0 = browLam + pitch - browUp，三點 = [[yaw-half, L0+curve], [yaw, L0], [yaw+half, L0+curve]]
struct Face3dChar {
  uint8_t eyeKind;          // 0=poly 1=cap
  float cx, cy;
  uint8_t nVerts;           // poly：頂點數（最多 6，凸多邊形）
  float verts[6][2];
  uint8_t nRim;             // poly：描邊段數（最多 6）
  uint8_t rim[6][2];
  float radius;             // cap
  bool capRim;              // cap
  bool hasBrow;
  float browLam, browHalf, browCurve;
  float idle[3], err[3];
};

// 數值同 face_3d.js 的 MARVIN / ROUND_DEMO。
inline const Face3dChar FACE3D_MARVIN = {
  0, 0.72f, 0.15f,
  3, {{0.27f, 0.05f}, {-0.27f, 0.0f}, {0.0f, 0.26f}},
  2, {{0, 2}, {2, 1}},
  0.0f, false,
  true, 0.15f, 1.25f, 0.035f,
  {104, 158, 58}, {230, 90, 55}};
inline const Face3dChar FACE3D_ROUND_DEMO = {
  1, 0.40f, 0.05f,
  0, {},
  0, {},
  0.19f, true,
  false, 0.0f, 0.0f, 0.0f,
  {104, 158, 58}, {230, 90, 55}};

// face3d_render() 用的工作區：每列被畫到的像素範圍（這一格／上一格）。沒有則 X0=W, X1=-1。
struct Face3dWork {
  int W;
  int16_t curX0[FACE3D_MAX_W], curX1[FACE3D_MAX_W];
  int16_t prevX0[FACE3D_MAX_W], prevX1[FACE3D_MAX_W];
};

namespace face_rt3d_detail {

constexpr float HALF = 1.5707963267948966f;  // M_PI/2
constexpr float P2 = 6.283185307179586f;
constexpr int BOX_N = 16;            // 筆畫外框：每段曲線取樣數
constexpr float LIMB_BOX_Z = 0.35f;   // 筆畫外框：取樣點 z 低於這個就不做粗篩（見 prepare）
constexpr float LIMB_MIN = 0.15f;
constexpr float RIM_COL[3] = {8, 10, 9};
constexpr float BROW_COL[3] = {16, 19, 17};
constexpr float BIG = 1e30f;  // JS 的 Infinity；fast-math 下不用真的 inf

inline float clamp01(float x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }
inline float clampH(float v) { return v < -HALF ? -HALF : (v > HALF ? HALF : v); }
inline float hyp(float a, float b) { return sqrtf(a * a + b * b); }

struct V3 { float x, y, z; };
inline V3 v3(float phi, float lam) { float cl = cosf(lam); return {cl * sinf(phi), sinf(lam), cl * cosf(phi)}; }
inline float dot3(const V3& a, const V3& b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline V3 cross3(const V3& a, const V3& b) { return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x}; }
inline V3 nrm3(const V3& a) { float L = sqrtf(a.x * a.x + a.y * a.y + a.z * a.z); return {a.x / L, a.y / L, a.z / L}; }
inline V3 neg3(const V3& a) { return {-a.x, -a.y, -a.z}; }
inline V3 sub3(const V3& u, const V3& v) { return {u.x - v.x, u.y - v.y, u.z - v.z}; }

struct Rim {
  V3 n, a, b;
  float xi, yi, zi, xj, yj, zj;
};

struct Eye {
  uint8_t kind;  // 0=poly 1=cap
  int nEdges, nRims;
  V3 edges[6];
  Rim rims[6];
  V3 c;          // cap
  float cosr;
  bool rim;
  float sx, sy, topY;
};

struct Brow {
  V3 n;
  float d;
  V3 m1, m3;
  float x1, y1, z1, x3, y3, z3;
};

struct Prepared {
  float dark45[3], dark1[3], dark42[3], bright[3];
  float cr, sr;
  Eye eyes[2];
  bool hasBrow;
  Brow brow;
  FaceRect rect;
  // 每筆畫的螢幕外框（0,1=眼，2=眉線），已跟 rect 取交集；valid=false 表示沒有任何可見取樣點或交集為空
  bool bvalid[3];
  int bx0[3], by0[3], bx1[3], by1[3];
};

// 筆畫取樣點的螢幕外框累加器（旋轉後座標）
struct Acc {
  float x0, y0, x1, y1;
  bool any;
  Acc() : x0(0), y0(0), x1(0), y1(0), any(false) {}
  void add(float X, float Y) {
    if (!any) { x0 = x1 = X; y0 = y1 = Y; any = true; return; }
    if (X < x0) x0 = X;
    if (X > x1) x1 = X;
    if (Y < y0) y0 = Y;
    if (Y > y1) y1 = Y;
  }
};

// 每格先算好的東西（跟像素無關）：邊的法向量、描邊線段、眉線平面、漸層中心、陰影位置、重算區域、每筆畫外框
inline void prepare(const Face3dChar& ch, const FacePose& p, int W, const float col[3], int pad, Prepared& F) {
  const float C = W * 0.5f, R = W * 0.45f;
  auto projX = [&](float phi, float lam) { phi = clampH(phi); lam = clampH(lam); return C + R * cosf(lam) * sinf(phi); };
  auto projY = [&](float lam) { lam = clampH(lam); return C + R * sinf(lam); };
  auto dark = [&](float k, float* o) { for (int i = 0; i < 3; i++) o[i] = (float)(int)(col[i] * k); };
  dark(0.45f, F.dark45); dark(1.0f, F.dark1); dark(0.42f, F.dark42);
  F.bright[0] = (float)(int)(col[0] + 80 < 255 ? col[0] + 80 : 255);
  F.bright[1] = (float)(int)(col[1] + 70 < 255 ? col[1] + 70 : 255);
  F.bright[2] = (float)(int)(col[2] + 70 < 255 ? col[2] + 70 : 255);
  F.cr = cosf(p.roll);
  F.sr = sinf(p.roll);
  F.hasBrow = false;

  Acc all;                 // 整體（未旋轉座標下 z>0 的點，轉 roll 後的外框）
  Acc stroke[3];
  // 整體 rect 跟 JS 一樣，用經緯度直線取樣（addPt）。筆畫外框是 C++ 才有的粗篩，取樣要沿真正畫出來的
  // 3D 曲線（addBox）：邊是大圓弧、圓眼是球面小圓、眉線是過三點的小圓——466 下這些曲線跟經緯度直線
  // 差到超過 pad，用經緯度取樣會漏畫。
  // 筆畫有取樣點轉到球背面（z<=0）時，它的可見部分會一路延伸到球緣，但取樣點到不了球緣，
  // 這種筆畫不做粗篩，外框直接用整體 rect。
  bool behind[3] = {false, false, false};
  int cur = 0;             // 目前取樣點歸給哪筆畫
  auto addPt = [&](float phi, float lam) {
    V3 P = v3(phi, lam);
    if (P.z > 0) {
      float dx = C + R * P.x - C, dy = C + R * P.y - C;
      float X = C + F.cr * dx - F.sr * dy, Y = C + F.sr * dx + F.cr * dy;
      all.add(X, Y);
    }
  };
  auto addBox = [&](const V3& P) {
    // 靠近球緣（z 小）時，一階螢幕距離 v/|g| 會低估實際距離（nz 有 LIMB_MIN 下限），筆畫會往球緣暈開，
    // 畫到的範圍超出自己的曲線外框。實測門檻約 0.15（＝LIMB_MIN），取 0.35（同 face_rt.h 精確路徑門檻）留餘裕。
    if (P.z < LIMB_BOX_Z) behind[cur] = true;
    if (P.z > 0) {
      float dx = R * P.x, dy = R * P.y;
      stroke[cur].add(C + F.cr * dx - F.sr * dy, C + F.sr * dx + F.cr * dy);
    } else {
      behind[cur] = true;
    }
  };

  int ei = 0;
  for (int sgn = 0; sgn < 2; sgn++, ei++) {
    const float sign = sgn == 0 ? -1.0f : 1.0f;
    cur = ei;
    Eye& E = F.eyes[ei];
    E.kind = ch.eyeKind;
    if (ch.eyeKind == 0) {
      const int n = ch.nVerts;
      float V[6][2];
      V3 P[6];
      const float p0 = sign * ch.cx + p.yaw, lam0 = ch.cy + p.pitch;
      for (int k = 0; k < n; k++) {
        V[k][0] = p0 + sign * ch.verts[k][0];
        V[k][1] = lam0 + ch.verts[k][1];
        P[k] = v3(V[k][0], V[k][1]);
      }
      V3 sum = {0, 0, 0};
      for (int k = 0; k < n; k++) { sum.x += P[k].x; sum.y += P[k].y; sum.z += P[k].z; }
      const V3 ctr = nrm3(sum);
      E.nEdges = n;
      for (int k = 0; k < n; k++) {
        V3 nn = nrm3(cross3(P[k], P[(k + 1) % n]));
        E.edges[k] = dot3(nn, ctr) < 0 ? neg3(nn) : nn;
      }
      E.nRims = ch.nRim;
      for (int r = 0; r < ch.nRim; r++) {
        const int i = ch.rim[r][0], j = ch.rim[r][1];
        V3 nn = nrm3(cross3(P[i], P[j]));
        Rim& m = E.rims[r];
        m.n = nn;
        m.a = cross3(nn, P[i]);
        m.b = cross3(P[j], nn);
        m.xi = R * P[i].x; m.yi = R * P[i].y; m.zi = P[i].z;
        m.xj = R * P[j].x; m.yj = R * P[j].y; m.zj = P[j].z;
      }
      const float PBx = projX(V[1][0], V[1][1]), PBy = projY(V[1][1]);
      const float PCx = projX(V[2][0], V[2][1]), PCy = projY(V[2][1]);
      E.sx = (PBx + PCx) / 2 + sinf(clampH(p.yaw)) * R * 0.5f;
      E.sy = (PBy + PCy) / 2;
      float topY = BIG;
      for (int k = 0; k < n; k++) {
        const float* A = V[k];
        const float* B = V[(k + 1) % n];
        for (int i = 0; i <= 8; i++) {
          const float t = i / 8.0f;
          const float ph = A[0] + (B[0] - A[0]) * t, la = A[1] + (B[1] - A[1]) * t;
          const float qy = projY(la);
          if (qy < topY) topY = qy;
          addPt(ph, la);
        }
      }
      E.topY = topY;
      for (int k = 0; k < n; k++) {
        const V3& A = P[k];
        const V3& B = P[(k + 1) % n];
        for (int i = 0; i <= BOX_N; i++) {
          const float t = (float)i / BOX_N;
          addBox(nrm3({A.x + (B.x - A.x) * t, A.y + (B.y - A.y) * t, A.z + (B.z - A.z) * t}));
        }
      }
    } else {
      const float cen0 = sign * ch.cx + p.yaw, cen1 = ch.cy + p.pitch;
      E.c = v3(cen0, cen1);
      E.cosr = cosf(ch.radius);
      E.rim = ch.capRim;
      const float pcx = projX(cen0, cen1), pcy = projY(cen1);
      E.sx = pcx - R * 0.06f;
      E.sy = pcy - R * 0.06f;
      E.topY = pcy - R * sinf(ch.radius);
      for (int i = 0; i < 24; i++) {
        const float t = i / 24.0f * P2;
        addPt(cen0 + ch.radius * cosf(t), cen1 + ch.radius * sinf(t));
      }
      const V3& c = E.c;
      const V3 u = nrm3(cross3(c, fabsf(c.x) < 0.9f ? V3{1, 0, 0} : V3{0, 1, 0}));
      const V3 w = cross3(c, u);
      const float sr = sinf(ch.radius);
      for (int i = 0; i < 2 * BOX_N; i++) {
        const float t = (float)i / (2 * BOX_N) * P2, ct = cosf(t) * sr, st = sinf(t) * sr;
        addBox({c.x * E.cosr + u.x * ct + w.x * st, c.y * E.cosr + u.y * ct + w.y * st, c.z * E.cosr + u.z * ct + w.z * st});
      }
    }
  }
  if (ch.hasBrow) {
    cur = 2;
    const float L0 = ch.browLam + p.pitch - p.browUp;
    const float Q[3][2] = {{p.yaw - ch.browHalf, L0 + ch.browCurve}, {p.yaw, L0}, {p.yaw + ch.browHalf, L0 + ch.browCurve}};
    V3 P[3];
    for (int k = 0; k < 3; k++) P[k] = v3(Q[k][0], Q[k][1]);
    Brow& B = F.brow;
    B.n = nrm3(cross3(sub3(P[1], P[0]), sub3(P[2], P[0])));
    B.d = -dot3(B.n, P[0]);
    B.m1 = cross3(B.n, P[0]); if (dot3(B.m1, P[1]) < 0) B.m1 = neg3(B.m1);
    B.m3 = cross3(B.n, P[2]); if (dot3(B.m3, P[1]) < 0) B.m3 = neg3(B.m3);
    B.x1 = R * P[0].x; B.y1 = R * P[0].y; B.z1 = P[0].z;
    B.x3 = R * P[2].x; B.y3 = R * P[2].y; B.z3 = P[2].z;
    F.hasBrow = true;
    for (int s = 0; s < 2; s++) {
      for (int i = 0; i <= 12; i++) {
        const float t = i / 12.0f;
        addPt(Q[s][0] + (Q[s + 1][0] - Q[s][0]) * t, Q[s][1] + (Q[s + 1][1] - Q[s][1]) * t);
      }
    }
    {
      const V3 o = {-B.d * B.n.x, -B.d * B.n.y, -B.d * B.n.z};   // 小圓圓心
      const V3 r0 = sub3(P[0], o);
      const float rho = sqrtf(dot3(r0, r0));
      const V3 u = nrm3(r0), w = cross3(B.n, u);
      auto ang = [&](const V3& q) { const V3 d = sub3(q, o); float a = atan2f(dot3(d, w), dot3(d, u)); return a < 0 ? a + P2 : a; };
      const float a1 = ang(P[1]), a2 = ang(P[2]);
      const float span = a1 < a2 ? a2 : a2 - P2;   // 從 P0 出發、經過 P1 抵達 P2 的方向
      for (int i = 0; i <= 2 * BOX_N; i++) {
        const float a = span * i / (2 * BOX_N), ca = cosf(a) * rho, sa = sinf(a) * rho;
        addBox({o.x + u.x * ca + w.x * sa, o.y + u.y * ca + w.y * sa, o.z + u.z * ca + w.z * sa});
      }
    }
  }

  // 整體 rect：沒有任何可見點時沿用 JS 的初值（x0=y0=W, x1=y1=0）
  float rx0 = W, ry0 = W, rx1 = 0, ry1 = 0;
  if (all.any) { rx0 = all.x0; ry0 = all.y0; rx1 = all.x1; ry1 = all.y1; }
  const float padF = 4.0f;
  F.rect.x0 = (int16_t)(floorf(rx0 - padF) > 0 ? floorf(rx0 - padF) : 0);
  F.rect.y0 = (int16_t)(floorf(ry0 - padF) > 0 ? floorf(ry0 - padF) : 0);
  F.rect.x1 = (int16_t)(ceilf(rx1 + padF) < W - 1 ? ceilf(rx1 + padF) : W - 1);
  F.rect.y1 = (int16_t)(ceilf(ry1 + padF) < W - 1 ? ceilf(ry1 + padF) : W - 1);

  // 每筆畫外框 = 該筆畫取樣點外框外擴 pad，再跟整體 rect 取交集
  for (int s = 0; s < 3; s++) {
    F.bvalid[s] = false;
    if (behind[s]) {
      if (F.rect.x0 > F.rect.x1 || F.rect.y0 > F.rect.y1) continue;
      F.bvalid[s] = true;
      F.bx0[s] = F.rect.x0; F.by0[s] = F.rect.y0; F.bx1[s] = F.rect.x1; F.by1[s] = F.rect.y1;
      continue;
    }
    if (!stroke[s].any) continue;
    int bx0 = (int)floorf(stroke[s].x0 - pad), by0 = (int)floorf(stroke[s].y0 - pad);
    int bx1 = (int)ceilf(stroke[s].x1 + pad), by1 = (int)ceilf(stroke[s].y1 + pad);
    if (bx0 < F.rect.x0) bx0 = F.rect.x0;
    if (by0 < F.rect.y0) by0 = F.rect.y0;
    if (bx1 > F.rect.x1) bx1 = F.rect.x1;
    if (by1 > F.rect.y1) by1 = F.rect.y1;
    if (bx0 > bx1 || by0 > by1) continue;
    F.bvalid[s] = true;
    F.bx0[s] = bx0; F.by0[s] = by0; F.bx1[s] = bx1; F.by1[s] = by1;
  }
}

inline void mix(float* c, const float* col, float a) {
  c[0] += (col[0] - c[0]) * a;
  c[1] += (col[1] - c[1]) * a;
  c[2] += (col[2] - c[2]) * a;
}

inline void gradColor(const Prepared& F, float t, float* out) {
  t = clamp01(t);
  const float *a, *b;
  float u;
  if (t < 0.4f) { a = F.bright; b = F.dark1; u = t / 0.4f; } else { a = F.dark1; b = F.dark42; u = (t - 0.4f) / 0.6f; }
  out[0] = a[0] + (b[0] - a[0]) * u;
  out[1] = a[1] + (b[1] - a[1]) * u;
  out[2] = a[2] + (b[2] - a[2]) * u;
}

// 線性函數 v(p)=n·p 在螢幕上的有號距離（像素）
inline float sdist(const V3& n, float v, float kx, float ky, float R) {
  float gx = (n.x - n.z * kx) / R, gy = (n.y - n.z * ky) / R;
  return v / hyp(gx, gy);
}

}  // namespace face_rt3d_detail

// 用前必須呼叫一次：記住 W，cur/prev 全設成空（X0=W, X1=-1）。
inline void face3d_init(Face3dWork* work, int W) {
  work->W = W;
  for (int y = 0; y < W; y++) {
    work->curX0[y] = work->prevX0[y] = (int16_t)W;
    work->curX1[y] = work->prevX1[y] = -1;
  }
}

inline FaceRect face3d_render(const Face3dChar& ch, const FacePose& p,
                              const uint8_t* base888, uint8_t* out888, Face3dWork* work) {
  using namespace face_rt3d_detail;
  const int W = work->W;

  // 把上一格的畫過範圍（cur）存成 prev，再用 base 還原那些範圍；cur 清空，等下面逐像素填回。
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

  const float e = clamp01(p.err);
  float col[3] = {
    ch.idle[0] + (ch.err[0] - ch.idle[0]) * e,
    ch.idle[1] + (ch.err[1] - ch.idle[1]) * e,
    ch.idle[2] + (ch.err[2] - ch.idle[2]) * e,
  };

  Prepared F;
  prepare(ch, p, W, col, pad, F);
  const int x0 = F.rect.x0, y0 = F.rect.y0, x1 = F.rect.x1, y1 = F.rect.y1;

  float c[3], g[3];
  V3 p3;
  float kx = 0, ky = 0;
  for (int y = y0; y <= y1; y++) {
    // 這一列有哪些筆畫的外框涵蓋（y 範圍），以及它們 x 範圍的聯集
    bool rowAct[3];
    int rx0 = x1 + 1, rx1 = x0 - 1;
    bool any = false;
    for (int s = 0; s < 3; s++) {
      rowAct[s] = F.bvalid[s] && y >= F.by0[s] && y <= F.by1[s];
      if (!rowAct[s]) continue;
      any = true;
      if (F.bx0[s] < rx0) rx0 = F.bx0[s];
      if (F.bx1[s] > rx1) rx1 = F.bx1[s];
    }
    if (!any) continue;

    for (int x = rx0; x <= rx1; x++) {
      bool act[3];
      for (int s = 0; s < 3; s++) act[s] = rowAct[s] && x >= F.bx0[s] && x <= F.bx1[s];
      if (!(act[0] || act[1] || act[2])) continue;

      const float dx = x + 0.5f - C, dy = y + 0.5f - C;
      const float ux = F.cr * dx + F.sr * dy, uy = -F.sr * dx + F.cr * dy;
      const float clip = clamp01(R - 0.5f - hyp(ux, uy) + 0.5f);
      if (clip <= 0) continue;
#ifdef FACE_RT3D_STATS
      ++g_face_rt3d_px;
#endif
      const float nx = ux / R, ny = uy / R, r2 = nx * nx + ny * ny;
      if (r2 >= 1) continue;
      const float nz = sqrtf(1 - r2), nzg = nz > LIMB_MIN ? nz : LIMB_MIN;
      kx = nx / nzg;
      ky = ny / nzg;
      p3 = {nx, ny, nz};
      const size_t i = (size_t)(y * W + x) * 3;
      c[0] = out888[i]; c[1] = out888[i + 1]; c[2] = out888[i + 2];
      bool drew = false;
      for (int k = 0; k < 2; k++) {
        if (!act[k]) continue;
        const Eye& E = F.eyes[k];
        float cov, dmin = BIG;
        if (E.kind == 0) {
          cov = 1;
          for (int q = 0; q < E.nEdges; q++) {
            const V3& n = E.edges[q];
            const float v = clamp01(0.5f + sdist(n, dot3(n, p3), kx, ky, R));
            if (v < cov) cov = v;
            if (cov <= 0) break;
          }
          for (int q = 0; q < E.nRims; q++) {
            const Rim& m = E.rims[q];
            float d;
            if (dot3(m.a, p3) >= 0 && dot3(m.b, p3) >= 0) {
              d = sdist(m.n, dot3(m.n, p3), kx, ky, R);
              if (d < 0) d = -d;
            } else {
              const float di = m.zi > 0 ? hyp(ux - m.xi, uy - m.yi) : BIG;
              const float dj = m.zj > 0 ? hyp(ux - m.xj, uy - m.yj) : BIG;
              d = di < dj ? di : dj;
            }
            if (d < dmin) dmin = d;
          }
        } else {
          const float s = sdist(E.c, dot3(E.c, p3) - E.cosr, kx, ky, R);
          cov = clamp01(0.5f + s);
          if (E.rim) dmin = s < 0 ? -s : s;
        }
        if (cov > 0) {
          drew = true;
          const float kk = cov * clip;
          mix(c, F.dark45, 0.98f * kk);
          gradColor(F, hyp(ux + C - E.sx, uy + C - E.sy) / (R * 0.46f), g);
          mix(c, g, 0.98f * kk);
          const float ty = (uy + C - (E.topY - R * 0.01f)) / (R * 0.17f);
          static const float BLACK3[3] = {0, 0, 0};
          mix(c, BLACK3, 0.5f * (1 - clamp01(ty)) * kk);
        }
        const float rc = clamp01(RIM_W / 2 - dmin + 0.5f);
        if (rc > 0) {
          drew = true;
          mix(c, RIM_COL, 0.96f * rc * clip);
        }
      }
      if (F.hasBrow && act[2]) {
        const Brow& B = F.brow;
        float bd;
        if (dot3(B.m1, p3) >= 0 && dot3(B.m3, p3) >= 0) {
          bd = sdist(B.n, dot3(B.n, p3) + B.d, kx, ky, R);
          if (bd < 0) bd = -bd;
        } else {
          const float d1 = B.z1 > 0 ? hyp(ux - B.x1, uy - B.y1) : BIG;
          const float d3 = B.z3 > 0 ? hyp(ux - B.x3, uy - B.y3) : BIG;
          bd = d1 < d3 ? d1 : d3;
        }
        const float bc = clamp01(BROW_W / 2 - bd + 0.5f);
        if (bc > 0) {
          drew = true;
          mix(c, BROW_COL, 0.92f * bc * clip);
        }
      }
      if (!drew) continue;   // 沒畫到：不寫 out888、不計入 cur（JS 會寫回同值）
      out888[i] = (uint8_t)(c[0] + 0.5f);
      out888[i + 1] = (uint8_t)(c[1] + 0.5f);
      out888[i + 2] = (uint8_t)(c[2] + 0.5f);
      if (x < work->curX0[y]) work->curX0[y] = (int16_t)x;
      if (x > work->curX1[y]) work->curX1[y] = (int16_t)x;
    }
  }
  return F.rect;
}

#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC pop_options
#endif

#endif  // FACE_RT3D_H
