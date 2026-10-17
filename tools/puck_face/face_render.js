// face_render.js — 從 tools/puck_face/mockup.html 搬出的純渲染 + 烘焙邏輯。
// 數值/公式/順序逐字照抄 mockup.html；不含 UI、狀態機、decode、推送、模擬、BPM_NOD、SONGS。
// 瀏覽器端執行（headless Chrome via bake.html），用 window.bakeAll() 取得可 JSON 序列化的烘焙結果。
(() => {
const W=240, C=120, R=108;
const MOODCFG={
  idle:        {col:[104,158,58]},
  wake:        {col:[150,224,72]},
    sleep:       {col:[60,75,95], alpha:0.45},
  quickApprove:{col:[150,255,120]},
  escalate:    {col:[230,90,55]},
};

// ---------- 渲染：照搬 hud_shared.js 的球體＋眼睛＋眉線，球固定不漂；另加「瞇眼笑」畫法 ----------
const rc=document.createElement('canvas'); rc.width=W; rc.height=W;
const r=rc.getContext('2d',{willReadFrequently:true});
function renderHead(p){
  const cfg=MOODCFG[p.mood], col=p.col||cfg.col, alpha=p.alpha??cfg.alpha??0.98;
  const cx=C, cy=C, P2=Math.PI*2;
  // 每格從乾淨的繪圖狀態開始，否則上一格的 lineCap='round' 會讓球外框接縫長出圓頭
  r.setTransform(1,0,0,1,0,0); r.globalAlpha=1; r.lineCap='butt'; r.lineJoin='miter'; r.fillStyle='#000'; r.fillRect(0,0,W,W);
  const sph=r.createRadialGradient(cx-R*0.34,cy-R*0.42,R*0.05,cx,cy,R*1.07);
  sph.addColorStop(0,'#ffffff');sph.addColorStop(0.3,'#eef2f4');sph.addColorStop(0.66,'#cfd6dc');sph.addColorStop(0.9,'#b6c0c8');sph.addColorStop(1,'#8b959d');
  r.fillStyle=sph;r.beginPath();r.arc(cx,cy,R,0,P2);r.fill();
  r.save();r.beginPath();r.arc(cx,cy,R,0,P2);r.clip();
  const hot=r.createRadialGradient(cx-R*0.33,cy-R*0.42,0,cx-R*0.33,cy-R*0.42,R*0.5);
  hot.addColorStop(0,'rgba(255,255,255,0.9)');hot.addColorStop(1,'rgba(255,255,255,0)');
  r.fillStyle=hot;r.fillRect(cx-R,cy-R,2*R,2*R);
  r.restore();
  r.strokeStyle='rgba(255,255,255,.4)';r.lineWidth=1;r.beginPath();r.arc(cx,cy,R,0,P2);r.stroke();

  const dark=k=>`rgba(${col[0]*k|0},${col[1]*k|0},${col[2]*k|0},${alpha})`;
  const bright=`rgba(${Math.min(255,col[0]+80)|0},${Math.min(255,col[1]+70)|0},${Math.min(255,col[2]+70)|0},${alpha})`;
  const yaw=p.yaw||0, pitch=p.pitch||0, roll=p.roll||0, blink=p.blink??1;
  const phiC=0.72,dw=0.27,lamC=0.15,dhA=0.26;
  // 球面投影：轉到背面的點（|phi| 或 |lam| 超過 90°）壓在邊緣上，眼睛就會貼著輪廓被擠扁、消失
  const H=Math.PI/2, cl=v=>Math.max(-H,Math.min(H,v));
  const proj=(phi,lam)=>{ const hid=Math.abs(phi)>H-0.02||Math.abs(lam)>H-0.02; phi=cl(phi); lam=cl(lam); const q=[cx+R*Math.cos(lam)*Math.sin(phi),cy+R*Math.sin(lam)]; q.hid=hid; return q; };
  // 描邊時跳過轉到背面的那段，不然它會被壓在輪廓上、沿著邊緣畫出一條黑線
  const strokeVisible=pts=>{ r.beginPath(); let pen=false; pts.forEach((q,i)=>{ const prev=pts[i-1]; const ok=!q.hid&&prev&&!prev.hid; if(ok){ if(!pen) r.moveTo(prev[0],prev[1]); r.lineTo(q[0],q[1]); pen=true; } else pen=false; }); r.stroke(); };
  // 邊用很多點細分，壓扁時才會沿著球面彎，不是直線三角形
  const seg=(a,b,n)=>N(n+1,(i)=>{const t=i/n;return proj(a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t);});
  r.save(); r.beginPath(); r.arc(cx,cy,R-0.5,0,P2); r.clip();
  if(roll){ r.translate(cx,cy); r.rotate(roll); r.translate(-cx,-cy); }
  function eye(sign){
    const p0=sign*phiC+yaw, lam0=lamC+pitch;
    if(p.happy){
      const lift=0.07+(p.bounce||0)*0.02;
      const pts=N(13,(i)=>{const s=-1+i/6;return proj(p0+sign*dw*0.95*s,lam0+0.07-lift*(1-s*s));});
      const stroke=(w,c)=>{r.lineCap='round';r.lineJoin='round';r.lineWidth=w;r.strokeStyle=c;strokeVisible(pts);};
      stroke(R*0.07,'rgba(8,10,9,.96)'); stroke(R*0.045,dark(1));
      return;
    }
    const A=[p0+sign*dw,lam0+0.05*blink], B=[p0-sign*dw,lam0], Cc=[p0,lam0+dhA*blink];
    const poly=[...seg(A,B,8),...seg(B,Cc,8).slice(1),...seg(Cc,A,8).slice(1)];
    const path=()=>{r.beginPath();poly.forEach((q,i)=>i?r.lineTo(q[0],q[1]):r.moveTo(q[0],q[1]));r.closePath();};
    path();r.fillStyle=dark(0.45);r.fill();
    const PB=proj(B[0],B[1]), PC=proj(Cc[0],Cc[1]);
    r.save();path();r.clip();
    const sx=(PB[0]+PC[0])/2+Math.sin(cl(yaw))*R*0.5, sy=(PB[1]+PC[1])/2;
    const g=r.createRadialGradient(sx,sy,0,sx,sy,R*0.46);
    g.addColorStop(0,bright);g.addColorStop(0.4,dark(1));g.addColorStop(1,dark(0.42));r.fillStyle=g;r.fill();
    const topY=Math.min(...poly.map(q=>q[1]));
    const sh=r.createLinearGradient(0,topY-R*0.01,0,topY+R*0.16);sh.addColorStop(0,'rgba(0,0,0,.5)');sh.addColorStop(1,'rgba(0,0,0,0)');
    r.fillStyle=sh;r.fill();r.restore();
    r.lineJoin='round';r.lineCap='round';r.lineWidth=Math.max(2,R*0.035);r.strokeStyle='rgba(8,10,9,.96)';
    const rim=[...seg(A,Cc,8),...seg(Cc,B,8).slice(1)];
    strokeVisible(rim);
  }
  eye(-1); eye(1);
  r.strokeStyle='rgba(16,19,17,.92)';r.lineWidth=Math.max(1.5,R*0.02);r.lineCap='round';r.lineJoin='round';
  const phiEnd=phiC+dw+0.26,curve=0.035, browUp=p.browUp||0;
  strokeVisible(N(49,(i)=>{ const s=-1+i/24; return proj(yaw+s*phiEnd,lamC+pitch-browUp+curve*s*s); }));
  r.restore();
  return r.getImageData(0,0,W,W).data;
}

// ---------- RGB565（可選 4×4 Bayer 抖色）----------
const BAYER=[0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5];
function to565(d,dither){
  const out=new Uint16Array(W*W);
  for(let y=0;y<W;y++) for(let x=0;x<W;x++){
    const i=y*W+x, j=i*4, b=dither?(BAYER[(y&3)*4+(x&3)]/16-0.5):0;
    const R5=Math.max(0,Math.min(31,Math.round(d[j]/255*31+b)));
    const G6=Math.max(0,Math.min(63,Math.round(d[j+1]/255*63+b)));
    const B5=Math.max(0,Math.min(31,Math.round(d[j+2]/255*31+b)));
    out[i]=(R5<<11)|(G6<<5)|B5;
  }
  return out;
}

// ---------- 片段 ----------
const N=(n,f)=>Array.from({length:n},(_,i)=>f(i,n));
const lerp=(a,b,t)=>a.map((v,i)=>v+(b[i]-v)*t);
const ss=x=>x*x*(3-2*x);
// 全部片段固定 24fps（死守畫面節拍，不靠降 fps 省容量）。時間用秒寫，換算成格數。
const FPS=24, F=sec=>Math.max(1,Math.round(sec*FPS));
const go=(sec,f)=>{const n=F(sec);return N(n,(i)=>f(ss((i+1)/n)));};   // 0→1 緩動
const back=arr=>arr.slice(0,-1).reverse();                              // 原路回來
const hold=(p,sec)=>N(F(sec),()=>p);
function trip(sec,h,f){ const out=go(sec,f); return [...out,...hold(out[out.length-1],h),...back(out),f(0)]; }
// 律動用的姿勢表：S3 依 BPM 算出相位，再從表裡挑一格（表本身跟 BPM 無關）
const NOD_P=20, NOD_PMAX=0.42, SW_N=25;
const CLIPS=[
  // 第 3 層
  {id:'still', frames:[{mood:'idle'}]},
  {id:'look',
    frames:[...trip(1.2,1.2,t=>({mood:'idle',yaw:-1.25*t,roll:-0.12*t})), ...trip(1.2,1.2,t=>({mood:'idle',yaw:1.25*t,roll:0.12*t}))]},
  // 第 2 層
  {id:'nod', ladder:true,
    frames:N(NOD_P,(d)=>({mood:'idle',pitch:NOD_PMAX*d/(NOD_P-1),roll:0.06*d/(NOD_P-1)}))},
  {id:'sway', ladder:true,
    frames:N(SW_N,(i)=>{const s=-1+2*i/(SW_N-1);return {mood:'idle',roll:0.3*s,yaw:0.45*s,pitch:0.06*s*s};})},
  {id:'newsong',
    frames:trip(0.6,0.7,t=>({mood:'idle',yaw:0.85*t,roll:0.32*t,pitch:-0.08*t,browUp:0.05*t}))},
  {id:'lookup',
    frames:trip(1.0,1.4,t=>({mood:'idle',pitch:-0.5*t,yaw:0.4*t,roll:-0.16*t,browUp:0.04*t}))},
  // 第 1 層
  {id:'listen',
    frames:[...go(0.6,t=>({mood:'idle',roll:0.34*t,yaw:-0.35*t,pitch:-0.06*t})),
            ...hold({mood:'idle',roll:0.34,yaw:-0.35,pitch:-0.06},1.2),
            ...trip(0.35,0.05,t=>({mood:'idle',roll:0.34,yaw:-0.35,pitch:-0.06+0.1*t}))], loopFrom:F(0.6)},
  {id:'speak', frames:trip(0.45,0.05,t=>({mood:'idle',pitch:0.16*t,yaw:0.2*t}))},
  {id:'boot',
    frames:[...hold({mood:'sleep',blink:0.12,pitch:0.36,roll:0.16},0.6),
            ...go(0.9,t=>({mood:'idle',blink:0.12+0.88*t,pitch:0.36*(1-t),roll:0.16*(1-t),col:lerp([60,75,95],[104,158,58],t),alpha:0.45+0.53*t}))]},
  {id:'error',
    frames:[...trip(0.35,0.05,t=>({mood:'escalate',yaw:-0.7*t})),...trip(0.35,0.05,t=>({mood:'escalate',yaw:0.7*t}))]},
];

// ---------- 烤畫格：每段各自裁切 ----------
let base=null;
function bboxVsBase(bufs){
  let x0=W,y0=W,x1=-1,y1=-1;
  bufs.forEach(buf=>{ for(let i=0;i<buf.length;i++) if(buf[i]!==base[i]){const x=i%W,y=(i/W)|0; if(x<x0)x0=x; if(x>x1)x1=x; if(y<y0)y0=y; if(y>y1)y1=y;} });
  if(x1<0) return null;
  x0&=~1; y0&=~1; x1|=1; y1|=1;
  return {x:x0,y:y0,w:x1-x0+1,h:y1-y0+1};
}

// ---------- bakeAll：烘焙成 JSON 可序列化物件（base64, RGB565 big-endian）----------
function u16ToBase64BE(arr){
  const bytes=new Uint8Array(arr.length*2);
  for(let i=0;i<arr.length;i++){ bytes[i*2]=(arr[i]>>8)&0xff; bytes[i*2+1]=arr[i]&0xff; }
  let bin='';
  for(let i=0;i<bytes.length;i++) bin+=String.fromCharCode(bytes[i]);
  return btoa(bin);
}
function extractRect(buf,rect){
  const out=new Uint16Array(rect.w*rect.h);
  for(let y=0;y<rect.h;y++) for(let x=0;x<rect.w;x++) out[y*rect.w+x]=buf[(rect.y+y)*W+(rect.x+x)];
  return out;
}
const LOOP_IDS=new Set(['listen','speak','error']);
const LADDER_IDS=new Set(['nod','sway']);
function clipKind(id){ return LADDER_IDS.has(id) ? 'ladder' : (LOOP_IDS.has(id) ? 'loop' : 'once'); }

function bakeAll(){
  const raw={};
  CLIPS.forEach(c=>{ raw[c.id]=c.frames.map(p=>to565(renderHead(p),true)); });
  base=raw.still[0];
  const clips=CLIPS.map(c=>{
    const bufs=raw[c.id];
    const rect=bboxVsBase(bufs);
    const uniq=[];
    const seq=bufs.map(b=>{
      let k=uniq.findIndex(u=>u.length===b.length&&u.every((v,i)=>v===b[i]));
      if(k<0){ uniq.push(b); k=uniq.length-1; }
      return k;
    });
    const frames=rect?uniq.map(b=>u16ToBase64BE(extractRect(b,rect))):[];
    return {
      id:c.id,
      kind:clipKind(c.id),
      loop_from:c.loopFrom||0,
      psram:LADDER_IDS.has(c.id),
      rect:rect?{x:rect.x,y:rect.y,w:rect.w,h:rect.h}:null,
      frames,
      seq:rect?seq:[],
    };
  });
  return { fps:FPS, width:W, height:W, base:u16ToBase64BE(base), clips };
}
window.bakeAll=bakeAll;
})();
