"""아이폰이 QR로 접속했을 때 보이는 업로드 페이지."""

PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>사진 보내기</title>
<style>
:root{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--sub:#6e6e73;--blue:#0071e3;--green:#1a8f3c;--red:#d70015;--line:#e5e5ea}
@media(prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--fg:#f5f5f7;--sub:#98989d;--line:#38383a;--blue:#0a84ff;--green:#30d158;--red:#ff453a}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{font-family:-apple-system,sans-serif;margin:0;padding:max(20px,env(safe-area-inset-top)) 16px 40px;background:var(--bg);color:var(--fg)}
h1{font-size:22px;margin:0 0 6px}
.sub{color:var(--sub);font-size:13px;line-height:1.5;margin:0 0 14px}
button{display:block;width:100%;padding:15px;border:0;border-radius:12px;background:var(--blue);color:#fff;font-size:17px;margin-bottom:10px}
button.alt{background:var(--card);color:var(--blue);border:1px solid var(--line)}
button:disabled{opacity:.4}
.card{background:var(--card);border-radius:12px;padding:12px 14px;margin-bottom:10px}
#sum{font-size:15px;font-weight:600}
.bar{height:8px;border-radius:4px;background:var(--line);margin-top:8px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--green);transition:width .2s}
#msg{font-size:13px;color:var(--sub);margin-top:8px;min-height:1em}
details{background:var(--card);border-radius:12px;padding:10px 14px}
summary{font-size:14px;color:var(--sub)}
#list{max-height:50vh;overflow:auto;margin-top:8px}
.row{display:flex;justify-content:space-between;gap:8px;font-size:12px;padding:5px 0;border-top:1px solid var(--line)}
.row .n{word-break:break-all}.row .d{color:var(--sub)}.row .s{white-space:nowrap;color:var(--sub)}
.row.done .s,.row.sent .s{color:var(--green)}.row.err .s{color:var(--red)}.row.up .s{color:var(--blue)}
#stat{font-size:12px;color:var(--sub);margin-top:6px}
.link{background:none;color:var(--sub);font-size:12px;text-decoration:underline;width:auto;margin:14px auto 0;padding:6px}
</style></head><body>
<h1>사진 보내기</h1>
<p class="sub">한 번에 <b>최대 150장</b>까지 선택하세요. 여러 번 나눠 선택해도 되고, 이미 선택한 사진은 자동으로 제외됩니다.<br>오래된 사진은 iPhone이 iCloud에서 다시 내려받아야 해서, 사진을 선택할 때 시간이 더 걸릴 수 있어요.<br>전송이 끝날 때까지 화면을 켜 두고 이 페이지를 유지하세요.</p>
<button id="pick">사진 선택 (최대 150장씩)</button>
<div class="card"><div id="sum">선택한 사진이 없습니다</div><div class="bar"><i id="bar"></i></div><div id="msg"></div><div id="stat"></div></div>
<button id="go" disabled>보내기</button>
<button id="retry" class="alt" hidden>실패한 사진 다시 보내기</button>
<details><summary id="dsum">목록 보기</summary><div id="list"></div></details>
<button id="reset" class="link">전송 기록 지우기 (이미 보낸 사진도 다시 보내기)</button>
<script>
const $=id=>document.getElementById(id);
const CONC=3, MAXTRY=4, MAXPICK=150, LABEL={wait:'대기',up:'전송 중',done:'완료',err:'실패',sent:'이미 전송됨'};
const items=[], byKey=new Map(), holders=[];
const cnt={wait:0,up:0,done:0,err:0,sent:0};
let running=false, active=0, lock=null, raf=0, t0=Date.now(), b0=0;
const keyOf=f=>f.name+'|'+f.size;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

function pick(){            // 선택할 때마다 새 input을 만들어 이전 선택의 파일 참조를 유지한다
  const inp=document.createElement('input');
  inp.type='file'; inp.multiple=true; inp.accept='image/*,image/heic,image/heif,.heic,.heif';
  inp.onchange=()=>{
    let files=[...inp.files], over=0;
    if(files.length>MAXPICK){ over=files.length-MAXPICK; files=files.slice(0,MAXPICK); }
    addFiles(files,over);
  };
  holders.push(inp); inp.click();
}

async function addFiles(files,over=0){
  const frag=document.createDocumentFragment(), fresh=[]; let dups=0;
  for(const f of files){
    const k=keyOf(f), ex=byKey.get(k);
    if(ex){ ex.dup++; ex.el.querySelector('.d').textContent=' · 중복 선택 ×'+ex.dup; dups++; continue; }
    const it={file:f,key:k,state:'wait',tries:0,dup:0};
    it.el=document.createElement('div'); it.el.className='row wait';
    it.el.innerHTML='<span><span class="n"></span><span class="d"></span></span><span class="s"></span>';
    it.el.querySelector('.n').textContent=f.name; it.el.querySelector('.s').textContent=LABEL.wait;
    frag.appendChild(it.el); byKey.set(k,it); items.push(it); fresh.push(it); cnt.wait++;
  }
  $('list').appendChild(frag);
  let msg=[];
  if(dups) msg.push(dups+'장은 이미 선택되어 제외했어요');
  if(fresh.length) msg.push(fresh.length+'장 추가');
  if(over) msg.push('한 번에 최대 '+MAXPICK+'장까지만 추가돼요. 나머지 '+over+'장은 다시 선택해 주세요');
  $('msg').textContent=msg.join(' · ')||'선택한 사진이 없습니다';
  render();
  if(fresh.length){
    const sent=await checkServer(fresh);
    if(sent) $('msg').textContent+=' · '+sent+'장은 이미 전송되어 건너뜀';
  }
  if(running) pump();
}

async function checkServer(fresh){      // 이 PC가 이미 받은 사진은 보내지 않는다
  let sent=0;
  for(let i=0;i<fresh.length;i+=200){
    const part=fresh.slice(i,i+200);
    try{
      const r=await fetch('/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys:part.map(x=>x.key)})});
      const have=new Set((await r.json()).have);
      for(const it of part) if(have.has(it.key)&&it.state==='wait'){setState(it,'sent');sent++;}
    }catch(e){}
  }
  return sent;
}

function setState(it,s){
  cnt[it.state]--; cnt[s]++; it.state=s;
  it.el.className='row '+s; it.el.querySelector('.s').textContent=LABEL[s]+(s==='err'?'':'');
  render();
}

function render(){
  if(raf) return;
  raf=requestAnimationFrame(()=>{
    raf=0;
    const total=items.length, ok=cnt.done+cnt.sent;
    let all=0, sent=0;
    for(const it of items){ if(it.state==='sent') continue; all+=it.file.size;
      sent+= it.state==='done'?it.file.size:(it.state==='up'?(it.loaded||0):0); }
    const pct=all?Math.min(100,sent*100/all):0, mb=x=>(x/1048576).toFixed(0);
    $('sum').textContent=total?('선택 '+total+'장 · 완료 '+ok+' · 남음 '+(cnt.wait+cnt.up)+(cnt.err?' · 실패 '+cnt.err:'')):'선택한 사진이 없습니다';
    $('bar').style.width=(all?pct:(total?100:0))+'%';
    let st='';
    if(all&&(sent||running)){
      st='전송 '+Math.floor(pct)+'% ('+mb(sent)+' / '+mb(all)+'MB)';
      const el=(Date.now()-t0)/1000, got=sent-b0;
      if(running&&el>5&&got>0){ const m=Math.ceil((all-sent)/(got/el)/60); st+=' · 남은 시간 약 '+(m<1?'1분 미만':m+'분'); }
    }
    $('stat').textContent=st;
    $('dsum').textContent='목록 보기 ('+total+')';
    const left=cnt.wait+cnt.up;
    $('go').disabled=!left&&!running;
    $('go').textContent=running?'일시정지':(left?(active||cnt.done?'이어서 보내기 ('+left+'장)':'보내기 ('+left+'장)'):(total?'모두 완료 ✓':'보내기'));
    $('retry').hidden=!cnt.err||running;
  });
}

async function keepAwake(){ try{ if(navigator.wakeLock) lock=await navigator.wakeLock.request('screen'); }catch(e){} }
function release(){ try{ lock&&lock.release(); }catch(e){} lock=null; }
document.addEventListener('visibilitychange',()=>{ if(document.visibilityState==='visible'&&running) keepAwake(); });
window.addEventListener('beforeunload',e=>{ if(running||active){ e.preventDefault(); e.returnValue=''; } });

function markStart(){
  t0=Date.now(); b0=0;
  for(const it of items) b0+= it.state==='done'?it.file.size:(it.state==='up'?(it.loaded||0):0);
}

function pump(){
  while(running&&active<CONC){
    const it=items.find(x=>x.state==='wait'); if(!it) break;
    send(it);
  }
  if(running&&!active&&!items.some(x=>x.state==='wait')){
    running=false; release();
    $('msg').textContent=cnt.err?('완료했지만 '+cnt.err+'장 실패했어요. 다시 보내기를 눌러 주세요'):'모든 사진을 보냈어요 ✓';
    render();
  }
}

function send(it){
  active++; setState(it,'up'); it.loaded=0;
  const xhr=new XMLHttpRequest();
  xhr.open('POST','/upload'); xhr.timeout=180000;
  xhr.setRequestHeader('X-Filename',encodeURIComponent(it.file.name));
  xhr.setRequestHeader('Content-Type','application/octet-stream');
  xhr.upload.onprogress=e=>{ it.loaded=e.loaded; render(); };
  xhr.onloadend=async()=>{
    if(xhr.status>=200&&xhr.status<300){ it.loaded=it.file.size; setState(it,'done'); }
    else{
      it.loaded=0; it.tries++;
      if(it.tries<MAXTRY){ setState(it,'wait'); await sleep(1000*it.tries); }
      else setState(it,'err');
    }
    active--; pump();
  };
  xhr.send(it.file);
}

$('pick').onclick=pick;
$('go').onclick=()=>{
  if(running){ running=false; release(); $('msg').textContent='일시정지했어요'; render(); return; }
  running=true; markStart(); keepAwake(); $('msg').textContent='전송 중... 화면을 켜 두세요'; pump(); render();
};
$('retry').onclick=()=>{
  for(const it of items) if(it.state==='err'){ it.tries=0; setState(it,'wait'); }
  running=true; markStart(); keepAwake(); pump(); render();
};
$('reset').onclick=async()=>{
  if(!confirm('전송 기록을 지우면 이미 보낸 사진도 다시 보낼 수 있습니다. 계속할까요?')) return;
  try{ await fetch('/reset',{method:'POST'}); }catch(e){}
  for(const it of items) if(it.state==='sent') setState(it,'wait');
  $('msg').textContent='전송 기록을 지웠어요';
};
</script></body></html>"""
