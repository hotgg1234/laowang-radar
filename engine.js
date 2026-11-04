/* 選股策略引擎（網頁版）。與 scripts/strategy.py 是同一套規則，修改時兩邊要一起改。
   規則移植自 github.com/kevin801221/stock-strategies-only（V3.3 參數化策略）。
   rows：[[date, open, high, low, close, volume, foreign, trust], ...] 依日期排序 */
(function(root){
"use strict";
const rnd=x=>Math.floor(x+0.5);
const bad=v=>v===null||v===undefined||Number.isNaN(v);

/* 補償加總，結果與 Python 3.12+ 的 sum() 相同，兩邊算出的均線才會一模一樣 */
function fsum(a,i0,i1,f){let s=0,c=0;for(let j=i0;j<=i1;j++){const x=f?f(a[j]):a[j];const t=s+x;c+=Math.abs(s)>=Math.abs(x)?(s-t)+x:(x-t)+s;s=t;}return c&&isFinite(c)?s+c:s;}
function sma(a,k){const o=new Array(a.length).fill(NaN);for(let i=k-1;i<a.length;i++)o[i]=fsum(a,i-k+1,i)/k;return o;}
function std(a,k,m){const o=new Array(a.length).fill(NaN);for(let i=k-1;i<a.length;i++){const mi=m[i];o[i]=Math.sqrt(fsum(a,i-k+1,i,v=>(v-mi)**2)/(k-1));}return o;}
function ewmAdjust(a,com){const b=1-1/(1+com);const o=new Array(a.length).fill(NaN);let num=0,den=0,st=false;
  for(let i=0;i<a.length;i++){const v=a[i];if(bad(v)){if(st){num*=b;den*=b;o[i]=num/den;}continue;}st=true;num=v+b*num;den=1+b*den;o[i]=num/den;}return o;}
function ewmSpan(a,span){const al=2/(span+1);const o=new Array(a.length);let e=null;for(let i=0;i<a.length;i++){e=e===null?a[i]:al*a[i]+(1-al)*e;o[i]=e;}return o;}

function indicators(rows){
  const n=rows.length,H=rows.map(r=>r[2]),L=rows.map(r=>r[3]),C=rows.map(r=>r[4]);
  const ma20=sma(C,20),sd=std(C,20,ma20);const rsv=new Array(n).fill(NaN);
  for(let i=8;i<n;i++){let lo=Infinity,hi=-Infinity;for(let j=i-8;j<=i;j++){if(L[j]<lo)lo=L[j];if(H[j]>hi)hi=H[j];}rsv[i]=hi>lo?(C[i]-lo)/(hi-lo)*100:50;}
  const k=ewmAdjust(rsv,2),d=ewmAdjust(k,2);const e12=ewmSpan(C,12),e26=ewmSpan(C,26);
  const dif=e12.map((v,i)=>v-e26[i]);const dea=ewmSpan(dif,9);
  return {close:C,ma5:sma(C,5),ma20,ma60:sma(C,60),bbU:ma20.map((m,i)=>m+2*sd[i]),bbL:ma20.map((m,i)=>m-2*sd[i]),k,d,dif,dea,hist:dif.map((v,i)=>v-dea[i])};
}

const SWITCHES=["use_ma_alignment","use_bollinger_bounce","use_kd_golden_cross","use_macd_bullish"];
const SIG_NAMES=["均線多頭","布林下軌反彈","KD黃金交叉","MACD多頭"];
const PARTIAL=[0.48,0.4,0.4,0.4];

function techState(ind,i){
  const c=ind.close[i],m20=ind.ma20[i],m60=ind.ma60[i];const st=[null,null,null,null];
  if(!bad(m20)&&!bad(m60))st[0]=c>m20&&m20>m60?2:(c>m20?1:0);
  const bl=ind.bbL[i],bm=ind.ma20[i];
  if(!bad(bl)&&!bad(bm)){const dist=bl?(c-bl)/bl:NaN;st[1]=(!bad(dist)&&dist>0&&dist<0.03)?2:(c<bm?1:0);}
  const k=ind.k[i],d=ind.d[i];if(!bad(k)&&!bad(d))st[2]=k>d&&k<80?2:(k>d?1:0);
  const h=ind.hist[i];if(!bad(h))st[3]=h>0&&ind.dif[i]>ind.dea[i]?2:(h>0?1:0);
  return st;
}
function techScore(state,p){
  const on=SWITCHES.map(s=>p[s]!==false);const per=100/(on.filter(Boolean).length||1);let score=0;const sigs=[];
  for(let j=0;j<4;j++){if(!on[j]||state[j]===null)continue;if(state[j]===2){score+=per;sigs.push(SIG_NAMES[j]);}else if(state[j]===1)score+=per*PARTIAL[j];}
  return {score:rnd(score),signals:sigs};
}

const PATTERN_NOTE={"倍量柱":"今日量 ≥ 昨日 2 倍（+10）","梯量柱":"連 3 日量增且價漲（+8）","縮量柱":"連 3 日量縮且價跌，洗盤（+5）",
  "低量柱":"量 < 20 日均量 60%，拋壓耗盡（+8）","平量柱":"連 3 日量能接近，多空平衡（0）","放量滯漲":"放量但收黑、長上影或幾乎沒漲（−20）"};
function volumePatterns(rows,i){
  const n=rows.length;if(n<21)return {patterns:[],bonus:0};if(i===undefined)i=n-1;
  const V=rows.map(r=>r[5]),C=rows.map(r=>r[4]);const P=[];let b=0;
  const vt=V[i],vy=V[i-1];if(vy>0&&vt>=2*vy){P.push("倍量柱");b+=10;}
  const v3=V[i],v2=V[i-1],v1=V[i-2];
  if(v3>v2&&v2>v1&&C[i]>C[i-2]){P.push("梯量柱");b+=8;}
  if(v3<v2&&v2<v1&&C[i]<C[i-2]){P.push("縮量柱");b+=5;}
  const seg=V.slice(Math.max(0,i-20),i);const v20=seg.length?seg.reduce((s,v)=>s+v,0)/seg.length:0;
  if(v20>0&&vt<0.6*v20){P.push("低量柱");b+=8;}
  if(v1>0&&v2>0&&v3>0&&(Math.max(v1,v2,v3)-Math.min(v1,v2,v3))/Math.max(v1,v2,v3)<0.15)P.push("平量柱");
  const s5=V.slice(Math.max(0,i-5),i);const v5=s5.length?s5.reduce((s,v)=>s+v,0)/s5.length:0;
  if(v5>0&&vt>=1.5*v5){const o=rows[i][1],h=rows[i][2],c=C[i],pc=C[i-1];const body=Math.abs(c-o),up=h-Math.max(c,o),chg=pc>0?(c-pc)/pc:0;
    if(c<o||(body>0&&up>body*2)||(chg>0&&chg<0.01)){P.push("放量滯漲");b-=20;}}
  return {patterns:P,bonus:b};
}
function verdict(P){
  if(P.includes("放量滯漲"))return "高檔爆量疑似出貨，持有者應考慮減碼";
  if(P.includes("倍量柱")&&P.includes("梯量柱"))return "動能突破成立，主力積極進場";
  if(P.includes("倍量柱"))return "今日出現倍量攻勢，關注後續跟進";
  if(P.includes("梯量柱"))return "量能階梯推升，走勢健康";
  if(P.includes("低量柱")&&P.includes("縮量柱"))return "底部極限縮量、洗盤完成，逢訊號可伏擊";
  if(P.includes("低量柱"))return "極限縮量、拋壓耗盡，等待訊號";
  if(P.includes("平量柱"))return "多空平衡蓄力中，等下一根倍量或破線";
  if(P.includes("縮量柱"))return "量縮洗盤，主力未退場";
  return "量能平淡，無特殊訊號";
}

/* 歷史訊號勝率（原專案 backtest()）：第 i 天訊號 → i+1 開盤進場 → 看 i+2 ~ i+1+N 天的高低點與第 N 天收盤 */
function signalBacktest(rows,states,p){
  const hold=+p.hold_days,tgt=+p.target_return,stp=+p.stop_loss,min=+p.min_tech_score_for_signal,n=rows.length;
  let w=0,l=0;const rets=[];const hits=[];
  for(let i=60;i<n-hold-1;i++){
    if(techScore(states[i],p).score<min)continue;
    const entry=rows[i+1][1];if(!entry||entry<=0)continue;
    const fut=rows.slice(i+2,i+2+hold);if(fut.length<hold)continue;
    let hi=-Infinity,lo=Infinity;for(const r of fut){if(r[2]>hi)hi=r[2];if(r[3]<lo)lo=r[3];}
    const ht=hi>=entry*(1+tgt),hs=lo<=entry*(1-stp);let r,win;
    if(ht&&!hs){r=tgt;win=true;}else if(hs){r=-stp;win=false;}else{r=(fut[fut.length-1][4]-entry)/entry;win=r>0;}
    win?w++:l++;rets.push(r);hits.push({i,r,win});
  }
  const t=w+l;if(!t)return {winrate:null,samples:0,avg:null,hits};
  return {winrate:Math.round(w/t*1000)/1000,samples:t,avg:Math.round(rets.reduce((s,v)=>s+v,0)/rets.length*1e4)/1e4,hits};
}

/* 單檔評估。s 是 screener.json 的一筆；p 是策略參數（含 id） */
function evaluate(s,p,marketOk){
  if(s.short)return {action:"SKIP",score:null,tech:null,fundPass:null,signals:[],notes:["價格資料不足 100 個交易日"]};
  const ts=techScore(s.ts,p);const bonus=p.use_volume_patterns!==false?s.vb:0;
  const tech=Math.max(0,Math.min(100,ts.score+bonus));
  const bt=(s.bt&&s.bt[p.id])||[null,0,null];const wr=bt[0];const btScore=(wr!==null?wr:0.5)*100;
  const etf=s.k==="ETF";
  const fundPass=etf?null:(s.eps!==null&&s.eps!==undefined&&s.roe!==null&&s.roe!==undefined&&s.eps>p.eps_threshold&&s.roe>p.roe_threshold);
  let wf=etf?0:+p.weight_fundamental,wt=+p.weight_technical,wb=+p.weight_backtest;let tot=wf+wt+wb;if(tot<=0){wf=0;wt=1;wb=0;tot=1;}
  const score=rnd((wf*(fundPass?100:40)+wt*tech+wb*btScore)/tot*10)/10;
  const gate=etf||p.fundamental_pass_required===false||fundPass;
  let action=score>=p.min_total_score_for_buy&&gate&&tech>=p.min_tech_score_for_buy?"BUY":(score>=50?"WATCH":"SKIP");
  const notes=[];
  if(action==="BUY"&&p.market_filter_enabled!==false&&marketOk===false){action="WATCH";notes.push("大盤跌破 "+p.market_filter_ma_period+" 日線，自動降為 WATCH");}
  if(bt[1]<8)notes.push("回測樣本僅 "+bt[1]+" 次，統計弱");
  if(!etf){if(s.eps===null||s.eps===undefined)notes.push("沒有本益比資料，無法判斷基本面");else if(!fundPass)notes.push("基本面未過門檻");}
  if(wr!==null&&wr<0.5)notes.push("歷史勝率 "+Math.round(wr*100)+"% 低於五成");
  if(s.bbHi)notes.push("已突破布林上軌，追高風險");
  if(p.use_volume_patterns!==false&&(s.vp||[]).includes("放量滯漲"))notes.push("偵測到放量滯漲，高檔爆量疑似出貨");
  return {action,score,tech,techRaw:ts.score,bonus,fundPass,signals:ts.signals,winrate:wr,samples:bt[1],avgRet:bt[2],btScore,notes,weights:{wf,wt,wb,tot}};
}

function plan(close,p){
  const sl=+p.stop_loss,tp=+p.target_return;
  return {entry:close,stop:Math.round(close*(1-sl)*100)/100,target:Math.round(close*(1+tp)*100)/100,rr:Math.round(tp/sl*100)/100,
    pos:Math.round(Math.min(2/(sl*100)*100,20)*10)/10};
}

/* 大盤狀態：market = {d:[日期], c:[收盤]}；回傳第 i 天是否站上 N 日線 */
function marketSeries(market,period){
  const c=market.c;const o={};let s=0,cnt=0;const q=[];
  for(let i=0;i<c.length;i++){const v=c[i];if(v===null||v===undefined){continue;}q.push(v);s+=v;if(q.length>period)s-=q.shift();
    o[market.d[i]]=q.length===period?{close:v,ma:s/period,ok:v>s/period}:{close:v,ma:null,ok:null};}
  return o;
}

/* 逐筆交易回測（回測頁用）：一次只持有一筆、隔天開盤進場。
   出場：盤中碰到停損價（先判斷）或停利價；跳空越過就用開盤價；持有滿 N 天以收盤出場。 */
function tradeBacktest(rows,states,p,opt){
  opt=opt||{};const n=rows.length,hold=+p.hold_days,tgt=+p.target_return,stp=+p.stop_loss,min=+p.min_tech_score_for_signal;
  const fee=opt.fee===undefined?0.001425*0.6:opt.fee,tax=opt.tax===undefined?0.003:opt.tax;
  const buyCost=x=>x*(1+fee),sellNet=x=>x*(1-fee-tax);
  const START=60;const trades=[];const curve=new Array(n).fill(NaN),bh=new Array(n).fill(NaN);let eq=1,pos=null;
  for(let t=START;t<n;t++){
    bh[t]=rows[t][4]/rows[START][4];
    if(pos&&t>pos.e){
      const r=rows[t];const held=t-pos.e;let px=null,why=null;
      const sp=pos.price*(1-stp),tp=pos.price*(1+tgt);
      if(r[3]<=sp){px=Math.min(r[1],sp);why="停損 -"+Math.round(stp*100)+"%";}
      else if(r[2]>=tp){px=Math.max(r[1],tp);why="停利 +"+Math.round(tgt*100)+"%";}
      else if(held>=hold){px=r[4];why="持有滿 "+hold+" 天";}
      if(px!==null){const ret=sellNet(px)/buyCost(pos.price);eq=pos.eq*ret;trades.push({e:pos.e,x:t,inDate:rows[pos.e][0],inPx:pos.price,outDate:r[0],outPx:px,days:held,ret:ret-1,why});pos=null;curve[t]=eq;continue;}
      curve[t]=pos.eq*sellNet(r[4])/buyCost(pos.price);continue;
    }
    if(pos){curve[t]=pos.eq*sellNet(rows[t][4])/buyCost(pos.price);continue;}
    curve[t]=eq;
    if(t+1<n&&techScore(states[t],p).score>=min&&(!opt.allow||opt.allow(t))){const px=rows[t+1][1];if(px>0)pos={e:t+1,price:px,eq};}
  }
  if(pos){const last=rows[n-1];const ret=sellNet(last[4])/buyCost(pos.price);trades.push({e:pos.e,x:n-1,inDate:rows[pos.e][0],inPx:pos.price,outDate:"持有中",outPx:last[4],days:n-1-pos.e,ret:ret-1,why:"",open:true});curve[n-1]=pos.eq*ret;}
  let peak=0,mdd=0;for(let t=START;t<n;t++){const v=curve[t];if(!isFinite(v))continue;if(v>peak)peak=v;mdd=Math.min(mdd,v/peak-1);}
  const closed=trades.filter(x=>!x.open),wins=closed.filter(x=>x.ret>0);
  const gp=wins.reduce((s,x)=>s+x.ret,0),gl=closed.filter(x=>x.ret<=0).reduce((s,x)=>s-x.ret,0);
  const total=(isFinite(curve[n-1])?curve[n-1]:eq)-1;const years=Math.max(1/252,(n-1-START)/252);
  return {START,trades,curve,bh,total,cagr:Math.pow(1+total,1/years)-1,bhRet:sellNet(rows[n-1][4])/buyCost(rows[START][4])-1,mdd,
    n:closed.length,win:closed.length?wins.length/closed.length:NaN,avg:closed.length?closed.reduce((s,x)=>s+x.ret,0)/closed.length:NaN,
    pf:gl>0?gp/gl:(gp>0?Infinity:NaN),avgDays:closed.length?closed.reduce((s,x)=>s+x.days,0)/closed.length:NaN,
    inPos:trades.reduce((s,x)=>s+x.days,0)/Math.max(1,n-START)};
}

const Engine={indicators,techState,techScore,volumePatterns,verdict,signalBacktest,evaluate,plan,marketSeries,tradeBacktest,SWITCHES,SIG_NAMES,PATTERN_NOTE};
if(typeof module!=="undefined"&&module.exports)module.exports=Engine;else root.Engine=Engine;
})(typeof window!=="undefined"?window:globalThis);
