const $=s=>document.querySelector(s);
const HDR={'X-Requested-With':'mks','Content-Type':'application/json'};
const P={check:'M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z',
 snow:'M22 11h-4.17l3.24-3.24-1.41-1.42L15 11h-2V9l4.66-4.66-1.42-1.41L13 6.17V2h-2v4.17L7.76 2.93 6.34 4.34 11 9v2H9L4.34 6.34 2.93 7.76 6.17 11H2v2h4.17l-3.24 3.24 1.41 1.42L9 13h2v2l-4.66 4.66 1.42 1.41L11 17.83V22h2v-4.17l3.24 3.24 1.42-1.41L13 15v-2h2l4.66 4.66 1.41-1.42L17.83 13H22z',
 pause:'M6 5h4v14H6zM14 5h4v14h-4z',play:'M8 5v14l11-7z',stop:'M6 6h12v12H6z',home:'M12 3 2 12h3v8h5v-6h4v6h5v-8h3z',arrow:'M12 4l8 9h-5v7H9v-7H4z',
 motor:'M3 9h2v6H3zM6 6h10v12H6zM16 10h5v4h-5zM8 8v8h2V8z',power:'M11 3h2v10h-2zM17.7 6.3l-1.4 1.4A6 6 0 1 1 7.7 7.7L6.3 6.3a8 8 0 1 0 11.4 0z',
 refresh:'M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4z',trash:'M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6zM19 4h-3.5l-1-1h-5l-1 1H5v2h14z',
 tune:'M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z'};
const PS={chev:'M6 15l6-6 6 6'};
function ico(n,r){const st=PS[n];return '<svg viewBox="0 0 24 24" width="22" height="22" '+(st?'fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"':'fill="currentColor"')+' style="transform:rotate('+(r||0)+'deg)"><path d="'+(st||P[n])+'"/></svg>'}
document.querySelectorAll('[data-i]').forEach(b=>b.innerHTML=ico(b.dataset.i,b.dataset.r));$('#gear').innerHTML=ico('tune');
function lsGet(k){try{return localStorage.getItem(k)}catch(_){return null}}function lsSet(k,v){try{localStorage.setItem(k,v)}catch(_){}}
async function api(p,body){
  try{const r=await fetch(p,body===undefined?{cache:'no-store'}:{method:'POST',headers:HDR,body:JSON.stringify(body)});
    if(r.status===401){location.reload();return{}}return await r.json()}catch(e){return{error:String(e),bridge:false}}}
// Окно подтверждения вместо системного confirm(). Возвращает true/false, а с choices — выбранный вариант или null.
// У «красных» действий фокус на «Отмена»: случайный Enter ничего не сломает.
function ask(title,text,ok,danger,choices){return new Promise(res=>{
  const o=$('#dlg'),y=$('#dlgY'),n=$('#dlgN'),ch=$('#dlgC'),no=choices?null:false,prev=document.activeElement;
  if(o.classList.contains('on'))$('#dlgN').click();   // одно окно за раз: предыдущее считаем отменённым
  $('#dlgT').textContent=title;$('#dlgX').textContent=text||'';ch.textContent='';
  const key=e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();done(no)}};
  function done(v){o.classList.remove('on');document.removeEventListener('keydown',key,true);o.onclick=n.onclick=y.onclick=null;
    if(prev&&prev.focus&&document.contains(prev))prev.focus();res(v)}
  if(choices){y.style.display='none';choices.forEach(c=>{const b=document.createElement('button');b.textContent=c;b.onclick=()=>done(c);ch.appendChild(b)})}
  else{y.style.display='';y.textContent=ok||'OK';y.className=danger?'dz':'p';y.onclick=()=>done(true)}
  n.onclick=()=>done(no);o.onclick=e=>{if(e.target===o)done(no)};
  document.addEventListener('keydown',key,true);o.classList.add('on');(choices?ch.firstChild:danger?n:y).focus()})}
async function quitApp(){if(await ask('Выключить мост?','Orca перестанет отправлять файлы на принтер, панель перестанет обновляться.','Выключить',1))api('/api/quit',{})}
function toast(t){const e=$('#toast');e.textContent=t;e.style.display='block';clearTimeout(toast.t);toast.t=setTimeout(()=>e.style.display='none',5500);
  try{navigator.vibrate&&navigator.vibrate(150)}catch(_){}}
// ---- тема
const TN={auto:'как в системе',dark:'тёмная',light:'светлая'};
function applyTheme(){const t=lsGet('mks_theme')||'auto';t==='auto'?document.documentElement.removeAttribute('data-theme'):document.documentElement.dataset.theme=t;
  $('#thm').title='Тема: '+TN[t]+' (нажми, чтобы сменить)';drawChart()}
function cycleTheme(){const n={auto:'dark',dark:'light',light:'auto'}[lsGet('mks_theme')||'auto'];lsSet('mks_theme',n);applyTheme();toast('Тема: '+TN[n])}
// ---- статус
// время окончания по часам компьютера: «21:40», «завтра 03:15»
function finAt(eta){const d=new Date(Date.now()+eta*1000),n=new Date(),dd=Math.round((new Date(d).setHours(0,0,0,0)-new Date(n).setHours(0,0,0,0))/864e5);
  return(dd===1?'завтра ':dd>1?d.toLocaleDateString('ru-RU',{day:'numeric',month:'short'})+' ':'')+d.toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}
function fmt(s){if(s==null)return'—';s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return(h?h+' ч ':'')+m+' мин'}
function ttH(a){return a?'<span>'+a[0].toFixed(0)+'</span><i> / '+a[1].toFixed(0)+'°</i>':'—'}
function tt(a){return a?a[0].toFixed(0)+' / '+a[1].toFixed(0)+'°':'—'}
const seen={};let HIST=[],NOW=0,RANGE=+(lsGet('mks_range')||900);
async function tick(){
  if(document.hidden)return;
  const s=await api('/api/status?win='+RANGE);
  if(s.bridge!==false)$('#ip').textContent=s.ip||'не задан';
  const b=$('#state');b.textContent=s.online?(s.homing?'ПАРКОВКА':s.heating?'НАГРЕВ':(s.state||'—')+(s.stale?' …':'')):s.bridge===false?'мост выключен':'нет связи';b.style.cursor=s.online?'':'pointer';b.onclick=s.online?null:reconnect;b.title=s.online?'':'Нажми, чтобы переподключиться';b.className='badge '+(s.online?(/PRINT/.test(s.state||'')?'on':''):'off');
  $('#nozzle').innerHTML=ttH(s.nozzle);$('#bed').innerHTML=ttH(s.bed);
  // в пустом поле серым — текущая цель, как в Mainsail/OctoPrint: вписал поверх, Enter
  $('#tn').placeholder=s.nozzle?s.nozzle[1].toFixed(0):'';$('#tb').placeholder=s.bed?s.bed[1].toFixed(0):'';
  $('#prog').style.width=(s.progress||0)+'%';$('#progtxt').textContent=s.progress!=null?s.progress+'%':'—';
  $('#layer').textContent=s.layers?(s.layer_exact?'':'≈ ')+s.layer+' / '+s.layers:'—';
  $('#zv').textContent=s.pos&&s.pos.z!=null?s.pos.z.toFixed(2)+' мм':'—';
  $('#elapsed').textContent=fmt(s.elapsed);$('#eta').textContent=s.eta!=null?'≈ '+fmt(s.eta):'—';$('#fin').textContent=s.eta!=null?'· до '+finAt(s.eta):'';
  // начало печати: прошивка ждёт нагрева (M190/M109) и на координаты не отвечает — пишем, что происходит
  const warm=(k,n)=>{const t=s[k];return t&&t[1]>0&&t[0]<t[1]-2?'Нагрев '+n+': '+t[0].toFixed(0)+' → '+t[1].toFixed(0)+'°':''};
  const heat=/PRINT/.test(s.state||'')&&(s.progress||0)<1?warm('bed','стола')||warm('nozzle','сопла'):'';
  $('#file').textContent=heat||s.file||'';$('#err').textContent=s.error||'';
  HIST=s.history||[];NOW=s.now||Math.floor(Date.now()/1000);drawChart();jobs(s.jobs||[]);updateTwin(s);
  
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick()});
// ---- график: окно времени фиксировано, пропуски данных — разрыв линии
const GAP=40,RANGES=[[300,'5 мин'],[900,'15 мин'],[3600,'1 час']];
$('#ranges').innerHTML=RANGES.map(r=>'<button class="s" data-r="'+r[0]+'" onclick="setRange('+r[0]+')">'+r[1]+'</button>').join(' ');
function setRange(r){RANGE=r;lsSet('mks_range',r);document.querySelectorAll('#ranges button').forEach(b=>b.classList.toggle('on',+b.dataset.r===r));tick()}
document.querySelectorAll('#ranges button').forEach(b=>b.classList.toggle('on',+b.dataset.r===RANGE));
let hoverX=null;
function drawChart(){
  // холст ровно в физических пикселях экрана, иначе браузер растягивает картинку и линии мылятся
  const c=$('#chart'),dpr=window.devicePixelRatio||1,r=c.getBoundingClientRect(),W=Math.max(260,r.width),H=Math.max(110,r.height);
  c.width=Math.round(W*dpr);c.height=Math.round(H*dpr);const x=c.getContext('2d');x.setTransform(c.width/W,0,0,c.height/H,0,0);x.clearRect(0,0,W,H);
  const px1=v=>(Math.round(v*dpr)+.5)/dpr;   // тонкая линия точно по пикселю
  const cs=getComputedStyle(document.documentElement),bd=cs.getPropertyValue('--bd').trim(),mu=cs.getPropertyValue('--mut').trim(),fg=cs.getPropertyValue('--fg').trim(),cd=cs.getPropertyValue('--card').trim();
  const L=34,R=108,T=8,B=20,pw=W-L-R,ph=H-T-B,h=HIST,span=RANGE,now=NOW;
  const vals=[];h.forEach(p=>[1,2,3,4].forEach(k=>{if(p[k])vals.push(p[k])}));
  const mxv=Math.max(50,...vals),step=[10,20,25,50,100].find(s=>mxv/s<=5)||100,ymax=Math.ceil((mxv+1)/step)*step;
  const X=t=>L+pw-(now-t)/span*pw,Y=v=>T+ph-v/ymax*ph,SER=[[1,3,'#ef6c00'],[2,4,'#1e88e5']];
  x.font='11px system-ui,sans-serif';x.lineWidth=1;x.textAlign='right';
  for(let v=0;v<=ymax;v+=step){const y=px1(Y(v));x.strokeStyle=bd;x.beginPath();x.moveTo(L,y);x.lineTo(L+pw,y);x.stroke();x.fillStyle=mu;x.fillText(v,L-5,y+4)}
  const ti=[10,30,60,120,300,600,900,1800].find(s=>span/s<=6)||1800;x.textAlign='center';
  for(let a=0;a<=span;a+=ti){const px=px1(L+pw-a/span*pw);x.strokeStyle=bd;x.beginPath();x.moveTo(px,T);x.lineTo(px,T+ph);x.stroke();
    x.fillStyle=mu;x.fillText(a===0?'сейчас':'−'+(a>=3600?a/3600+' ч':a>=60?a/60+' мин':a+' с'),Math.min(px,W-R+10),H-5)}
  function line(k,col,tg){let prev=null;
    h.forEach(p=>{const v=p[k];if(v==null||(tg&&!v)){prev=null;return}
      if(prev&&p[0]-prev[0]>GAP)prev=null;
      if(prev){const gap=p[0]-prev[0]>GAP;x.beginPath();x.strokeStyle=col;x.lineWidth=tg?1:(gap?1.5:2);x.globalAlpha=tg?.55:(gap?.6:1);x.setLineDash(tg||gap?[4,4]:[]);
        x.moveTo(X(prev[0]),Y(prev[k]));x.lineTo(X(p[0]),Y(v));x.stroke()}
      prev=p});x.setLineDash([]);x.globalAlpha=1}
  x.save();x.beginPath();x.rect(L,T,pw,ph+1);x.clip();SER.forEach(([k,kt,col])=>{line(kt,col,true);line(k,col,false)});x.restore();
  const last=k=>{for(let i=h.length-1;i>=0;i--)if(h[i][k]!=null)return h[i];return null},tags=[];
  SER.forEach(([k,kt,col])=>{const p=last(k);if(!p||now-p[0]>GAP*2)return;
    tags.push({col,y:Y(p[k]),t:Math.round(p[k])+'°',tg:false});if(p[kt])tags.push({col,y:Y(p[kt]),t:Math.round(p[kt])+'°',tg:true})});
  // метки справа: «пилюля» с указателем на линию; факт — залитая, цель — той же формы с цветной рамкой
  x.font='bold 11px system-ui,sans-serif';
  // Близкие метки раздвигаются симметрично вокруг своих линий (группой, шаг 20 px), а хвостик метки смотрит
  // на высоту своей линии — насколько позволяет высота метки
  [false,true].forEach(tg=>{const col=tags.filter(t=>t.tg===tg).sort((a,b)=>a.y-b.y),G=20;
    let gr=col.map(t=>({ts:[t],y0:t.y}));
    for(let i=1;i<gr.length;i++){const a=gr[i-1],b=gr[i];if(b.y0-(a.y0+(a.ts.length-1)*G)<G){
      a.ts=a.ts.concat(b.ts);a.y0=a.ts.reduce((s,t)=>s+t.y,0)/a.ts.length-(a.ts.length-1)*G/2;gr.splice(i,1);i=0}}
    gr.forEach(g=>g.ts.forEach((t,i)=>t.py=g.y0+i*G));
    col.forEach(t=>{const y=Math.round(t.py),w=Math.ceil(x.measureText(t.t).width)+12,hh=17,x0=Math.round(L+pw+8+(tg?50:0)),r=5,rl=3,
        ty=Math.max(y-hh/2+1,Math.min(y+hh/2-1,Math.round(t.y))),bc=y+Math.max(-2,Math.min(2,ty-y));   // кончик хвостика и середина его основания
      x.beginPath();x.moveTo(x0-5,ty);x.lineTo(x0,bc-3.5);x.arcTo(x0,y-hh/2,x0+w,y-hh/2,rl);x.arcTo(x0+w,y-hh/2,x0+w,y+hh/2,r);
      x.arcTo(x0+w,y+hh/2,x0,y+hh/2,r);x.arcTo(x0,y+hh/2,x0,y-hh/2,rl);x.lineTo(x0,bc+3.5);x.closePath();
      if(tg){x.fillStyle=cd;x.fill();x.strokeStyle=t.col;x.lineWidth=1.5;x.stroke();x.fillStyle=t.col}else{x.fillStyle=t.col;x.fill();x.fillStyle='#fff'}
      x.textAlign='left';x.fillText(t.t,x0+6,y+4)})});
  x.font='11px system-ui,sans-serif';
  if(hoverX!=null&&h.length>1&&hoverX>=L&&hoverX<=L+pw){
    const t=now-(L+pw-hoverX)/pw*span;let p=h[0];h.forEach(q=>{if(Math.abs(q[0]-t)<Math.abs(p[0]-t))p=q});const px=X(p[0]);
    x.strokeStyle=fg;x.globalAlpha=.4;x.lineWidth=1;x.beginPath();x.moveTo(px,T);x.lineTo(px,T+ph);x.stroke();x.globalAlpha=1;
    const f=(a,b)=>a==null?'нет данных':a.toFixed(0)+'°'+(b?' → '+b.toFixed(0)+'°':'');
    const txt='−'+Math.round(now-p[0])+' с   сопло '+f(p[1],p[3])+'   стол '+f(p[2],p[4]),w=x.measureText(txt).width+14,bx=Math.min(Math.max(px-w/2,L),L+pw-w);
    x.fillStyle=cd;x.strokeStyle=bd;x.fillRect(bx,T+2,w,20);x.strokeRect(bx,T+2,w,20);x.fillStyle=fg;x.textAlign='left';x.fillText(txt,bx+7,T+16);
    SER.forEach(([k,kt,col])=>{if(p[k]!=null){x.fillStyle=col;x.beginPath();x.arc(px,Y(p[k]),4,0,7);x.fill()}});
  }
}
(function(){const c=$('#chart');const set=e=>{const q=e.touches?e.touches[0]:e;hoverX=q.clientX-c.getBoundingClientRect().left;drawChart()};
  c.addEventListener('mousemove',set);c.addEventListener('touchstart',set,{passive:true});c.addEventListener('touchmove',set,{passive:true});
  c.addEventListener('mouseleave',()=>{hoverX=null;drawChart()});window.addEventListener('resize',drawChart)})();
// ---- задачи загрузки
function jobs(list){
  const box=$('#jobs');box.textContent='';
  list.slice().reverse().forEach(j=>{
    if(seen[j.id]&&seen[j.id]!==j.state&&j.state!=='working'){toast((j.state==='done'?'✔ ':'✖ ')+j.note);if(j.state==='done'&&$('#files').children.length)loadFiles()}
    seen[j.id]=j.state;
    if(j.age!=null&&j.age>(j.state==='error'?3600:600))return;   // старые загрузки не загромождают «Файлы»
    const d=document.createElement('div');d.className='job '+j.state;const n=document.createElement('b');n.textContent=j.name;d.appendChild(n);
    const p=j.total?Math.round(j.sent*100/j.total):0;
    if(j.state==='working'){const bar=document.createElement('div');bar.className='bar';const i=document.createElement('i');i.style.width=p+'%';bar.appendChild(i);d.appendChild(bar)}
    const m=document.createElement('div');m.className='mut n';m.textContent=(j.state==='working'&&j.total?p+'% · ':'')+j.note;d.appendChild(m);box.appendChild(d)});
}
const drop=$('#drop'),pick=$('#pick');drop.onclick=()=>pick.click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add('h')};drop.ondragleave=()=>drop.classList.remove('h');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('h');sendFiles(e.dataTransfer.files)};pick.onchange=()=>{sendFiles(pick.files);pick.value=''};
function sendFiles(fl){[...fl].forEach(f=>{
  const x=new XMLHttpRequest();x.open('POST','/api/upload?name='+encodeURIComponent(f.name)+'&print='+($('#startAfter').checked?1:0));
  x.setRequestHeader('X-Requested-With','mks');x.upload.onprogress=e=>{drop.firstChild.textContent='Передача на мост: '+f.name+' '+Math.round(e.loaded*100/e.total)+'%'};
  const done=err=>{drop.firstChild.textContent='Перетащи .gcode сюда или нажми, чтобы выбрать';if(err)toast('✖ '+f.name+': '+err);tick()};
  x.onload=()=>{let e=null;if(x.status!==200){try{e=JSON.parse(x.responseText).error}catch(_){}e=e||('мост ответил HTTP '+x.status)}done(e)};
  x.onerror=()=>done('связь с мостом оборвалась');x.send(f)})}
// ---- файлы
async function loadFiles(){
  const box=$('#files');box.textContent='Загрузка…';const r=await api('/api/list');if(r.error){box.textContent=r.error;return}
  box.textContent='';if(!r.files.length){box.textContent=r.cached?'Во время печати принтер не отдаёт список файлов. Он появится после печати.':'Файлов нет';return}
  if(r.cached||r.partial){const n=document.createElement('div');n.className='mut sm';n.textContent=r.partial?'Принтер прислал список не целиком — нажми «Обновить».':'Во время печати принтер не отдаёт список: показан последний известный.';box.appendChild(n)}
  r.files.forEach(n=>{const l=document.createElement('label'),c=document.createElement('input');c.type='checkbox';c.value=n;l.appendChild(c);l.appendChild(document.createTextNode(n));box.appendChild(l)})}
const sel=()=>[...document.querySelectorAll('#files input:checked')].map(c=>c.value);
async function delSel(){const n=sel();if(!n.length){toast('Отметь файлы для удаления');return}
  if(!await ask(n.length===1?'Удалить файл с принтера?':'Удалить '+n.length+' файл(ов) с принтера?',n.join(', '),'Удалить',1))return;
  toast('Удаляю…');const r=await api('/api/delete',{names:n});toast(r.error||('Удалено: '+r.results.filter(x=>x.ok).length+' из '+n.length));loadFiles()}
async function printSel(){const n=sel();if(n.length!==1){toast('Выбери один файл');return}
  if(!await ask('Запустить печать?',n[0],'Печатать'))return;const r=await api('/api/print',{name:n[0]});toast(r.error||'Печать запущена')}
// ---- консоль
// [заголовок, пояснение, кнопка, красная]
const DANGER={G28:['Парковка','Головка и стол поедут к концевикам. Проверь, что на столе и под соплом ничего нет.','Парковать'],
 M26:['Остановить печать?','Печать прервётся, продолжить её с этого места будет нельзя.','Остановить',1],
 M112:['Аварийный стоп','Принтер немедленно остановится и потребует перезагрузки по питанию.','Аварийный стоп',1],
 M500:['Записать в EEPROM?','Текущие настройки платы сохранятся в её память и переживут перезагрузку.','Записать'],
 M502:['Сброс к заводским','Шаги, ускорения и PID вернутся к значениям прошивки. В память не запишется, пока не выполнишь M500.','Сбросить',1],
 M303:['Калибровка PID','Сопло будет греться и остывать несколько минут. Не оставляй принтер без присмотра.','Запустить']};
const CMDH=[];let hi=0;
function log(t,c){const l=$('#log'),d=document.createElement('div');d.textContent=t;if(c)d.style.color=c;l.appendChild(d);l.scrollTop=l.scrollHeight}
function homeAnim(c){const ax=c.trim().toUpperCase().split(/\s+/).slice(1).filter(a=>/^[XYZ]$/.test(a)),all=!ax.length,h=a=>all||ax.includes(a);
  holdMove(h('X')?0:null,h('Y')?0:null,h('Z')?0:null,feedXY(50),Math.min(LIM.z||5,5),3500)}
async function cmd(c){const tok=c.trim().split(/\s+/)[0].toUpperCase(),dg=DANGER[tok];if(dg&&!await ask(dg[0],dg[1],dg[2],dg[3]))return;
  if(tok==='G28'&&!/PRINT|PAUSE/.test(LASTS.state||'')){homeAnim(c);HOMING=Date.now()}
  tab('log');log('> '+c);const r=await api('/api/console',{cmd:c,force:$('#force').checked});
  if(r.error){if(tok==='G28')HOMING=0;log(r.error,'#ef5350');return false}
  else{log(r.reply||(r.homing?'(принтер паркуется: пока не закончит, остальные команды недоступны)':'(команда отправлена, ответа нет)'));if(r.homing)tick();return true}}
function sendCon(){const v=$('#cin').value.trim();if(!v)return;CMDH.push(v);hi=CMDH.length;cmd(v);$('#cin').value=''}
function conKey(e){if(e.key==='Enter')sendCon();else if(e.key==='ArrowUp'&&CMDH.length){hi=Math.max(0,hi-1);$('#cin').value=CMDH[hi];e.preventDefault()}
  else if(e.key==='ArrowDown'&&CMDH.length){hi=Math.min(CMDH.length,hi+1);$('#cin').value=CMDH[hi]||'';e.preventDefault()}}
// ---- справочник команд: [код, описание, режим s=отправить сразу / t=подставить в поле]
const REF=[
['Информация',[['M105','Температуры сопла и стола: текущая / заданная','s'],['M114','Текущие координаты X Y Z E','s'],['M115','Версия прошивки и список возможностей','s'],
 ['M503','Все настройки платы: шаги, ускорения, PID, смещения','s'],['M119','Состояние концевиков (сработал или нет)','s'],['M20','Список файлов на карте памяти','s'],
 ['M27','Прогресс печати с карты, в процентах (у MKS)','s'],['M992','MKS: сколько времени идёт печать (чч:мм:сс)','s'],['M994','MKS: имя печатаемого файла','s'],['M997','MKS: состояние (IDLE, PRINTING, PAUSE)','s']]],
['Движение',[['G28','Домой по всем осям (с подтверждением)','s'],['G28 X Y','Домой только по X и Y (не трогая Z)','s'],['G90','Абсолютные координаты (обычный режим)','s'],
 ['G91','Относительные координаты. После перемещений верни G90!','s'],['G1 X','Переместиться: G1 X50 Y50 F3000 (F — скорость, мм/мин)','t'],['G92','Задать текущую позицию без движения: G92 X0 Y0 Z0','t'],
 ['M84','Выключить моторы: оси можно двигать руками','s'],['M400','Дождаться завершения всех движений','s']]],
['Температура и обдув',[['M104 S','Сопло: задать температуру и не ждать. M104 S200; S0 — выключить','t'],['M109 S','Сопло: задать температуру и дождаться нагрева','t'],
 ['M140 S','Стол: задать температуру и не ждать. M140 S60; S0 — выключить','t'],['M190 S','Стол: задать температуру и дождаться нагрева','t'],
 ['M106 S','Вентилятор обдува модели, 0–255. M106 S255 — на полную','t'],['M107','Выключить вентилятор обдува','s']]],
['Печать',[['M23','Выбрать файл на карте: M23 имя.gcode','t'],['M24','Начать выбранный файл / продолжить после паузы','s'],['M25','Пауза печати','s'],
 ['M26','Остановка печати (у MKS так, по Cura-плагину; в чистом Marlin M26 задаёт позицию в файле)','s'],['M220 S','Скорость печати в процентах: M220 S120 — на 20% быстрее','t'],
 ['M221 S','Поток пластика в процентах: M221 S95','t'],['M290 Z','Подстройка высоты Z «на лету», мм: M290 Z-0.05 — ближе к столу. Не сохраняется','t'],
 ['M117','Показать текст на экране принтера: M117 Привет','t'],['M300 S','Звуковой сигнал: M300 S440 P200 (частота Гц, длительность мс)','t']]],
['Калибровка и сервис',[['M900 K','Linear Advance: коэффициент K. M900 K0.04; K0 — выключить','t'],['M303','Автокалибровка PID: M303 E0 S210 C8 (греет сопло, нужен присмотр)','t'],
 ['M500','Сохранить текущие настройки в память платы (EEPROM)','s'],['M501','Загрузить настройки из памяти платы','s'],['M502','Сбросить настройки к заводским значениям (не сохраняя)','s'],
 ['M112','АВАРИЙНЫЙ стоп. Нужна перезагрузка принтера','s']]]];
function renderRef(){
  const q=($('#refq').value||'').toLowerCase(),box=$('#reflist');box.textContent='';
  REF.forEach(([g,items])=>{const it=items.filter(i=>!q||i[0].toLowerCase().includes(q)||i[1].toLowerCase().includes(q));if(!it.length)return;
    const h=document.createElement('div');h.className='rg';h.textContent=g;box.appendChild(h);
    it.forEach(([c,d,m])=>{const r=document.createElement('div');r.className='rr'+(DANGER[c.split(' ')[0]]?' dg':'');
      const b=document.createElement('code');b.textContent=c;b.title='Вставить в строку ввода';
      b.onclick=()=>{const i=$('#cin');i.value=m==='s'||/[SKZX]$/.test(c)?c:c+' ';i.focus();i.setSelectionRange&&i.setSelectionRange(i.value.length,i.value.length)};
      const s=document.createElement('span');s.textContent=d;r.appendChild(b);r.appendChild(s);box.appendChild(r)})});
}
// ---- движение и управление
let STEP=10;
$('#steps').innerHTML=[0.1,1,10,50].map(s=>'<button data-s="'+s+'" onclick="setStep('+s+')">'+s+'</button>').join('');
function setStep(s){STEP=s;$('#zstep').textContent=s;document.querySelectorAll('#steps button').forEach(b=>b.classList.toggle('on',+b.dataset.s===s))}setStep(10);
async function mv(a,sign){const d=sign*STEP;
  if(d<0&&a==='Z'&&STEP>=10&&!await ask('Сблизить сопло и стол на '+STEP+' мм?','Проверь, что под соплом ничего нет.','Сблизить'))return;
  log('> '+a+(d>0?'+':'')+d+' мм');
  let plan;{const c=TGTP||CUR||{x:0,y:0,z:0},lim={X:GEOM.bx,Y:GEOM.by,Z:GEOM.bz}[a],nv=Math.max(0,Math.min(lim,c[a.toLowerCase()]+d)),t={x:c.x,y:c.y,z:c.z};t[a.toLowerCase()]=nv;
   plan=holdPlan(t.x,t.y,t.z,feedXY(50),Math.min(10,LIM.z||10))}
  const r=await guardedMove(plan,()=>api('/api/move',{axis:a,dist:d}));if(r.error)log(r.error+(r.reply?' · '+r.reply.split('\n')[0]:''),'#ef5350');else log(r.reply||'(команда отправлена)');tick()}
function zMove(up){mv('Z',(up?1:-1)*(GEOM.z_bed?-1:1))}
function zTitles(){const b=GEOM.z_bed;$('#zUp').title=b?'Стол вверх, ближе к соплу (Z−)':'Сопло вверх (Z+)';$('#zDn').title=b?'Стол вниз, дальше от сопла (Z+)':'Сопло вниз (Z−)';$('#zcap').textContent=b?'Стол':'Сопло'}
let ESTEP=5;
$('#esteps').innerHTML=[1,5,10,50].map(s=>'<button data-s="'+s+'" onclick="setEStep('+s+')">'+s+'</button>').join('');
function setEStep(s){ESTEP=s;document.querySelectorAll('#esteps button').forEach(b=>b.classList.toggle('on',+b.dataset.s===s))}setEStep(5);
async function ex(sign){const d=sign*ESTEP;log('> пруток '+(d>0?'+':'')+d+' мм');const r=await api('/api/extrude',{dist:d});
  if(r.error){log(r.error,'#ef5350');return}log(r.reply||'(отправлено)');const m=/E:\s*(-?[\d.]+)/.exec(r.reply||'');setE(m?parseFloat(m[1]):(tgtE||0)+d);tick()}
function tab(n){$('#pLog').hidden=n!=='log';$('#pRef').hidden=n!=='ref';$('#clr').style.visibility=n==='log'?'visible':'hidden';document.querySelectorAll('.tb').forEach(b=>b.classList.toggle('on',b.dataset.t===n))}
function fanLabel(){const r=$('#fanr'),v=r.value;$('#fanv').textContent=v+'%';if(r.style&&r.style.setProperty)r.style.setProperty('--v',v+'%')}
async function setFan(){const v=+$('#fanr').value;log('> обдув '+v+'%');const r=await api('/api/fan',{percent:v});r.error?log(r.error,'#ef5350'):log(r.reply||'(отправлено)');tick()}
// ---- цифровая копия принтера
let GEOM={bx:255,by:210,bz:210,z_bed:true},TW=null,FR=null,visE=0,tgtE=null,eRaf=0,lastT=0,LASTS={},ARM=null,DOWN=null;
// Каретка X выходит за стол: слева пластина на 28 мм, справа гайка прижима BMG на 33 мм; по Y пластина ±24 мм.
// Направляющие Y стоят дальше, с половиной каретки Y (8 мм) и зазором — так каретки не наезжают друг на друга.
const FRAME={railL:38,railR:44,ml:54,mr:60,mt:34,mb:60},MINZ=0.5,SNAP=7;   // MINZ — ниже по клику не опускаем: промах мышью не должен вогнать сопло в стол
// SNAP — полоса вокруг рабочей зоны (в единицах рисунка): клик в ней даёт крайнее значение, иначе в 0/максимум не попасть
// плавный градиент: серый -> жёлтый -> оранжевый -> красный (непрерывный, без порогов)
const STOPS=[[0,[100,116,139]],[.28,[245,176,20]],[.62,[240,100,20]],[1,[215,38,38]]];
function tcol(t,max){const r=t==null?0:Math.max(0,Math.min(1,(t-25)/(max-25)));let i=1;while(i<STOPS.length-1&&r>STOPS[i][0])i++;
  const a=STOPS[i-1],b=STOPS[i],k=(r-a[0])/(b[0]-a[0]);return'rgb('+a[1].map((v,j)=>Math.round(v+(b[1][j]-v)*k)).join(',')+')'}
const LBL='style="fill:#fff;paint-order:stroke;stroke:#000a;stroke-width:3;font-weight:700"';
const HIT='fill="transparent"',RING='fill="none" class="ring" stroke-width="2" stroke-dasharray="5 3" style="stroke:var(--acc);display:none"';
// ---- голова принтера: экструдер BMG + хотэнд E3D V6 в реальных пропорциях (мм). Начало координат — кончик сопла,
// y вверх отрицательный. Один генератор для карточки «Экструдер» (крупно, подробно) и вида спереди (мелко, схематично).
// ---- голова принтера: экструдер BMG + хотэнд E3D V6. Размеры V6 — по официальным чертежам E3D (V6-175-SINK,
// V6-BLOCK-CARTRIDGE, V6-NOZZLE, V6-175-BREAK; общая длина 62.3 мм). Начало координат — кончик сопла, y вверх отрицательный.
// Один генератор для карточки «Экструдер» (крупно, подробно) и вида спереди (мелко, схематично) — пропорции совпадают.
const V6={hex:[-5,-2],gap:[-6,-5],blk:[-8,-17.5,23,11.5],cart:[8.5,-10,3],therm:[-5.5,-11.75,1.55],brk:[-19.6,-17.5],
  fins:[-45.6,-19.6],neck:[-49.6,-45.6],col1:[-52.6,-49.6],groove:[-58.6,-52.6],col2:[-62.3,-58.6]};
// BMG — размеры сняты с CAD-модели корпуса под оригинальные шестерни Bondtech (printables.com/model/43337):
// корпус 42×54 мм, вал мотора в центре квадрата NEMA17 (21; 21.3 от низа), ведущий вал на 16.7 мм выше (пара 17/50 зубьев,
// модуль 0.5), пруток по оси посадки хотэнда; рычаг: шарнир и ось прижимного колеса в 17.4 мм друг от друга.
// Перевод из координат корпуса (x' от левого края, y' от низа): x = x' − 25, y = −49.6 − y'.
const BMG_BODY=[[4,0],[38,0],[42,4],[42,49.6],[37.6,54],[18.7,54],[0,38.6],[0,4]],
  BMG_LATCH=[[15,55.2],[18.3,53],[18.3,7.4],[14.4,1.3],[10.6,.6],[6.7,2.4],[6.1,7.4],[8.3,13],[2.2,17.4],[.2,20.8],[2.2,24.1],[5.6,25.2],[11.1,25.8],[13.3,28],[14.4,53]];
const HEAD={top:123,hob:[-4.7,-87.6],big:12.5,pin:[-4,-70.9],pinR:4.25,idl:[4.7,-87.8],hobR:3.65,l:-26,r:35,rodY:-58.6};
const EK=2,GEAR_K=50/17,HOB_DEG=360/(2*Math.PI*HEAD.hobR);   // передаточное BMG; градусов колеса на 1 мм прутка
function headSVG(p,k,detail){
  // detail — карточка «Экструдер»: подробно и в разрезе по оси прутка (как на чертежах Mellow/Trianglelab);
  // иначе — вид спереди принтера: снаружи, схематично
  const n=v=>+(v*k).toFixed(2),R=(x,y,w,h,a)=>'<rect x="'+n(x)+'" y="'+n(y)+'" width="'+n(w)+'" height="'+n(h)+'" '+(a||'')+'/>';
  const C=(x,y,r,a)=>'<circle cx="'+n(x)+'" cy="'+n(y)+'" r="'+n(r)+'" '+(a||'')+'/>',sw=detail?1.2:.8;
  const ln='style="fill:var(--card);stroke:var(--fg)" stroke-width="'+sw+'"',metal='style="fill:var(--mut)"',H=HEAD,B=V6.blk;
  const cyl=(y0,y1,d,a)=>R(-d/2,y0,d,y1-y0,a);   // тело вращения в проекции — прямоугольник по диаметру
  const cut='style="fill:var(--bg)"';   // полость в разрезе
  let s='';
  // ---------- хотэнд V6 ----------
  s+=cyl(V6.col2[0],V6.col2[1],16,ln)+cyl(V6.groove[0],V6.groove[1],12,'style="fill:var(--bd);stroke:var(--mut)" stroke-width="'+sw*.7+'"')+cyl(V6.col1[0],V6.col1[1],16,ln);
  s+=cyl(V6.neck[0],V6.neck[1],10,'style="fill:var(--bd)"')+cyl(V6.fins[0],V6.fins[1],10,'style="fill:var(--bd)"');
  for(let i=0;i<11;i++){const yb=V6.fins[1]-i*2.5;s+=R(-11.15,yb-1,22.3,1,metal+' rx="'+n(.4)+'"')}   // 11 рёбер Ø22.3, 1 мм, шаг 2.5
  if(detail){   // разрез радиатора: канал Ø4.2 с PTFE сверху, резьба M7 под термобарьер снизу (15.1 мм)
    s+=cyl(V6.col2[0],V6.fins[1]-15.1,4.2,cut)+cyl(V6.fins[1]-15.1,V6.fins[1],7,cut);
    // термобарьер (V6-175-BREAK): в радиаторе M7 на 15 мм, сверху гнездо Ø4.1 — в него на треть заходит PTFE-трубка
    const bt=V6.fins[1]-15;
    s+=cyl(bt,V6.brk[1],7,'style="fill:var(--mut);stroke:var(--fg)" stroke-width=".6"')+cyl(bt,bt+5,4.1,cut)+cyl(bt+5,V6.brk[1],2,cut);
    s+=cyl(-80,bt+5,4,'style="fill:#e5e7eb" fill-opacity=".9"');   // PTFE-трубка от колёс BMG в термобарьер
  }
  // промежуток 2.1 мм: тонкая трубка Ø3 со стенкой 0.5 мм — тут тепло и отсекается
  s+=cyl(V6.brk[0],V6.brk[1],3,'style="fill:var(--mut);stroke:var(--fg)" stroke-width="'+(detail?.5:.3)+'"');
  if(detail)s+=cyl(V6.brk[0],V6.brk[1],2,cut);
  // блок 23×11.5: сопло в 8 мм от левого торца, справа картридж Ø6 (торец к нам), слева термистор Ø3.1
  s+='<g id="'+p+'Block" fill="#64748b">'+R(B[0],B[1],B[2],B[3],'rx="'+n(.6)+'" style="stroke:var(--fg)" stroke-width="'+sw+'"')+'</g>';
  if(detail)s+=cyl(B[1],B[1]+5,6,'style="fill:var(--mut)"')+cyl(B[1],B[1]+5,2,cut);   // термобарьер в блоке (5 мм)
  s+=R(V6.cart[0]+V6.cart[2],V6.cart[1]-.5,B[0]+B[2]-V6.cart[0]-V6.cart[2],1,'fill="#000" fill-opacity=".45"');   // прорезь зажима
  s+=C(V6.cart[0],V6.cart[1],V6.cart[2],'fill="#2b2f33" style="stroke:var(--fg)" stroke-width="'+sw*.7+'"');
  if(detail){s+=C(V6.cart[0],V6.cart[1],1.6,'fill="#3a3f45"');
    s+=C(V6.therm[0],V6.therm[1],V6.therm[2],'fill="#2b2f33" style="stroke:var(--fg)" stroke-width=".6"');
    s+=R(11.6,B[1]-1.6,3.4,1.6,metal+' rx="'+n(.5)+'"')}   // головка винта зажима сверху
  // два провода картриджа: из торца вверх, справа в обход рёбер радиатора (Ø22.3), и в низ корпуса экструдера,
  // где на каретке стоит плата головы; корпус рисуется позже и закрывает их концы
  const wire=(dx,w)=>{const xs=14+dx,P=(x,y)=>n(x)+' '+n(y);
    return '<path d="M'+P(V6.cart[0]+dx,V6.cart[1])+' C'+P(V6.cart[0]+dx+1,-16)+' '+P(xs,B[1]+.5)+' '+P(xs,-22)+
      ' L'+P(xs,-44)+' C'+P(xs,-47)+' '+P(12.4+dx*.6,-48)+' '+P(12.4+dx*.6,-50.5)+
      '" fill="none" stroke="#b91c1c" stroke-width="'+n(w)+'" stroke-linecap="round" stroke-linejoin="round"/>'};
  s+=wire(-.8,detail?.9:.7)+wire(.8,detail?.9:.7);
  // сопло V6: резьба в блоке (в разрезе), видимый зазор 0.5 мм, шестигранник 7×3, острый конус к кончику Ø1 — цвет по температуре целиком
  s+=cyl(V6.gap[0]+.5,V6.gap[1],6,'style="fill:var(--mut)"');
  s+='<g id="'+p+'Noz" fill="#64748b">'+(detail?cyl(B[1]+5,B[1]+B[3],6,'style="stroke:var(--fg)" stroke-width=".5"'):'')+
     R(-3.5,V6.hex[0],7,3,'style="stroke:var(--fg)" stroke-width="'+sw*.75+'"')+
     '<polygon points="'+[[-2.4,-2],[2.4,-2],[.5,0],[-.5,0]].map(q=>n(q[0])+','+n(q[1])).join(' ')+'" style="stroke:var(--fg)" stroke-width="'+sw*.75+'"/></g>';
  if(detail)s+=[-1.75,1.75].map(x=>'<line x1="'+n(x)+'" y1="'+n(-5)+'" x2="'+n(x)+'" y2="'+n(-2)+'" style="stroke:var(--bg)" stroke-opacity=".55"/>').join('')+
     cyl(B[1]+5,-.3,1,cut);   // канал сопла
  // ---------- экструдер BMG ----------
  const P=(pts,tx,ty,a)=>'<polygon points="'+pts.map(q=>n(q[0]+tx)+','+n(ty-q[1])).join(' ')+'" '+a+'/>';
  s+=P(BMG_BODY,-25,-49.6,ln+' stroke-linejoin="round"');
  s+=R(-3.5,-105.6,7,2,ln)+R(-4.6,-106.8,9.2,1.2,'style="fill:var(--mut)"')+R(-3.8,-108.4,7.6,1.6,ln+' rx="'+n(.7)+'"');   // фитинг: основание, кольцо цанги, шляпка
  const xr=detail?'style="fill:var(--bg);stroke:var(--mut)"':'fill="none" style="stroke:var(--mut)" stroke-opacity=".75"';
  const gear=(id,x,y,r,teeth,spokes)=>{const per=2*Math.PI*r/teeth;
    return '<g transform="translate('+n(x)+','+n(y)+')"><g id="'+p+id+'">'+
      (detail?'<circle r="'+n(r)+'" fill="none" style="stroke:var(--fg)" stroke-width="'+n(.9)+'" stroke-dasharray="'+n(per*.5)+' '+n(per*.5)+'"/>':'')+
      '<circle r="'+n(r-(detail?.9:0))+'" '+xr+' stroke-width="'+(detail?.9:.6)+'"/>'+
      (spokes?[0,120,240].map(a=>'<line x1="0" y1="0" x2="'+n((r-1.5)*Math.cos(a*Math.PI/180))+'" y2="'+n((r-1.5)*Math.sin(a*Math.PI/180))+'" style="stroke:var(--mut)" stroke-width="'+n(.8)+'"/>').join(''):'')+
      '<circle r="'+n(Math.min(1.4,r/3))+'" style="fill:var(--mut)"/></g></g>'};
  s+=gear('Big',H.hob[0],H.hob[1],H.big,50,true)+gear('Pin',H.pin[0],H.pin[1],H.pinR,17,true)+gear('Hob',H.hob[0],H.hob[1],H.hobR+.5,20,false);
  // винт прижима: вкручен в корпус у правого верхнего края, проходит сквозь лапку рычага, пружина — между лапкой и гайкой
  const sy=-49.6-51.3;
  if(detail)s+=R(11,sy-.8,22,1.6,'style="fill:var(--mut)"');   // стержень винта (его часть в корпусе видна в разрезе)
  // рычаг: шарнир (10.6; 4.7) совпадает с отверстием корпуса (36.4; 22.1) → сдвиг (25.8; 17.4)
  s+=P(BMG_LATCH,.8,-67,'style="fill:var(--bg);stroke:var(--fg)" stroke-width="'+sw+'" stroke-linejoin="round"');
  s+=C(.8+10.6,-67-4.7,1.5,'style="fill:var(--card);stroke:var(--mut)" stroke-width=".6"');
  s+=R(19.1,sy-.8,8,1.6,'style="fill:var(--mut)"');   // винт снаружи лапки
  s+='<path d="M'+n(19.3)+' '+n(sy)+[...Array(9)].map((_,i)=>' L'+n(19.9+i*.75)+' '+n(sy+(i%2?2.6:-2.6))).join('')+' L'+n(26.8)+' '+n(sy)+'" fill="none" style="stroke:var(--fg)" stroke-width="'+n(detail?.45:.35)+'"/>';   // пружина
  s+=R(27,sy-4.3,6.2,8.6,'style="fill:var(--mut);stroke:var(--fg)" stroke-width="'+sw*.6+'" rx="'+n(1)+'"');   // рифлёная гайка (до 60 мм от левого края корпуса)
  if(detail)s+=[0,1,2,3,4].map(i=>'<line x1="'+n(27.8+i*1.15)+'" y1="'+n(sy-4)+'" x2="'+n(27.8+i*1.15)+'" y2="'+n(sy+4)+'" style="stroke:var(--bg)" stroke-opacity=".45" stroke-width=".7"/>').join('');
  s+=gear('Idl',H.idl[0],H.idl[1],H.hobR+.5,20,false);   // прижимное колесо на рычаге
  return s}
// плашка температуры на видах принтера: цветная точка (цвет — температура), факт жирно, цель приглушённо;
// ширина подгоняется под текст при каждом обновлении. a — привязка: start/middle/end
function pill(id,x,y,fs,a,lx,ly){return '<g id="'+id+'" data-a="'+(a||'middle')+'" data-fs="'+fs+'"'+(lx!=null?' data-lx="'+lx+'" data-ly="'+ly+'"':'')+' transform="translate('+x+','+y+')" style="pointer-events:none">'+
  (lx!=null?'<line class="pll" style="stroke:var(--mut)" stroke-width="'+(fs*.09)+'"/><circle class="pld" cx="'+lx+'" cy="'+ly+'" r="'+(fs*.16)+'" style="fill:var(--mut)"/>':'')+
  '<rect rx="'+(fs*.8)+'" height="'+(fs*1.7)+'" y="'+(-fs*.85)+'" style="fill:var(--card);fill-opacity:.93;stroke:var(--bd)" stroke-width="'+(fs*.1)+'"/>'+
  '<circle class="pdot" r="'+(fs*.32)+'" fill="#64748b"/><text font-size="'+fs+'" y="'+(fs*.36)+'" style="fill:var(--fg)"><tspan class="pl" style="fill:var(--mut)"></tspan>'+
  '<tspan class="pv" font-weight="700"></tspan><tspan class="pt" style="fill:var(--mut)"></tspan></text></g>'}
function setPill(id,t,col,label){const g=$(id);if(!g)return;const fs=+g.dataset.fs,tx=g.querySelector('text');
  g.querySelector('.pl').textContent=label?label+' ':'';g.querySelector('.pv').textContent=t?Math.round(t[0])+'°':'—';
  g.querySelector('.pt').textContent=t&&t[1]?' / '+Math.round(t[1])+'°':'';
  let tl=0;try{tl=tx.getComputedTextLength()}catch(_){}if(!tl)tl=tx.textContent.length*fs*.55;
  const pad=fs*.6,dot=fs*.64+fs*.4,w=Math.ceil(pad+dot+tl+pad),a=g.dataset.a,x0=a==='end'?-w:a==='start'?0:-w/2,r=g.querySelector('rect'),c=g.querySelector('.pdot');
  r.setAttribute('x',x0);r.setAttribute('width',w);c.setAttribute('cx',x0+pad+fs*.32);c.setAttribute('fill',col);tx.setAttribute('x',x0+pad+dot);
  const L=g.querySelector('.pll');if(L){   // выноска — от ближайшего края плашки
    const lx=+g.dataset.lx,ly=+g.dataset.ly,h=fs*.85;let sx,sy;
    if(lx<x0){sx=x0;sy=0}else if(lx>x0+w){sx=x0+w;sy=0}else{sx=Math.max(x0+h,Math.min(x0+w-h,lx));sy=ly<0?-h:h}
    L.setAttribute('x1',sx);L.setAttribute('y1',sy);L.setAttribute('x2',lx);L.setAttribute('y2',ly)}}
function buildTwin(){
  const g=GEOM,F=FRAME,sc=300/(g.bx+F.ml+F.mr),bw=g.bx*sc,bh=g.by*sc,ox=F.ml*sc,oy=F.mt*sc,
    rl=ox-F.railL*sc,rr=ox+bw+F.railR*sc,ty=26*sc;   // направляющие Y; выезд каретки по Y за стол
  TW={s:sc,ox,oy,bw,bh,rl,rr,vbH:Math.round((g.by+F.mt+F.mb)*sc)};
  $('#svgTop').setAttribute('viewBox','0 0 300 '+TW.vbH);
  {const tip=Math.round(6+HEAD.top*sc),T=g.bz*sc,fl=tip+4+T,bot=fl+24;FR={l:ox,bw:bw,tip:tip,T:T,floor:fl,bot:bot,vbH:Math.round(bot+36),s:sc,
    beam:Math.round(tip+HEAD.rodY*sc)}}   // верхняя балка рамы — на уровне валов X, фидер торчит над ней
  $('#svgFront').setAttribute('viewBox','0 0 300 '+FR.vbH);   // вид спереди использует ту же ось X, что и вид сверху
  let grid='';for(let v=50;v<g.bx;v+=50)grid+='<line x1="'+(ox+v*sc)+'" y1="'+oy+'" x2="'+(ox+v*sc)+'" y2="'+(oy+bh)+'" stroke="#fff" stroke-opacity=".25"/>';
  for(let v=50;v<g.by;v+=50)grid+='<line x1="'+ox+'" y1="'+(oy+bh-v*sc)+'" x2="'+(ox+bw)+'" y2="'+(oy+bh-v*sc)+'" stroke="#fff" stroke-opacity=".25"/>';
  const rod='style="fill:var(--mut)"';
  $('#svgTop').innerHTML=
   '<rect x="'+(rl-12*sc)+'" y="'+(oy-ty-4)+'" width="'+(rr-rl+24*sc)+'" height="'+(bh+2*ty+8)+'" rx="8" fill="none" style="stroke:var(--bd)" stroke-width="3"/>'+
   '<line x1="'+rl+'" y1="'+(oy-ty)+'" x2="'+rl+'" y2="'+(oy+bh+ty)+'" style="stroke:var(--mut)" stroke-width="'+(8*sc)+'" stroke-linecap="round" opacity=".75"/><line x1="'+rr+'" y1="'+(oy-ty)+'" x2="'+rr+'" y2="'+(oy+bh+ty)+'" style="stroke:var(--mut)" stroke-width="'+(8*sc)+'" stroke-linecap="round" opacity=".75"/>'+
   '<rect id="tBed" x="'+ox+'" y="'+oy+'" width="'+bw+'" height="'+bh+'" rx="4" fill="#64748b" fill-opacity=".9" style="stroke:var(--mut)"/>'+grid+
   '<circle cx="'+ox+'" cy="'+(oy+bh)+'" r="2.5" style="fill:var(--fg)"/>'+
   '<g id="tModel" transform="translate('+ox+','+(oy+bh)+') scale('+(sc/10)+','+(-sc/10)+')" style="display:none;pointer-events:none">'+
    '<path id="tGhost" vector-effect="non-scaling-stroke" fill="none" stroke-linejoin="round" stroke-linecap="round" stroke="#fff" stroke-opacity=".22" stroke-width="1"/><path id="tLayF" fill="#fff" fill-opacity=".22" fill-rule="evenodd" stroke="none"/><path id="tLayD" vector-effect="non-scaling-stroke" fill="none" stroke-linejoin="round" stroke-linecap="round" stroke="#fff" stroke-opacity=".6" stroke-width="1"/><path id="tLayH" vector-effect="non-scaling-stroke" fill="none" stroke-linejoin="round" stroke-linecap="round" stroke="#000" stroke-opacity=".45" stroke-width="3.2"/>'+
    '<path id="tLay" vector-effect="non-scaling-stroke" fill="none" stroke-linejoin="round" stroke-linecap="round" stroke="#fff" stroke-width="1.6"/></g>'+
   '<g id="tGant">'+[-1,1].map(d=>'<rect x="'+rl+'" y="'+((d*18-4)*sc)+'" width="'+(rr-rl)+'" height="'+(8*sc)+'" rx="'+(4*sc)+'" '+rod+' opacity=".9"/>').join('')+
   [rl,rr].map(x=>'<rect x="'+(x-8*sc)+'" y="'+(-26*sc)+'" width="'+(16*sc)+'" height="'+(52*sc)+'" rx="2" style="fill:var(--bd);stroke:var(--mut)" stroke-width="1"/>').join('')+'</g>'+

   '<rect id="tZone" x="'+(ox-SNAP)+'" y="'+(oy-SNAP)+'" width="'+(bw+2*SNAP)+'" height="'+(bh+2*SNAP)+'" rx="6" style="fill:var(--acc);fill-opacity:.09;stroke:var(--acc);stroke-opacity:.5;display:none;pointer-events:none" stroke-width="1"/>'+
   // каретка сверху: пластина на двух валах X (±18 мм), на ней корпус BMG 42×33 над осью прутка, под ним радиатор V6
   '<g id="tHead" style="cursor:pointer"><rect class="hb" x="'+(-28*sc)+'" y="'+(-24*sc)+'" width="'+(56*sc)+'" height="'+(48*sc)+'" rx="3" style="fill:var(--bd);stroke:var(--mut)" stroke-width="1"/>'+
   '<rect class="hb" x="'+(-25*sc)+'" y="'+(-16.5*sc)+'" width="'+(42*sc)+'" height="'+(33*sc)+'" rx="2.5" style="fill:var(--card);stroke:var(--fg)" stroke-width="1"/>'+
   '<line class="hb" x1="'+(-6.3*sc)+'" y1="'+(-16.5*sc)+'" x2="'+(-6.3*sc)+'" y2="'+(16.5*sc)+'" style="stroke:var(--mut)" stroke-width=".8"/>'+   // ребро скоса корпуса
   '<rect class="hb" x="'+(11*sc)+'" y="'+(-10*sc)+'" width="'+(8.3*sc)+'" height="'+(20*sc)+'" rx="1.5" style="fill:var(--bg);stroke:var(--fg)" stroke-width="1"/>'+   // рычаг
   '<rect class="hb" x="'+(19.3*sc)+'" y="'+(-.8*sc)+'" width="'+(8*sc)+'" height="'+(1.6*sc)+'" style="fill:var(--mut)"/>'+
   '<rect class="hb" x="'+(27*sc)+'" y="'+(-4.3*sc)+'" width="'+(6.2*sc)+'" height="'+(8.6*sc)+'" rx="1" style="fill:var(--mut)"/>'+   // винт и гайка
   '<circle class="hb" r="'+(4.6*sc)+'" style="fill:var(--bd);stroke:var(--mut)" stroke-width=".8"/>'+   // фитинг над осью прутка
   '<circle class="hb" r="'+(11.15*sc)+'" fill="none" style="stroke:var(--mut)" stroke-width="1" stroke-dasharray="2 1.6"/>'+
   '<circle id="tNoz" r="3.5" fill="#64748b" style="stroke:var(--fg)"/><rect x="'+(-28*sc-3)+'" y="'+(-24*sc-3)+'" width="'+(56*sc+6)+'" height="'+(48*sc+6)+'" '+HIT+'/><rect id="tRing" x="'+(-28*sc-5)+'" y="'+(-24*sc-5)+'" width="'+(56*sc+10)+'" height="'+(48*sc+10)+'" rx="7" '+RING+'/></g>'+
   '<text x="'+(ox+bw/2)+'" y="'+(oy+bh+ty+17)+'" text-anchor="middle" font-size="6.5" letter-spacing=".08em" style="fill:var(--mut)" opacity=".8">перед</text>'+
   pill('tBedT',rr+12*sc,oy+bh+ty+15,8,'end',ox+bw-rr-12*sc,-ty-15)+   // под рамой справа, в строке с «перед»; выноска к углу стола
   '<g id="tHov" style="display:none;pointer-events:none"><line id="tHovH" x1="'+ox+'" x2="'+(ox+bw)+'" style="stroke:var(--acc)" stroke-width="1.3" stroke-dasharray="4 3"/><line id="tHovV" y1="'+oy+'" y2="'+(oy+bh)+'" style="stroke:var(--acc)" stroke-width="1.3" stroke-dasharray="4 3"/><circle id="tHovC" r="3.5" style="fill:var(--acc)"/><rect id="tHovR" rx="3" height="15" style="fill:var(--card);fill-opacity:.94;stroke:var(--acc)" stroke-width="1"/><text id="tHovT" font-size="10" style="fill:var(--fg);font-weight:700"></text></g>';
  const rx1=ox+22,rx2=ox+bw-22,cx=ox+bw/2,B=FR.beam,hs=sc,ht=HEAD.top*hs;
  $('#svgFront').innerHTML=
   '<defs><pattern id="zThread" width="'+(8*sc)+'" height="'+(2*sc)+'" patternUnits="userSpaceOnUse"><rect width="'+(8*sc)+'" height="'+(2*sc)+'" style="fill:var(--mut)"/>'+
   '<line x1="0" y1="'+(2*sc)+'" x2="'+(8*sc)+'" y2="0" style="stroke:var(--bg)" stroke-width="'+(.7*sc)+'" opacity=".7"/></pattern></defs>'+
   // основание с ножками, винт Z с муфтой и мотором внизу
   '<rect x="'+(rl-8)+'" y="'+FR.bot+'" width="'+(rr-rl+16)+'" height="26" rx="6" style="fill:var(--bd);stroke:var(--mut)" stroke-width="1.5"/><rect x="'+(rl-8)+'" y="'+FR.bot+'" width="'+(rr-rl+16)+'" height="7" rx="3" '+rod+' opacity=".45"/>'+
   '<rect x="'+(rl-2)+'" y="'+(FR.bot+26)+'" width="26" height="6" rx="2" '+rod+'/><rect x="'+(rr-24)+'" y="'+(FR.bot+26)+'" width="26" height="6" rx="2" '+rod+'/>'+
   // верхняя балка рамы (на заднем плане, валы X и голова проходят перед ней)
   '<rect x="'+(rl-5)+'" y="'+(B-5)+'" width="'+(rr-rl+10)+'" height="10" rx="3" '+rod+' opacity=".7"/>'+
   '<rect x="'+(rx1-6*sc)+'" y="'+(B+5)+'" width="'+(12*sc)+'" height="'+(FR.bot-B-5)+'" style="fill:var(--mut)" opacity=".5"/><rect x="'+(rx2-6*sc)+'" y="'+(B+5)+'" width="'+(12*sc)+'" height="'+(FR.bot-B-5)+'" style="fill:var(--mut)" opacity=".5"/>'+
   '<rect x="'+(cx-4*sc)+'" y="'+(B+5)+'" width="'+(8*sc)+'" height="'+(FR.bot-25*sc-B-5)+'" fill="url(#zThread)" opacity=".55"/>'+
   '<rect x="'+(cx-10*sc)+'" y="'+(FR.bot-25*sc)+'" width="'+(20*sc)+'" height="'+(25*sc)+'" rx="'+(2*sc)+'" style="fill:var(--bd);stroke:var(--mut)" stroke-width="1"/>'+
   '<line x1="'+(cx-10*sc)+'" y1="'+(FR.bot-12.5*sc)+'" x2="'+(cx+10*sc)+'" y2="'+(FR.bot-12.5*sc)+'" style="stroke:var(--mut)" stroke-width=".8" opacity=".6"/>'+
   '<rect id="fZone" rx="4" style="fill:var(--acc);fill-opacity:.09;stroke:var(--acc);stroke-opacity:.5;display:none;pointer-events:none" stroke-width="1"/>'+
   '<g id="fBedG" style="cursor:pointer"><rect id="fBed" x="'+ox+'" y="0" width="'+bw+'" height="10" rx="2" fill="#64748b" style="stroke:var(--mut)"/>'+
   '<rect x="'+(rx1-9)+'" y="10" width="18" height="7" rx="2" '+rod+'/><rect x="'+(rx2-9)+'" y="10" width="18" height="7" rx="2" '+rod+'/>'+
   pill('fBedT',(rx1+6*sc+cx-4*sc)/2,19,7)+
   '<g id="fModel" transform="translate('+ox+',0) scale('+sc+','+(-sc)+')" style="display:none;pointer-events:none">'+
    '<path id="fSilDone" style="fill:var(--fg);fill-opacity:.5"/></g>'+
   '<rect x="'+ox+'" y="-4" width="'+bw+'" height="24" '+HIT+'/><rect id="fRingB" x="'+(ox-3)+'" y="-4" width="'+(bw+6)+'" height="24" rx="4" '+RING+'/></g>'+
   // ось X: два вала на уровне балки, каретка с головой перед ними
   '<g id="fGantry"><rect x="'+(rl-5)+'" y="'+(FR.tip+(HEAD.rodY-4)*hs)+'" width="'+(rr-rl+10)+'" height="'+(8*hs)+'" '+rod+'/>'+
   '<g id="fHead" style="cursor:pointer"><g transform="translate(0,'+FR.tip+')">'+
    '<rect x="'+(-27*hs)+'" y="'+(-67.6*hs)+'" width="'+(54*hs)+'" height="'+(18*hs)+'" rx="2" style="fill:var(--bd);stroke:var(--mut)" stroke-width=".8"/>'+
    headSVG('f',hs,false)+'</g>'+
   pill('fNozT',17*hs+3,FR.tip-8*hs,7,'start')+
   '<rect x="'+(HEAD.l*hs-2)+'" y="'+(FR.tip-ht-3)+'" width="'+((HEAD.r-HEAD.l)*hs+4)+'" height="'+(ht+5)+'" '+HIT+'/>'+
   '<rect id="fRingH" x="'+(HEAD.l*hs-4)+'" y="'+(FR.tip-ht-5)+'" width="'+((HEAD.r-HEAD.l)*hs+8)+'" height="'+(ht+9)+'" rx="6" '+RING+'/></g></g>'+
   // боковые стенки рамы — впереди валов X: концы валов уходят в них
   '<g style="pointer-events:none">'+'<rect x="'+(rl-5)+'" y="'+(B-5)+'" width="10" height="'+(FR.bot-B+5)+'" rx="3" style="fill:var(--bd)"/><rect x="'+(rr-5)+'" y="'+(B-5)+'" width="10" height="'+(FR.bot-B+5)+'" rx="3" style="fill:var(--bd)"/>'+'</g>'+
   '<g id="fHov" style="display:none;pointer-events:none"><line id="fHovL" style="stroke:var(--acc)" stroke-width="1.5" stroke-dasharray="4 3"/><circle id="fHovC" r="3.5" style="fill:var(--acc)"/><rect id="fHovR" rx="3" height="15" style="fill:var(--card);fill-opacity:.94;stroke:var(--acc)" stroke-width="1"/><text id="fHovT" font-size="10" style="fill:var(--fg);font-weight:700"></text></g>';
  // карточка экструдера: та же голова крупно, в разрезе; рамка рисунка — точно по габариту головы (−26…34 × −124…+5 мм)
  const ek=EK,ex=26*ek+2,ey=124*ek+2,en=v=>+(v*ek).toFixed(2);
  $('#svgExt').setAttribute('viewBox','0 0 '+(66*ek+4)+' '+(129*ek+4));
  $('#svgExt').innerHTML=
   '<g transform="translate('+ex+','+ey+')">'+headSVG('e',ek,true)+'</g>'+
   '<line id="eFil" x1="'+ex+'" y1="0" x2="'+ex+'" y2="'+(ey+en(-.4))+'" stroke="#22c55e" stroke-width="'+en(1.75)+'" stroke-dasharray="'+en(3)+' '+en(1.2)+'" stroke-dashoffset="0"/>'+
   pill('eNozT',ex+en(6.5),ey+en(1.6),8.5,'start',en(3.5-6.5),en(-3.5-1.6))+
   '<line id="eMelt" x1="'+ex+'" y1="'+(ey+.5)+'" x2="'+ex+'" y2="'+(ey+8)+'" stroke="#f59e0b" stroke-width="3" stroke-linecap="round" style="opacity:0"/>';
  $('#dimTop').textContent='X '+g.bx+' × Y '+g.by+' мм';$('#dimFront').textContent='X '+g.bx+' × Z '+g.bz+' мм';
  document.querySelectorAll('.ring').forEach(fixRing);drawModel();
  zTitles();renderE();syncArm();fitViews();renderPos()}
// ---- плавное движение каретки и стола: на экране едут с реальной скоростью (≈100 мм/с по XY, 25 мм/с по Z), но любой переезд укладывается в ~1.5 с
let ANIM=true,CUR=null,TGTP=null,MOTION=null,posRaf=0,posT=0,HOLD=0,HOMING=0,LASTGOOD=Date.now(),EVER=false,LIM={x:null,y:null,z:null},LIMTRIED=false;
const T0=Date.now();
function renderPos(){if(!TW||!FR||!CUR)return;const g=GEOM,c=CUR,px=TW.ox+c.x*TW.s,py=TW.oy+(g.by-c.y)*TW.s,fx=FR.l+(c.x/g.bx)*FR.bw;
  $('#tHead').setAttribute('transform','translate('+px+','+py+')');$('#tGant').setAttribute('transform','translate(0,'+py+')');
  $('#fHead').setAttribute('transform','translate('+fx+',0)');$('#fGantry').setAttribute('transform','translate(0,'+frontDy(c.z)+')');
  $('#fBedG').setAttribute('transform','translate(0,'+bedYAt(c.z)+')')}
function setPosTarget(x,y,z,o){TGTP={x,y,z};MOTION=o||null;if(!CUR||!ANIM){CUR={x,y,z};renderPos();return}
  if(!posRaf){posT=performance.now();posRaf=requestAnimationFrame(animPos)}}
function animPos(t){const dt=Math.min(.1,(t-posT)/1000);posT=t;const c=CUR,g=TGTP;
  const dx=g.x-c.x,dy=g.y-c.y,dd=Math.hypot(dx,dy);
  const o=MOTION;
  if(dd<.02){c.x=g.x;c.y=g.y}else{const st=Math.min(dd,(o?o.vxy:Math.max(100,dd/1.5))*dt);c.x+=dx*st/dd;c.y+=dy*st/dd}
  const dz=g.z-c.z;if(Math.abs(dz)<.02)c.z=g.z;else c.z+=Math.sign(dz)*Math.min(Math.abs(dz),(o?o.vz:Math.max(25,Math.abs(dz)/1.5))*dt);
  renderPos();
  posRaf=(c.x!==g.x||c.y!==g.y||c.z!==g.z)?requestAnimationFrame(animPos):0}
function setVeil(s,fresh){
  let msg=null,sub='';
  if(!EVER&&Date.now()-T0<5000){msg='Загрузка…'}
  else if(s.bridge===false){msg='Мост не отвечает';sub='Программа MKS Bridge закрыта или не запущена. Запусти её снова — панель подключится сама.'}
  else if(s.online===false||(s.error&&!fresh)){msg='Нет связи с принтером';sub=(s.error?String(s.error).replace(/\.?\s*$/,'. '):'')+'Показано последнее известное положение.'}
  else if(!fresh&&homing()){msg='Принтер едет домой';sub='Во время парковки (G28) прошивка не отвечает на запросы — это нормально. Положение обновится, когда он закончит.'}
  else if(!fresh&&!CUR&&/PRINT|PAUSE/.test(s.state||'')){msg='Ждём координаты от принтера';sub='Пока принтер греется или паркуется, он не сообщает положение. Модель на столе появится сразу, как только он ответит.'}
  else if(!fresh&&Date.now()-LASTGOOD>25000&&!/PRINT|PAUSE/.test(s.state||'')){msg='Принтер долго не отвечает';sub='Возможно, он занят движением. Показано последнее известное положение.'}
  const wait=s.bridge===false||msg==='Загрузка…'||msg==='Принтер едет домой'||msg==='Ждём координаты от принтера';
  document.querySelectorAll('.veil').forEach(v=>{v.hidden=!msg;if(msg){v.children[0].textContent=msg;v.children[1].textContent=sub;v.children[2].style.display=wait?'none':''}})}
// парковку отслеживает мост (s.homing); HOMING закрывает несколько секунд до первого ответа моста после нажатия
function homing(){return !!LASTS.homing||(HOMING&&Date.now()-HOMING<4000)}
async function reconnect(){toast('Переподключаюсь…');const r=await api('/api/reconnect',{});toast(r.error||('Связь есть: '+(r.state||'ok')));tick()}
function feedXY(f){return Math.min(f,LIM.x||f,LIM.y||f)}
function holdPlan(tx,ty,tz,vxy,vz,extra){const c=TGTP||CUR||{x:0,y:0,z:0};
  tx=tx==null?c.x:tx;ty=ty==null?c.y:ty;tz=tz==null?c.z:tz;
  return{tx,ty,tz,vxy,vz,extra,prev:{x:c.x,y:c.y,z:c.z},est:Math.max(Math.hypot(tx-c.x,ty-c.y)/vxy,Math.abs(tz-c.z)/vz)}}
function holdRun(p){HOLD=Date.now()+p.est*1000+(p.extra||1200);setPosTarget(p.tx,p.ty,p.tz,{vxy:p.vxy,vz:p.vz})}
function holdMove(tx,ty,tz,vxy,vz,extra){holdRun(holdPlan(tx,ty,tz,vxy,vz,extra))}
// Каретка начинает ехать на экране не сразу: если принтер тут же откажет («Home first»), она не дёргается и не бежит назад.
async function guardedMove(p,call){let started=false;const go=()=>{if(!started){started=true;holdRun(p)}};const tm=setTimeout(go,350);
  const r=await call();clearTimeout(tm);
  if(r.error){HOLD=0;if(started)setPosTarget(p.prev.x,p.prev.y,p.prev.z);toast(r.error)}else go();return r}
function showLimits(v){if(!v)return;LIM={x:v.x,y:v.y,z:v.z};$('#lims').textContent='Макс. скорости (M203), мм/с: X '+v.x+' · Y '+v.y+' · Z '+v.z+(v.e?' · E '+v.e:'')}
async function loadLimits(){toast('Читаю настройки платы (M503)…');const r=await api('/api/limits?refresh=1');if(r.error){toast(r.error);return}
  const v=r.limits&&r.limits.vmax;if(v){showLimits(v);toast('Скорости считаны')}else toast(r.note||'Прошивка не прислала M203')}
function fitViews(){
  try{const mq=window.matchMedia&&window.matchMedia('(min-width:1450px) and (min-height:640px)'),root=document.documentElement.style,gr=document.querySelector('.grid');
    if(!mq||!mq.matches){root.removeProperty&&root.removeProperty('--vw');if(gr&&gr.style)gr.style.gridTemplateRows='';return}
    if(gr&&gr.style&&FR)gr.style.gridTemplateRows='minmax(0,'+(TW?TW.vbH:232)+'fr) minmax(0,'+FR.vbH+'fr)';
    let w=1e9;[['#cTop',TW?TW.vbH:232],['#cFro',FR?FR.vbH:323]].forEach(([id,vh])=>{const e=document.querySelector(id+' .vwrap');if(!e)return;const r=e.getBoundingClientRect();w=Math.min(w,r.width-148,(r.height-4)*300/vh)});
    if(w>40&&w<1e9)root.setProperty('--vw',Math.floor(w)+'px')}catch(_){}}
window.addEventListener("resize",fitViews);setTimeout(fitViews,400);
function frontDy(z){const g=GEOM;return g.z_bed?0:(FR.floor-FR.tip)-(Math.min(z,g.bz)/g.bz)*FR.T}
function bedYAt(z){const g=GEOM;return g.z_bed?FR.tip+(Math.min(z,g.bz)/g.bz)*FR.T:FR.floor}
function updateTwin(s){
  if(!TW||!FR)return;LASTS=s||{};const g=GEOM,p=s.pos,nt=s.nozzle,bt=s.bed,cl=(v,m)=>Math.max(0,Math.min(m,v));
  const fresh=!!p&&!s.stale&&!s.pos_stale&&s.online!==false;
  if(fresh&&HOMING&&!s.homing&&Date.now()-HOMING>4000){HOMING=0;HOLD=0}   // парковка кончилась: сразу на настоящие координаты
  if(fresh){LASTGOOD=Date.now();EVER=true;if(Date.now()>=HOLD)setPosTarget(cl(p.x,g.bx),cl(p.y,g.by),cl(p.z,g.bz))}
  setVeil(s,fresh);
  const x=TGTP?TGTP.x:0,pp=p||(TGTP?{x:TGTP.x,y:TGTP.y,z:TGTP.z}:null),bc=tcol(bt&&bt[0],110),nc=tcol(nt&&nt[0],260);
  $('#tBed').style.fill=bc;$('#tNoz').style.fill=nc;setPill('#tBedT',bt,bc);
  $('#tCoord').textContent=pp?'X '+pp.x.toFixed(1)+'  Y '+pp.y.toFixed(1):'нет данных';
  const fx=FR.l+(x/g.bx)*FR.bw;
  $('#fBed').style.fill=bc;$('#fBlock').style.fill=nc;$('#fNoz').style.fill=nc;
  // у правого края подпись сопла — слева от блока, чтобы не уйти за раму
  const right=fx>FR.l+FR.bw*.7,ft=$('#fNozT');ft.dataset.a=right?'end':'start';ft.setAttribute('transform','translate('+(right?-(8*FR.s+3):17*FR.s+3)+','+(FR.tip-8*FR.s)+')');
  setPill('#fBedT',bt,bc);setPill('#fNozT',nt,nc);
  $('#fZ').textContent=pp?'Z '+pp.z.toFixed(2)+' мм':'Z —';
  $('#eBlock').style.fill=nc;$('#eNoz').style.fill=nc;setPill('#eNozT',nt,nc);
  $('#eMelt').style.opacity=nt?Math.max(0,Math.min(1,(nt[0]-150)/80)):0;
  const f=s.fan;$('#fanv').textContent=f==null?'—':f+'%';
  const bl=$('#eBlades');bl.style.animationPlayState=f>0?'running':'paused';bl.style.animationDuration=(f>0?(1.6-1.45*f/100):1)+'s';
  if(f!=null&&document.activeElement!==$('#fanr')){$('#fanr').value=f;fanLabel()}
  if(p&&p.e!=null)setE(p.e);updateModel(s)}
// ---- модель на столе во время печати: контур текущего слоя сверху, силуэт с напечатанной частью спереди
let OL={id:null,sum:null,layer:0,cur:'',fill:'',det:'',ghost:'',show:false};
const polyD=ps=>ps.map(p=>'M'+p[0]+' '+p[1]+'L'+p.slice(2).join(' ')).join('');
// заливка слоя: только замкнутые контуры (разомкнутые дали бы ложные перепонки); дыры — по правилу evenodd
const fillD=ps=>ps.filter(p=>p.length>=8&&Math.abs(p[0]-p[p.length-2])+Math.abs(p[1]-p[p.length-1])<=8).map(p=>polyD([p])+'Z').join('');
// силуэт спереди: каждый слой — полоска шириной с модель по X на своей высоте. Полосками, а не одним контуром:
// при последовательной печати объектов высота сбрасывается в ноль, и следующий объект растёт заново
function silD(upto){const s=OL.sum,d=[];
  for(let i=0;i<Math.min(upto,s.ext.length);i++){const e=s.ext[i],z1=s.zs[i];if(!e||!e.length||z1==null)continue;
    const p=s.zs[i-1],z0=p!=null&&p<z1&&z1-p<2?p:Math.max(0,z1-.2);
    (typeof e[0]==='number'?[e]:e).forEach(([a,b])=>d.push('M'+a/10+' '+z0+'H'+b/10+'V'+z1+'H'+a/10+'Z'))}
  return d.join('')}
function drawModel(){const on=OL.show&&!!OL.sum,t=$('#tModel'),f=$('#fModel');if(!t||!f)return;t.style.display=f.style.display=on?'':'none';if(!on)return;
  $('#tGhost').setAttribute('d',OL.ghost);$('#tLay').setAttribute('d',OL.cur);$('#tLayF').setAttribute('d',OL.fill);$('#tLayD').setAttribute('d',OL.det);$('#tLayH').setAttribute('d',OL.cur);
  $('#fSilDone').setAttribute('d',silD(OL.layer))}   // спереди только напечатанное: выше сопла модели ещё нет, там каретка
function updateModel(s){const pr=/PRINT|PAUSE/.test(s.state||'');$('#svgTop').classList.toggle('printing',pr);$('#svgFront').classList.toggle('printing',pr);
  // позицию E во время печати Marlin не сообщает (слайсер подаёт пруток относительно): крутим шестерни, пока идёт печать
  const ex=$('#svgExt'),run=/PRINTING/.test(s.state||'');if(ex.classList.contains('printing')!==run){ex.classList.toggle('printing',run);renderE()}
  OL.show=pr&&!!s.outline;
  if(OL.show&&OL.id!==s.outline){const id=s.outline;OL={id,sum:null,layer:0,cur:'',fill:'',det:'',ghost:'',show:true};
    api('/api/outline?file='+encodeURIComponent(id)).then(r=>{if(OL.id!==id||r.error)return;OL.sum=r;OL.ghost=polyD(r.ghost);drawModel()})}
  const L=s.layer||(OL.sum&&OL.sum.layers===1?1:0);   // файл без меток слоёв — один слой
  if(OL.show&&OL.sum&&L&&L!==OL.layer){OL.layer=L;const id=OL.id;
    api('/api/outline?file='+encodeURIComponent(id)+'&layer='+L).then(r=>{if(OL.id!==id||r.error)return;OL.cur=polyD(r.polys);OL.det=r.lines?polyD(r.lines):'';OL.fill=OL.det?'':fillD(r.polys);drawModel()})}
  drawModel()}
function setE(v){if(tgtE==null||Math.abs(v-visE)>60)visE=v;tgtE=v;if(!eRaf){lastT=performance.now();eRaf=requestAnimationFrame(animE)}}
function animE(t){const dt=Math.min(.1,(t-lastT)/1000);lastT=t;const d=tgtE-visE;visE+=Math.sign(d)*Math.min(Math.abs(d),25*dt);renderE();
  eRaf=Math.abs(tgtE-visE)>.005?requestAnimationFrame(animE):0}
function renderE(){if($('#svgExt').classList.contains('printing')){$('#eE').textContent='E по G-code';return}
  const a=visE*HOB_DEG,rot=(id,d)=>{const e=$(id);if(e)e.setAttribute('transform','rotate('+d.toFixed(2)+')')};
  rot('#eBig',a);rot('#eHob',a);rot('#eIdl',-a);rot('#ePin',-a*GEAR_K);
  $('#eFil').setAttribute('stroke-dashoffset',(-visE*EK).toFixed(2));$('#eE').textContent='E '+visE.toFixed(1)+' мм'}
// ---- управление кликом: клик по головке или столу включает режим, следующий клик по месту назначения запускает движение
function syncArm(){if(!TW)return;$('#tRing').style.display=ARM==='xy'?'':'none';$('#fRingH').style.display=ARM==='x'?'':'none';$('#fRingB').style.display=ARM==='z'?'':'none';
  $('#svgTop').style.cursor=ARM==='xy'?'crosshair':'';$('#svgFront').style.cursor=ARM==='x'||ARM==='z'?'crosshair':'';$('#tHov').style.display='none';$('#fHov').style.display='none';
  $('#tZone').style.display=ARM==='xy'?'':'none';
  const fz=frontZone(),fe=$('#fZone');fe.style.display=fz?'':'none';
  if(fz){['x','y','width','height'].forEach((k,i)=>fe.setAttribute(k,fz[i]));}}
// где можно кликнуть на виде спереди: по X — полоса хода головки, по Z — ход стола (или сопла, если по Z ездит головка)
function frontZone(){if(ARM==='x'){const h=HEAD.top*FR.s,y=(CUR&&!GEOM.z_bed?frontDy(CUR.z):0)+FR.tip-h-3;return[FR.l-SNAP,y,FR.bw+2*SNAP,h+5]}
  if(ARM==='z')return[FR.l-6,(GEOM.z_bed?FR.tip:FR.floor-FR.T)-SNAP,FR.bw+12,FR.T+2*SNAP];return null}
// пунктир обводок: длина узора 5+3 должна укладываться в периметр целое число раз, иначе в углу, где узор начинается, — обрубок
function fixRing(e){const w=+e.getAttribute('width'),h=+e.getAttribute('height'),r=+e.getAttribute('rx')||0,P=2*(w+h)-(8-2*Math.PI)*r;
  e.setAttribute('pathLength',Math.max(8,Math.round(P/8)*8))}
function setArm(k){if(/PRINT|PAUSE/.test(LASTS.state||'')){toast('Во время печати двигать оси нельзя');return}
  ARM=ARM===k?null:k;syncArm();if(ARM)toast({xy:'Теперь нажми на стол на виде сверху: головка поедет туда',x:'Теперь нажми на нужное место по горизонтали: головка поедет по X',z:'Теперь нажми на нужную высоту: стол поедет по Z'}[ARM]+'. Esc — отмена.')}
function svgQ(svg,e){const pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;return pt.matrixTransform(svg.getScreenCTM().inverse())}
// цель клика: значение зажимается в рабочую зону, а прицел (q) «прилипает» к краю — видно, куда реально поедет
function topTarget(e){const q=svgQ($('#svgTop'),e),g=GEOM,cl=(v,m)=>Math.max(0,Math.min(m,v)),
  ok=q.x>=TW.ox-SNAP&&q.x<=TW.ox+TW.bw+SNAP&&q.y>=TW.oy-SNAP&&q.y<=TW.oy+TW.bh+SNAP,
  x=cl((q.x-TW.ox)/TW.s,g.bx),y=cl(g.by-(q.y-TW.oy)/TW.s,g.by);
  return{x,y,ok,q:{x:TW.ox+x*TW.s,y:TW.oy+(g.by-y)*TW.s}}}
function frontTarget(e){const q=svgQ($('#svgFront'),e),g=GEOM;
  const zn=frontZone(),ok=!!zn&&q.x>=zn[0]&&q.x<=zn[0]+zn[2]&&q.y>=zn[1]&&q.y<=zn[1]+zn[3];   // кликать можно ровно там, где подсвечено
  if(ARM==='x'){const val=Math.max(0,Math.min(g.bx,(q.x-FR.l)/FR.bw*g.bx));return{ok,val,q:{x:FR.l+val/g.bx*FR.bw,y:q.y}}}
  const k=g.z_bed?(q.y-FR.tip)/FR.T:(FR.floor-q.y)/FR.T,val=Math.max(MINZ,Math.min(g.bz,k*g.bz)),kv=val/g.bz*FR.T;
  return{ok,val,q:{x:q.x,y:g.z_bed?FR.tip+kv:FR.floor-kv}}}
async function goTo(p){ARM=null;syncArm();log('> переезд '+Object.keys(p).map(k=>k.toUpperCase()+p[k].toFixed(1)).join(' '));
  const plan=holdPlan(p.x,p.y,p.z,feedXY(50),Math.min(10,LIM.z||10));
  const r=await guardedMove(plan,()=>api('/api/goto',p));if(r.error)log(r.error+(r.reply?' · '+r.reply.split('\n')[0]:''),'#ef5350');else log(r.reply||'(отправлено)');tick()}
function hovLabel(rid,tid,txt,x,y,minx,maxx,miny,maxy){const t=$(tid),r=$(rid);t.textContent=txt;let w=0;try{w=t.getBBox().width}catch(_){}if(!w)w=txt.length*6;w+=10;const h=15;
  let lx=x+10;if(lx+w>maxx)lx=x-10-w;if(lx<minx)lx=minx;let ly=y-10-h;if(ly<miny)ly=y+10;if(ly+h>maxy)ly=maxy-h;
  r.setAttribute('x',lx);r.setAttribute('y',ly);r.setAttribute('width',w);t.setAttribute('x',lx+5);t.setAttribute('y',ly+11)}
function inEl(t,id){return !!(t&&t.closest&&t.closest(id))}
(function(){const T=$('#svgTop'),F=$('#svgFront'),down=e=>{e.preventDefault();DOWN={x:e.clientX,y:e.clientY,t:e.target}},
  moved=e=>!DOWN||Math.hypot(e.clientX-DOWN.x,e.clientY-DOWN.y)>8;
  T.addEventListener('pointerdown',down);F.addEventListener('pointerdown',down);
  T.addEventListener('pointerup',e=>{const bad=moved(e),t=DOWN&&DOWN.t;DOWN=null;if(bad||!TW)return;
    if(ARM!=='xy'){if(inEl(t,'#tHead'))setArm('xy');else if(ARM){ARM=null;syncArm()}return}
    const r=topTarget(e);if(!r.ok){ARM=null;syncArm();return}goTo({x:r.x,y:r.y})});
  F.addEventListener('pointerup',e=>{const bad=moved(e),t=DOWN&&DOWN.t;DOWN=null;if(bad||!TW)return;
    if(ARM!=='x'&&ARM!=='z'){if(inEl(t,'#fHead'))setArm('x');else if(inEl(t,'#fBedG'))setArm('z');else if(ARM){ARM=null;syncArm()}return}
    const r=frontTarget(e);if(!r.ok){ARM=null;syncArm();return}goTo(ARM==='x'?{x:r.val}:{z:r.val})});
  T.addEventListener('pointermove',e=>{if(ARM!=='xy'||!TW)return;const r=topTarget(e),hv=$('#tHov');T.style.cursor=r.ok?'crosshair':'not-allowed';if(!r.ok){hv.style.display='none';return}
    hv.style.display='';const H=$('#tHovH'),V=$('#tHovV'),C=$('#tHovC');H.setAttribute('y1',r.q.y);H.setAttribute('y2',r.q.y);V.setAttribute('x1',r.q.x);V.setAttribute('x2',r.q.x);C.setAttribute('cx',r.q.x);C.setAttribute('cy',r.q.y);
    hovLabel('#tHovR','#tHovT','X '+r.x.toFixed(0)+'   Y '+r.y.toFixed(0),r.q.x,r.q.y,2,298,2,TW.vbH-2)});
  F.addEventListener('pointermove',e=>{if((ARM!=='x'&&ARM!=='z')||!TW)return;const r=frontTarget(e),hv=$('#fHov');F.style.cursor=r.ok?'crosshair':'not-allowed';if(!r.ok){hv.style.display='none';return}
    const L=$('#fHovL'),C=$('#fHovC');hv.style.display='';C.setAttribute('cx',r.q.x);C.setAttribute('cy',r.q.y);
    if(ARM==='x'){const zn=frontZone();L.setAttribute('x1',r.q.x);L.setAttribute('x2',r.q.x);L.setAttribute('y1',zn[1]);L.setAttribute('y2',zn[1]+zn[3]);hovLabel('#fHovR','#fHovT','X '+r.val.toFixed(0),r.q.x,r.q.y,2,298,2,FR.vbH-2)}
    else{L.setAttribute('x1',FR.l);L.setAttribute('x2',FR.l+FR.bw);L.setAttribute('y1',r.q.y);L.setAttribute('y2',r.q.y);hovLabel('#fHovR','#fHovT','Z '+r.val.toFixed(1)+(r.val<=MINZ?' · мин':''),r.q.x,r.q.y,2,298,2,FR.vbH-2)}});
  T.addEventListener('pointerleave',()=>{if(TW)$('#tHov').style.display='none'});F.addEventListener('pointerleave',()=>{if(TW)$('#fHov').style.display='none'});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&ARM){ARM=null;syncArm()}});
  document.addEventListener('pointerdown',e=>{if(ARM&&!inEl(e.target,'#svgTop')&&!inEl(e.target,'#svgFront')){ARM=null;syncArm()}},true)})();
// поле очищается, когда принтер принял команду: новая цель видна в плитке («/ 200°»)
async function setT(code,id){const el=$('#'+id),v=el.value;if(v==='')return toast('Введи температуру');if(await cmd(code+' S'+(+v))){el.value='';el.blur()}}
const printing=()=>/PRINT|PAUSE/.test(LASTS.state||'');
const SPOIL=['Выключить нагрев во время печати?','Пластик перестанет плавиться, печать будет испорчена.','Выключить',1];
async function heatOff(code){if(printing()&&!await ask(...SPOIL))return;cmd(code+' S0')}
async function coolAll(){if(printing()&&!await ask(...SPOIL))return;
  tab('log');log('> остудить всё: M104 S0, M140 S0');
  for(const c of['M104 S0','M140 S0']){const r=await api('/api/console',{cmd:c});if(r.error){log(r.error,'#ef5350');toast(r.error);return}}
  log('нагрев сопла и стола выключен');toast('Нагрев выключен');tick()}
// ---- настройки
function openSet(){$('#ov').classList.add('on');loadSettings()}function closeSet(){$('#ov').classList.remove('on')}
async function openLogDir(){const r=await api('/api/logdir',{});if(r.error)toast(r.error)}
async function loadSettings(){
  const s=await api('/api/settings');if(s.error){toast(s.error);return}
  $('#sip').value=s.printer_ip||'';markPrev(s.preview||'off');$('#sow').checked=!!s.overwrite;$('#sbg').checked=!!s.background_poll;$('#smax').value=s.max_name;
  $('#srem').checked=!!s.remote;$('#sauto').checked=!!s.autostart;$('#autorow').style.display=s.can_autostart?'':'none';$('#ver').textContent='v'+s.version;$('#logdir').hidden=!(s.local&&s.can_autostart);
  if(s.geom){GEOM=s.geom;$('#gbx').value=GEOM.bx;$('#gby').value=GEOM.by;$('#gbz').value=GEOM.bz;$('#gzb').checked=!!GEOM.z_bed;buildTwin();updateTwin(LASTS)}
  showLimits(s.limits&&s.limits.vmax);
  const rm=$('#remote');rm.hidden=!s.remote;rm.textContent='';
  if(s.remote){const el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e};
    const a=el('div',null,'Открой на телефоне (он должен быть в той же Wi-Fi-сети): ');s.urls.forEach((u,i)=>{if(i)a.append(' или ');a.append(el('code',null,u))});rm.append(a);
    if(s.pin){const p=el('div','row');p.style.margin='0';p.append(el('span','mut','PIN'),el('span','pin',s.pin),el('span','sp'));
      const b=el('button','sm','Новый PIN');b.onclick=newPin;p.append(b);rm.append(p)}}}
async function newPin(){if(!await ask('Сменить PIN?','Телефоны, на которых уже входили, попросят ввести новый.','Сменить'))return;
  const r=await api('/api/settings',{new_pin:true});toast(r.error||'PIN изменён');loadSettings()}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('#ov').classList.contains('on')&&!$('#dlg').classList.contains('on'))closeSet()});
async function saveIp(){const r=await api('/api/settings',{printer_ip:$('#sip').value});toast(r.error||'Сохранено');tick();loadSettings()}
// картинка для стокового экрана MKS: off / ghost (50 и 200 px) / mks (100 и 200 px)
function markPrev(p){document.querySelectorAll('#sprev button').forEach(b=>b.classList.toggle('on',b.dataset.p===p))}
function setPrev(b){markPrev(b.dataset.p);saveOpt()}
async function saveOpt(){const r=await api('/api/settings',{preview:($('#sprev .on')||{dataset:{p:'off'}}).dataset.p,overwrite:$('#sow').checked,background_poll:$('#sbg').checked,max_name:+$('#smax').value,bed_x:+$('#gbx').value,bed_y:+$('#gby').value,bed_z:+$('#gbz').value,z_bed:$('#gzb').checked});toast(r.error||'Сохранено');loadSettings()}
async function saveRemote(){const r=await api('/api/settings',{remote:$('#srem').checked});toast(r.error||'Сохранено');loadSettings()}
async function saveAuto(){const r=await api('/api/settings',{autostart:$('#sauto').checked});toast(r.error||'Сохранено')}
async function findPrinter(){toast('Ищу принтер в сети…');const r=await api('/api/discover',{});
  if(r.error){toast('Ошибка поиска: '+r.error);return}const sc=(r.scanned||[]).map(b=>b+'.1–254').join(', ');
  if(!r.found||!r.found.length){toast('Принтер не найден. Проверено: '+(sc||'ничего')+'. Включён ли принтер и в той же ли он сети? Можно ввести IP вручную.');return}
  let ip=r.found[0];if(r.found.length>1){ip=await ask('Найдено несколько принтеров','Выбери, к какому подключаться:',null,0,r.found);if(!ip)return}
  $('#sip').value=ip;saveIp()}
renderRef();buildTwin();applyTheme();loadSettings();tick();setInterval(tick,3000);setTimeout(loadFiles,1500);
