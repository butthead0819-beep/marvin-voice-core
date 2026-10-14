// hud_shared.js — /hud 與 /stage 共用繪圖的唯一真相來源（黑膠潑漆繪製 + Marvin 頭）。
// car_http_app.py 用 __SHARED_JS__ 佔位字串把這份檔案整段內嵌進 <script>，這裡同時是
// 唯一真相來源，不是另外複製一份邏輯進 HUD_HTML / hud_stage.html 字串裡。
// 不得引用 hud_performance.js 的任何符號（PERF_*／perfShouldRender／ensurePerf／
// EMOTIONS 等）——疊加表演取樣由呼叫端透過 opts.samplePerf 注入。

// ---------- spinning vinyl for 現正播放 ----------
function rng(seed){ return ()=>{ seed=(seed*1664525+1013904223)>>>0; return seed/4294967296; }; }
function shade(hex, amt){   // amt<0 變暗（往黑混）、amt>0 變亮（往白混）
  const n=parseInt(String(hex).replace('#',''),16), rr=(n>>16)&255, gg=(n>>8)&255, bb=n&255;
  const mix=c=> amt<0 ? Math.round(c*(1+amt)) : Math.round(c+(255-c)*amt);
  return `rgb(${mix(rr)},${mix(gg)},${mix(bb)})`;
}
function drawLabelArt(ctx,cx,cy,LR,tk){
  const [a,b,c,d]=tk.pal, PI2=Math.PI*2;
  ctx.save(); ctx.beginPath(); ctx.arc(cx,cy,LR,0,PI2); ctx.clip();
  const g=ctx.createLinearGradient(cx-LR,cy-LR,cx+LR,cy+LR); g.addColorStop(0,c); g.addColorStop(1,d);
  ctx.fillStyle=g; ctx.fillRect(cx-LR,cy-LR,LR*2,LR*2);
  [[a,-0.4,-0.3,1.1],[b,0.5,-0.1,1.0],[a,0.2,0.6,0.9]].forEach(([col,px,py,rad])=>{
    const x=cx+LR*px,y=cy+LR*py,R=LR*rad; const bg=ctx.createRadialGradient(x,y,0,x,y,R);
    bg.addColorStop(0,col+'DD'); bg.addColorStop(0.5,col+'55'); bg.addColorStop(1,col+'00');
    ctx.fillStyle=bg; ctx.beginPath(); ctx.arc(x,y,R,0,PI2); ctx.fill(); });
  ctx.globalCompositeOperation='soft-light'; ctx.fillStyle='#fff';
  ctx.globalAlpha=.45;
  for(let i=0;i<9;i++){ ctx.save(); ctx.translate(cx,cy); ctx.rotate(i/9*PI2);
    ctx.beginPath(); ctx.ellipse(0,LR*0.45,LR*0.12,LR*0.4,0,0,PI2); ctx.fill(); ctx.restore(); }
  ctx.globalCompositeOperation='source-over'; ctx.globalAlpha=1;
  ctx.fillStyle='rgba(255,255,255,.96)'; ctx.textAlign='center'; ctx.textBaseline='middle';
  ctx.font='700 '+(LR*0.34)+'px Futura,"Avenir Next",sans-serif';
  ctx.shadowColor='rgba(0,0,0,.35)'; ctx.shadowBlur=LR*0.08;
  ctx.fillText(tk.title,cx,cy-LR*0.02);
  ctx.shadowBlur=0; ctx.textAlign='left'; ctx.textBaseline='alphabetic'; ctx.restore();
}
// 中央標籤優先用真封面圖（iTunes/YouTube thumbnail，見 cover_palette.py）；
// 沒有圖或圖還沒載完 → 退回程序化 splatter 標籤。快取 Image 物件避免每次 render 重抓。
const imgCache=new Map();   // url -> Image | 'error'
function getCoverImage(url, onReady){
  if(!url) return null;
  const cached=imgCache.get(url);
  if(cached==='error') return null;
  if(cached instanceof Image) return (cached.complete && cached.naturalWidth) ? cached : null;
  const img=new Image();
  imgCache.set(url, img);
  img.onload=onReady;
  img.onerror=()=>imgCache.set(url,'error');
  img.src=url;
  return null;
}
function drawVinylDisc(dctx, cover, DPR, onCoverReady){
  const W=dctx.canvas.width,H=dctx.canvas.height,S=Math.min(W,H),cx=W/2,cy=H/2,Rdisc=S*0.49,LR=S*0.205,PI2=Math.PI*2;
  const pal=cover.pal, r=rng(cover.title.length*131+7);
  dctx.clearRect(0,0,W,H);
  const baseCol=pal[0]||'#1b1620';
  const body=dctx.createRadialGradient(cx-Rdisc*0.25,cy-Rdisc*0.3,Rdisc*0.1,cx,cy,Rdisc);
  body.addColorStop(0,shade(baseCol,0.32)); body.addColorStop(0.6,shade(baseCol,-0.15)); body.addColorStop(1,shade(baseCol,-0.55));
  dctx.fillStyle=body; dctx.beginPath(); dctx.arc(cx,cy,Rdisc,0,PI2); dctx.fill();
  dctx.save(); dctx.beginPath(); dctx.arc(cx,cy,Rdisc,0,PI2); dctx.arc(cx,cy,LR*0.98,0,PI2,true); dctx.clip();
  // 真潑漆黑膠的樣子：從標籤邊緣往外放射的噴痕，每條方向長短完全不均——多數噴痕很短、
  // 少數噴得很遠，方向之間留大片空白，不是整圈平均鋪滿（見參考圖：翻譯半透明藍膠+黑噴痕）。
  // 三種筆觸（細噴痕/潑濺塊/孤立小點）的比例每張唱片自己隨機抽一次，不是固定配方——
  // 有的歌噴痕多、有的歌潑濺塊多，混合起來才不會每張看起來都同一套公式。
  const mixRay=0.55+r()*0.7, mixSplash=0.45+r()*0.9, mixDot=0.45+r()*0.9;
  const rays=Math.floor((90+r()*70)*mixRay);
  for(let i=0;i<rays;i++){
    const ang=r()*PI2;
    const reach=Math.pow(r(),2.4);                    // 平方以上→大部分噴痕短，少數噴得遠
    const endR=LR+reach*(Rdisc-LR)*1.02;
    const segs=3+Math.floor(reach*9);                  // 噴得越遠，沿路留的斑點越多（拖尾感）
    const col=pal[Math.floor(r()*pal.length)];
    for(let s=0;s<segs;s++){
      const t=s/Math.max(1,segs-1);
      const rr=LR+t*(endR-LR)+(r()-0.5)*S*0.006;       // 半徑方向也帶點抖動，噴痕不是死直線
      const ja=ang+(r()-0.5)*0.05;
      const w=S*(0.005*(1-t*0.75)+r()*0.0025);         // 越接近尾端越細
      dctx.globalAlpha=(0.85-t*0.45)*(0.6+r()*0.4);
      dctx.fillStyle=col;
      dctx.beginPath();
      dctx.arc(cx+Math.cos(ja)*rr, cy+Math.sin(ja)*rr, w, 0, PI2);
      dctx.fill();
    }
  }
  // 混一些較大的潑濺塊（splash），不是只有細噴痕——每塊由幾顆重疊圓組成不規則形狀，
  // 大多落在靠標籤近的地方（reach 冪次偏小），少數飛遠一點。
  const splashes=Math.floor((8+r()*8)*mixSplash);
  for(let i=0;i<splashes;i++){
    const ang=r()*PI2, reach=Math.pow(r(),1.6), rr=LR+reach*(Rdisc-LR)*0.85;
    const bx=cx+Math.cos(ang)*rr, by=cy+Math.sin(ang)*rr;
    const blobR=S*(0.012+r()*0.022), col=pal[Math.floor(r()*pal.length)];
    const lumps=3+Math.floor(r()*4);
    dctx.fillStyle=col;
    for(let k=0;k<lumps;k++){
      const lx=bx+(r()-0.5)*blobR*1.6, ly=by+(r()-0.5)*blobR*1.6, lr=blobR*(0.4+r()*0.7);
      dctx.globalAlpha=0.55+r()*0.35;
      dctx.beginPath(); dctx.arc(lx,ly,lr,0,PI2); dctx.fill();
    }
  }
  // 少量脫離主噴痕、飛得比噴痕更遠的孤立小點
  for(let i=0;i<Math.floor(rays*0.25*mixDot);i++){
    const ang=r()*PI2, rr=LR+Math.pow(r(),0.35)*(Rdisc-LR);
    dctx.globalAlpha=0.5+r()*0.4; dctx.fillStyle=pal[Math.floor(r()*pal.length)];
    dctx.beginPath(); dctx.arc(cx+Math.cos(ang)*rr, cy+Math.sin(ang)*rr, S*(0.0015+r()*0.003), 0, PI2); dctx.fill();
  }
  dctx.globalAlpha=1;
  // 溝槽反光：畫在潑漆最上層（亮線+暗線成對＝溝槽斷面的高光/陰影），alpha 要夠強才不會
  // 被下面較實心的 splash/ray 蓋掉、在整張潑漆圖案上仍看得出一圈圈唱片紋理。
  dctx.lineWidth=Math.max(1,DPR*0.6);
  for(let R=LR*1.15; R<Rdisc*0.98; R+=S*0.008){
    dctx.strokeStyle='rgba(255,255,255,0.16)'; dctx.beginPath(); dctx.arc(cx,cy,R,0,PI2); dctx.stroke();
    dctx.strokeStyle='rgba(0,0,0,0.12)'; dctx.beginPath(); dctx.arc(cx,cy,R+DPR*0.7,0,PI2); dctx.stroke();
  }
  dctx.restore();
  const gl=dctx.createRadialGradient(cx-Rdisc*0.4,cy-Rdisc*0.5,0,cx-Rdisc*0.4,cy-Rdisc*0.5,Rdisc*1.1);
  gl.addColorStop(0,'rgba(255,255,255,0.12)'); gl.addColorStop(0.4,'rgba(255,255,255,0)');
  dctx.globalCompositeOperation='screen'; dctx.fillStyle=gl; dctx.beginPath(); dctx.arc(cx,cy,Rdisc,0,PI2); dctx.fill();
  dctx.globalCompositeOperation='source-over';
  dctx.strokeStyle='rgba(255,255,255,0.10)'; dctx.lineWidth=DPR; dctx.beginPath(); dctx.arc(cx,cy,Rdisc,0,PI2); dctx.stroke();
  const coverImg=cover.cover ? getCoverImage(cover.cover, onCoverReady) : null;
  if(coverImg){
    // 真封面：撐滿整個中央標籤圓（expand+fill，不留縫、不加邊框）。
    dctx.save(); dctx.beginPath(); dctx.arc(cx,cy,LR+DPR,0,PI2); dctx.clip();
    const iw=coverImg.naturalWidth, ih=coverImg.naturalHeight, s=Math.max((LR*2)/iw,(LR*2)/ih);
    const dw=iw*s, dh=ih*s;
    dctx.drawImage(coverImg, cx-dw/2, cy-dh/2, dw, dh);
    dctx.restore();
  } else {
    drawLabelArt(dctx,cx,cy,LR,cover);
    dctx.strokeStyle='rgba(0,0,0,.4)'; dctx.lineWidth=DPR*1.5; dctx.beginPath(); dctx.arc(cx,cy,LR,0,PI2); dctx.stroke();
  }
}

// ---------- live Marvin head (metallic + green triangle eyes) ----------
// 每個情緒只用三個既有槓桿驅動：眼睛顏色/亮度（col+alpha+pulse）、眨眼節奏（blink）、
// 視線方向（gaze）——不加新繪製層/新幾何，Pi 3B 要跑得動，全部只是餵給既有 frame() 的數字。
const MOODCFG={
  idle:    { col:[104,158,58], blink:{min:2,max:6,dur:0.16},
             gaze:t=>[Math.sin(t*0.33)*0.18, Math.sin(t*0.23+1.1)*0.12] },
  wake:    { col:[150,224,72], blink:{min:2,max:6,dur:0.16}, gaze:()=>[0,-0.03] },
  speak:   { col:[152,222,82], blink:{min:2,max:6,dur:0.16}, gaze:t=>[Math.sin(t*0.8)*0.05,-0.02],
             pulse:{speed:7.3,amp:0.5,speed2:11.1,amp2:0.3} },
  think:   { col:[92,178,120], blink:{min:2,max:6,dur:0.16},
             gaze:t=>[-0.1+Math.sin(t*0.5)*0.08, -0.16+Math.sin(t*0.7)*0.05] },
  // ---- 新增七態（睡眠/待機/工作中/待核准/升級 為持續態；被搖晃/快速核准 為一次性反應）----
  sleep:   { col:[60,75,95], alpha:0.45, blink:{forceClosed:true,min:5,max:9,dur:0.5},
             gaze:()=>[0,0], noSaccade:true },
  working: { col:[70,190,160], blink:{min:1.2,max:3,dur:0.12},
             gaze:t=>[-0.12+Math.sin(t*1.1)*0.04,-0.05+Math.sin(t*0.9)*0.03], pulse:{speed:1.6,amp:0.15} },
  pending: { col:[220,175,60], blink:{min:4,max:8,dur:0.2}, gaze:()=>[0,0], pulse:{speed:0.8,amp:0.1} },
  escalate:{ col:[230,90,55], blink:{min:0.6,max:1.5,dur:0.1},
             gaze:t=>[Math.sin(t*3.4)*0.3,0], pulse:{speed:3.9,amp:0.25} },
  shaken:  { col:[240,240,235], blink:{forceOpen:true},
             gaze:t=>[Math.sin(t*9)*0.35, Math.sin(t*7)*0.15] },
  quickApprove:{ col:[150,255,120], blink:{min:0.05,max:0.15,dur:0.18}, gaze:()=>[0,0] },
};
function createMarvinHead(canvas, opts){
  const params=opts.params, reduce=opts.reduce, fps=opts.fps;
  const samplePerf=opts.samplePerf || (()=>({pv:{}, perfCtx:null}));
  const ctx=canvas.getContext('2d');
  const st={t:0,gphi:0,glam:0,vphi:0,vlam:0,blink:1,blinkT:1.2,blinkStart:-1,sacT:0,sacX:0,sacY:0,ec:[104,158,58].slice(),cam:1};
  let W=0,Hh=0,DPR=1;
  // fps 節流：畫面在弱 GPU 裝置（如 Pi 3B kiosk）上很吃重，60Hz 全速重畫沒必要。
  // st.t 改用實際經過秒數推進（renderDt*1.8，1.8＝原本 0.03/frame 假設 60fps 換算的速率），
  // 節流後動畫速度才不會被拖慢——不能只是跳過重畫卻仍固定加 0.03。
  const FRAME_MS=1000/fps; let lastRenderTs=0;
  function size(){ const r=canvas.getBoundingClientRect(); DPR=Math.min(2,window.devicePixelRatio||1);
    W=canvas.width=Math.max(1,r.width*DPR); Hh=canvas.height=Math.max(1,r.height*DPR); }
  const ro=new ResizeObserver(size); ro.observe(canvas); size();
  const P2=Math.PI*2;
  let raf=null;
  function frame(ts){
    if(!lastRenderTs) lastRenderTs=ts;
    const sinceRender=ts-lastRenderTs;
    if(sinceRender<FRAME_MS){ if(!reduce) raf=requestAnimationFrame(frame); return; }
    const renderDt=sinceRender/1000; lastRenderTs=ts;
    const mood=params.mood;
    const {pv, perfCtx}=samplePerf(mood, st);
    if(!(pv.freeze>0.5)) st.t+=renderDt*1.8;
    ctx.clearRect(0,0,W,Hh);
    const cfg=MOODCFG[mood]||MOODCFG.idle;
    const p=cfg.pulse;
    const env = p ? Math.max(0, Math.sin(st.t*p.speed)*p.amp + (p.speed2?Math.sin(st.t*p.speed2)*p.amp2:0)) : 0;
    st.cam += (((mood==='speak'||mood==='wake')?1.05:1)-st.cam)*0.05;
    // headBase 只吃「高度」——卡片變寬只讓 canvas(W) 變寬，不會放大頭（卡片高度不隨
    // flex-grow 橫向分配變動，是穩定的頭部尺寸基準）。cx 的左右漂浮改吃全部的 W，
    // 卡片越寬、Marvin 能晃動的地盤就越大，這才是「卡片變大＝拓展活動空間」而非放大頭。
    // 例外：卡片比高還窄（/stage 放歌時左欄是直立窄卡）就改吃寬度，不然頭比卡片寬會被裁掉。
    const headBase=Math.min(Hh,W);
    const perfShakeX=pv.shake?Math.sin(st.t*41)*pv.shake*headBase*0.02:0;
    const perfShakeY=pv.shake?Math.cos(st.t*37)*pv.shake*headBase*0.02:0;
    const cx=W/2+Math.sin(st.t*0.4)*W*0.02+(pv.swayX||0)*headBase*0.05+(pv.driftX||0)*headBase*0.08+perfShakeX;
    const floatY=Math.sin(st.t*0.28)*headBase*0.045;   // 明顯一點的上下起伏，才有懸浮感（不只是待機微動）
    const cy=Hh*0.45+floatY+env*headBase*0.03+(pv.bobY||0)*headBase*0.05+(pv.driftY||0)*headBase*0.08+perfShakeY;
    const R=headBase*0.40*st.cam*(1+Math.sin(st.t*0.9)*0.006)*(pv.scale??1);
    // 陰影跟球體脫開一段距離、且隨浮動高度縮放變淡——飄得越高陰影越小越淡、
    // 沉得越低陰影越大越實，這種「陰影跟物體不貼在一起」才會讀成懸浮，不是貼地站著。
    const floatNorm=(floatY/(headBase*0.045)+1)/2;
    const shadowGap=headBase*0.10+floatNorm*headBase*0.05;
    const shadowScale=1-floatNorm*0.22, shadowAlpha=0.40-floatNorm*0.16;
    ctx.save(); ctx.translate(cx,cy+R+shadowGap); ctx.scale(shadowScale,0.22*shadowScale);
    const cs=ctx.createRadialGradient(0,0,0,0,0,R*0.85);
    cs.addColorStop(0,`rgba(0,0,0,${shadowAlpha})`); cs.addColorStop(0.7,`rgba(0,0,0,${shadowAlpha*0.4})`); cs.addColorStop(1,'rgba(0,0,0,0)');
    ctx.fillStyle=cs; ctx.beginPath(); ctx.arc(0,0,R*0.85,0,P2); ctx.fill();
    // 卡片底色接近黑，純黑陰影對比不夠、看不出來（跟卡片 box-shadow 那次同個問題）。
    // 疊一層 screen 混合的淡綠光暈——加法混合永遠比背景亮，暗底也讀得出來，順便呼應
    // 整個 HUD 的霓虹風格，讀起來像懸浮物投下的光暈而不是純陰影。
    const glowAlpha=0.16-floatNorm*0.09;
    const gl=ctx.createRadialGradient(0,0,0,0,0,R*0.75);
    gl.addColorStop(0,`rgba(140,214,90,${glowAlpha})`); gl.addColorStop(1,'rgba(140,214,90,0)');
    ctx.globalCompositeOperation='screen'; ctx.fillStyle=gl; ctx.beginPath(); ctx.arc(0,0,R*0.75,0,P2); ctx.fill();
    ctx.globalCompositeOperation='source-over';
    ctx.restore();
    const sph=ctx.createRadialGradient(cx-R*0.34,cy-R*0.42,R*0.05,cx,cy,R*1.07);
    sph.addColorStop(0,'#ffffff');sph.addColorStop(0.3,'#eef2f4');sph.addColorStop(0.66,'#cfd6dc');sph.addColorStop(0.9,'#b6c0c8');sph.addColorStop(1,'#8b959d');
    ctx.fillStyle=sph;ctx.beginPath();ctx.arc(cx,cy,R,0,P2);ctx.fill();
    ctx.save();ctx.beginPath();ctx.arc(cx,cy,R,0,P2);ctx.clip();
    const hot=ctx.createRadialGradient(cx-R*0.33,cy-R*0.42,0,cx-R*0.33,cy-R*0.42,R*0.5);
    hot.addColorStop(0,'rgba(255,255,255,0.9)');hot.addColorStop(1,'rgba(255,255,255,0)');
    ctx.fillStyle=hot;ctx.fillRect(cx-R,cy-R,2*R,2*R);
    ctx.restore();
    ctx.strokeStyle='rgba(255,255,255,.4)';ctx.lineWidth=DPR;ctx.beginPath();ctx.arc(cx,cy,R,0,P2);ctx.stroke();
    const tc=cfg.col; st.ec=st.ec.map((v,i)=>v+(tc[i]-v)*0.06);
    const boost=1+env*0.5, gr=Math.min(255,st.ec[0]*boost), gg=Math.min(255,st.ec[1]*boost), gb=Math.min(255,st.ec[2]*boost);
    const alpha=cfg.alpha!=null?cfg.alpha:0.98;
    const dark=k=>`rgba(${gr*k|0},${gg*k|0},${gb*k|0},${alpha})`;
    const bright=`rgba(${Math.min(255,gr+80)|0},${Math.min(255,gg+70)|0},${Math.min(255,gb+70)|0},${alpha})`;
    let [tphi,tlam]=cfg.gaze(st.t);
    tphi += params.focusDir*0.42;
    tphi += pv.gazeDir||0;
    if(!cfg.noSaccade){
      if(st.t>st.sacT){ st.sacT=st.t+0.4+Math.random()*1.7; st.sacX=(Math.random()-0.5)*0.1; st.sacY=(Math.random()-0.5)*0.06; }
      tphi+=st.sacX; tlam+=st.sacY;
    }
    st.vphi+=(tphi-st.gphi)*0.018-st.vphi*0.14; st.gphi+=st.vphi;
    st.vlam+=(tlam-st.glam)*0.018-st.vlam*0.14; st.glam+=st.vlam;
    const bc=cfg.blink||{min:2,max:6,dur:0.16};
    if(bc.forceOpen){ st.blink=1; st.blinkStart=-1; }
    else if(bc.forceClosed){
      if(st.blinkStart<0&&st.t>st.blinkT){ st.blinkStart=st.t; st.blinkT=st.t+bc.min+Math.random()*(bc.max-bc.min); }
      const rest=0.15;
      if(st.blinkStart>=0){ const pr=(st.t-st.blinkStart)/bc.dur; if(pr>=1) st.blinkStart=-1; else st.blink=rest+(1-rest)*0.4*Math.sin(pr*Math.PI); }
      else st.blink=rest;
    } else {
      if(st.blinkStart<0&&st.t>st.blinkT){ st.blinkStart=st.t; st.blinkT=st.t+bc.min+Math.random()*(bc.max-bc.min); }
      st.blink=1;
      if(st.blinkStart>=0){ const pr=(st.t-st.blinkStart)/bc.dur; if(pr>=1) st.blinkStart=-1; else st.blink=1-0.92*Math.sin(pr*Math.PI); }
    }
    const phiC=0.72,dw=0.27,lamC=0.15,dhA=0.26, proj=(phi,lam)=>[cx+R*Math.cos(lam)*Math.sin(phi),cy+R*Math.sin(lam)];
    function eye(sign){
      const p0=sign*phiC+st.gphi, lam0=lamC+st.glam;
      const P=[proj(p0+sign*dw,lam0+0.05*st.blink),proj(p0-sign*dw,lam0),proj(p0,lam0+dhA*st.blink)];
      const path=()=>{ctx.beginPath();ctx.moveTo(P[0][0],P[0][1]);ctx.lineTo(P[1][0],P[1][1]);ctx.lineTo(P[2][0],P[2][1]);ctx.closePath();};
      path();ctx.fillStyle=dark(0.45);ctx.fill();
      ctx.save();path();ctx.clip();
      const sx=(P[1][0]+P[2][0])/2+st.gphi*R*0.9, sy=(P[1][1]+P[2][1])/2;
      const g=ctx.createRadialGradient(sx,sy,0,sx,sy,R*0.46);
      g.addColorStop(0,bright);g.addColorStop(0.4,dark(1));g.addColorStop(1,dark(0.42));ctx.fillStyle=g;ctx.fill();
      const topY=Math.min(P[0][1],P[1][1]);
      const sh=ctx.createLinearGradient(0,topY-R*0.01,0,topY+R*0.16);sh.addColorStop(0,'rgba(0,0,0,.5)');sh.addColorStop(1,'rgba(0,0,0,0)');
      ctx.fillStyle=sh;ctx.fill();ctx.restore();
      ctx.lineJoin='round';ctx.lineCap='round';ctx.lineWidth=Math.max(2,R*0.035);ctx.strokeStyle='rgba(8,10,9,.96)';
      ctx.beginPath();ctx.moveTo(P[0][0],P[0][1]);ctx.lineTo(P[2][0],P[2][1]);ctx.lineTo(P[1][0],P[1][1]);ctx.stroke();
      return P;
    }
    // 墨鏡鏡片直接從 eye() 回傳的實際三角形頂點算 bounding box——不是另外用 proj() 算一次
    // 獨立座標，才能保證鏡片跟眼睛(含 saccade/blink 造成的偏移)完全同步移動，不會跑掉。
    function eyeBounds(P){
      const xs=P.map(p=>p[0]), ys=P.map(p=>p[1]);
      const minX=Math.min(...xs), maxX=Math.max(...xs), minY=Math.min(...ys), maxY=Math.max(...ys);
      return {cx:(minX+maxX)/2, cy:(minY+maxY)/2, w:maxX-minX, h:maxY-minY};
    }
    // 參考 Matrix 主題墨鏡設計：長方形鏡片(圓角)、深綠黑漸層＋幾道淡綠色橫紋模擬數位反光、
    // 細金屬框、鏡橋、外側鉸鏈螺絲——不是純色橢圓疊圖。
    function drawLens(b){
      const w=b.w*1.2, h=b.h*1.45;
      const x=b.cx-w/2, y=b.cy-h*0.58;
      const r=Math.min(w,h)*0.26;
      ctx.beginPath();
      if(ctx.roundRect) ctx.roundRect(x,y,w,h,r); else ctx.rect(x,y,w,h);
      const lg=ctx.createLinearGradient(x,y,x+w,y+h);
      lg.addColorStop(0,'rgba(8,16,11,0.95)');lg.addColorStop(0.55,'rgba(16,36,20,0.88)');lg.addColorStop(1,'rgba(6,10,8,0.95)');
      ctx.fillStyle=lg;ctx.fill();
      ctx.save();ctx.clip();
      ctx.fillStyle='rgba(120,230,140,0.22)';
      for(let i=0;i<3;i++){ const sy=y+h*(0.22+i*0.28)+Math.sin(st.t*2+i*2)*h*0.05; ctx.fillRect(x,sy,w,h*0.05); }
      ctx.restore();
      ctx.lineWidth=Math.max(1.5,R*0.022);ctx.strokeStyle='rgba(18,20,22,0.95)';ctx.stroke();
      return {x,y,w,h};
    }
    // 球體本身是圓形、繞中心轉沒有視覺差異，headTiltZ 只需要轉「眼睛＋眉毛＋墨鏡」這個子群組。
    const tiltRad=(pv.headTiltZ||0)*Math.PI/180;
    ctx.save(); if(tiltRad){ ctx.translate(cx,cy); ctx.rotate(tiltRad); ctx.translate(-cx,-cy); }
    const eyeP=[eye(-1),eye(1)];
    ctx.save();ctx.strokeStyle='rgba(16,19,17,.92)';ctx.lineWidth=Math.max(1.5,R*0.02);ctx.lineCap='round';ctx.lineJoin='round';
    const phiEnd=phiC+dw+0.26,curve=0.035;ctx.beginPath();
    for(let i=0;i<=24;i++){ const s=-1+i/12, ph=st.gphi+s*phiEnd, lm=lamC+st.glam+curve*s*s, q=proj(ph,lm); i?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); }
    ctx.stroke();ctx.restore();
    if(perfCtx && perfCtx.wearing && perfCtx.wearing.put_on_sunglasses){
      const lb=[eyeBounds(eyeP[0]),eyeBounds(eyeP[1])].sort((a,b)=>a.cx-b.cx).map(drawLens);
      ctx.strokeStyle='rgba(18,20,22,0.95)';ctx.lineWidth=Math.max(1.5,R*0.025);
      ctx.beginPath();ctx.moveTo(lb[0].x+lb[0].w,lb[0].y+lb[0].h*0.4);ctx.lineTo(lb[1].x,lb[1].y+lb[1].h*0.4);ctx.stroke();
      const hingeR=Math.max(1.2,R*0.018);
      ctx.fillStyle='rgba(40,42,45,0.95)';
      [[lb[0].x,lb[0].y+lb[0].h*0.35,-1],[lb[1].x+lb[1].w,lb[1].y+lb[1].h*0.35,1]].forEach(([hx,hy,dir])=>{
        ctx.beginPath();ctx.arc(hx,hy,hingeR,0,P2);ctx.fill();
        ctx.beginPath();ctx.moveTo(hx,hy);ctx.lineTo(hx+dir*R*0.12,hy+R*0.02);ctx.stroke();
      });
    }
    ctx.restore();
    if(!reduce) raf=requestAnimationFrame(frame);
  }
  raf=requestAnimationFrame(frame);
  return {
    destroy(){ if(raf!=null) cancelAnimationFrame(raf); ro.disconnect(); }
  };
}
