import {request,events} from './api.js';
import {escapeHtml as esc,freshness,delayLabel} from './model.js';
import {TransitMap} from './map.js';
const $=id=>document.getElementById(id);
const state={token:'',run:'live',vehicles:[],predictions:[],issues:[],selected:null,filter:'all',query:'',snapshot:null,detail:null,connected:false};
let session=null,stream=null,retry=null,poll=null,refreshTimer=null,pendingSignal=null,pendingRefresh=null,detailVersion=0;
const formatTime=t=>t?new Date(t).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
const number=v=>Number.isFinite(v)?v.toLocaleString('ru-RU',{maximumFractionDigits:1}):'—';
const map=new TransitMap($('map'),select);
function status(text,online=false){state.connected=online;$('connection').textContent=text;$('connection').className=`status ${online?'online':'offline'}`;}
function notice(text){$('notice').textContent=text;$('notice').hidden=!text;}
function empty(text,cols=1){return `<tr><td colspan="${cols}" class="empty">${esc(text)}</td></tr>`;}
function filtered(){return state.vehicles.filter(v=>v.tr_id.toLowerCase().includes(state.query.toLowerCase())&&(state.filter==='all'||freshness(v)===(state.filter==='fresh')));}
function render(){
  const snap=state.snapshot,visible=filtered();
  $('total').textContent=snap?number(snap.counts.vehicles):'—';
  $('scope').textContent=snap?`В выборке ${state.vehicles.length} из ${snap.counts.vehicles} ТС`:'Снимок бэкенда';
  $('fresh').textContent=snap?number(state.vehicles.filter(v=>freshness(v)).length):'—';
  const latest=new Map();for(const p of state.predictions)if(!latest.has(p.tr_id))latest.set(p.tr_id,p);
  $('forecast').textContent=snap?number([...latest.values()].filter(p=>p.status==='ok'&&Number.isFinite(p.delay_s)).length):'—';
  $('issues-count').textContent=snap?number(state.issues.length):'—';
  $('list-count').textContent=visible.length;
  $('list-scope').textContent=`Показано ${visible.length} из ${state.vehicles.length} в выборке · максимум 200`;
  $('vehicles').innerHTML=visible.length?visible.map(v=>`<button class="vehicle ${v.tr_id===state.selected?'selected':''}" data-id="${esc(v.tr_id)}"><div class="vehicle-top"><span class="bus-icon">▣</span><strong>${esc(v.tr_id)}</strong><span class="vehicle-arrow">↗</span></div><div class="vehicle-meta"><span>Устройство ${esc(v.unit_id)}</span><b>${number(v.speed_kmh)} <small>км/ч</small></b></div><div class="vehicle-bottom"><span class="badge ${freshness(v)?'good':'muted'}">${freshness(v)?'Свежие данные':'Устаревшие данные'}</span>${v.location_valid?'':'<span class="gps-error">GPS !</span>'}</div></button>`).join(''):`<div class="empty large">${state.query||state.filter!=='all'?'Ничего не найдено':'Нет транспорта'}<span>${state.query?'Измените запрос поиска':'В выбранном потоке пока нет данных'}</span></div>`;
  $('vehicles').querySelectorAll('[data-id]').forEach(el=>el.onclick=()=>select(el.dataset.id));
  map.update(visible,state.selected);
  $('snapshot-time').textContent=snap?`Снимок ${formatTime(snap.server_time)}`:'Нет снимка';
  $('model-state').textContent=snap?.ml==='disabled'?'ML-модель не подключена · прогноз может быть недоступен':snap?'ML настроена · статус каждого результата указан отдельно':'Состояние модели неизвестно';
  $('predictions').innerHTML=state.predictions.length?state.predictions.map(p=>`<tr><td class="mono">${formatTime(p.as_of)}</td><td>${esc(p.tr_id)}</td><td><span class="badge ${p.status==='ok'?'blue':'muted'}">${delayLabel(p)}</span></td><td>${esc(p.error_code||p.status)}</td><td>${esc(p.target_stop_id||'—')}<small>${formatTime(p.target_time_begin)}</small></td><td>${esc(p.model_version||'—')}</td></tr>`).join(''):empty('В этом потоке ещё нет результатов прогнозирования',6);
  $('issues').innerHTML=state.issues.length?state.issues.map(i=>`<tr><td class="mono">${formatTime(i.created_at)}</td><td>${esc(i.unit_id||'—')}</td><td><span class="badge warning">${esc(i.reason||'Проблема данных')}</span></td><td>${esc(i.detail||'—')}</td></tr>`).join(''):empty('Проблем данных не зарегистрировано',4);
  renderDetails();
}
function renderDetails(){
  const v=state.detail?.vehicle||state.vehicles.find(v=>v.tr_id===state.selected);
  if(!v){$('details').innerHTML='<div class="empty large">Выберите транспорт<span>Нажмите на строку в списке<br>или маркер на карте</span></div>';return;}
  const p=state.detail?.predictions?.[0];
  $('details').innerHTML=`<div class="detail-hero"><div class="detail-bus">▣</div><span class="eyebrow">ТРАНСПОРТНОЕ СРЕДСТВО</span><h3>${esc(v.tr_id)}</h3><span class="badge ${freshness(v)?'good':'warning'}">${freshness(v)?'Свежая телеметрия':'Данные устарели'}</span></div><div class="speed"><span>Скорость</span><strong>${number(v.speed_kmh)}<small> км/ч</small></strong></div><dl><dt>Устройство</dt><dd>${esc(v.unit_id)}</dd><dt>Последний пакет</dt><dd>${formatTime(v.event_time)}</dd><dt>Позиция получена</dt><dd>${formatTime(v.position_time)}</dd><dt>GPS последнего пакета</dt><dd class="${v.location_valid?'text-good':'text-warning'}">${v.location_valid?'Валиден':'Невалиден'}</dd><dt>Координаты</dt><dd class="mono">${Number.isFinite(v.lat)?v.lat.toFixed(5):'—'}<br>${Number.isFinite(v.lon)?v.lon.toFixed(5):'—'}</dd></dl><button id="focus-vehicle" class="button full">⌖ Показать на карте</button><div class="prediction-card"><span class="eyebrow">ПРОГНОЗ ЗАДЕРЖКИ</span><strong>${state.detail?delayLabel(p):'Загрузка…'}</strong><p>${esc(state.detail?(p?.error_code|| (p?`Расчёт: ${formatTime(p.as_of)}`:'Прогноз для этого ТС ещё не получен')):'Получаем карточку транспорта')}</p></div><div class="detail-note">${v.location_valid?'Показана последняя пригодная позиция.':'GPS последнего пакета невалиден. На карте сохранена предыдущая пригодная позиция.'}</div><div class="detail-note">Флаги качества: ${esc((v.quality_flags||[]).join(', ')||'не отмечены')}</div>`;
  $('focus-vehicle').onclick=()=>map.focus(v);
}
async function select(id){state.selected=id;state.detail=null;render();await loadDetail();}
async function loadDetail(){
  if(!state.selected||!session)return;
  const id=state.selected,version=++detailVersion,signal=session.signal;
  try{const detail=await request(`vehicles/${encodeURIComponent(id)}?run_id=${encodeURIComponent(state.run)}`,state.token,signal);if(version===detailVersion&&!signal.aborted&&id===state.selected){state.detail=detail;renderDetails();}}
  catch(e){if(!signal.aborted){if(e.status===401){logout('Сессия истекла. Введите токен снова.');return;}notice(`Карточка недоступна: ${e.message}`);}}
}
function refresh(){
  if(!session)return Promise.resolve();
  const signal=session.signal;
  if(pendingSignal===signal&&pendingRefresh)return pendingRefresh;
  pendingSignal=signal;pendingRefresh=refreshSnapshot(signal);
  return pendingRefresh;
}
async function refreshSnapshot(signal){
  $('refresh').disabled=true;
  try{
    const q=`run_id=${encodeURIComponent(state.run)}&limit=200`;
    const [snapshot,predictions,issues]=await Promise.all([request(`dashboard?${q}`,state.token,signal),request(`predictions?${q}`,state.token,signal),request(`data-issues?${q}`,state.token,signal)]);
    if(signal.aborted)return;
    Object.assign(state,{snapshot,vehicles:snapshot.vehicles,predictions:predictions.items,issues:issues.items});
    if(state.selected&&!state.vehicles.some(v=>v.tr_id===state.selected)){state.selected=null;state.detail=null;}
    render();notice('');if(!state.connected)status('Данные обновлены',true);await loadDetail();
    return snapshot;
  }catch(e){if(!signal.aborted){if(e.status===401){logout('Нет доступа. Проверьте токен.');}else{status('Нет связи');notice('Не удалось обновить данные. Показан последний полученный снимок. Повтор через 15 секунд.');}throw e;}}
  finally{if(pendingSignal===signal){pendingSignal=null;pendingRefresh=null;$('refresh').disabled=false;}}
}
function queueRefresh(){if(!refreshTimer)refreshTimer=setTimeout(()=>{refreshTimer=null;refresh().catch(()=>{});},350);}
function connectStream(cursor){
  if(!session)return;stream?.abort();stream=new AbortController();const signal=stream.signal;
  let last=cursor;
  events(state.run,cursor,state.token,signal,event=>{
    if(event.type==='resync_required'){signal.aborted||reconnect();return;}
    if(event.id)last=Number(event.id);queueRefresh();
  },()=>status('Поток подключён',true)).catch(e=>{
    if(signal.aborted||!session)return;
    if(e.status===401){logout('Нет доступа к потоку. Введите токен снова.');return;}
    status('Переподключение…');notice('Поток событий прерван. Данные обновляются опросом каждые 15 секунд.');
    retry=setTimeout(()=>{if(session)connectStream(last);},2500);
  });
}
async function reconnect(){
  const current=session;if(!current)return;
  clearTimeout(retry);stream?.abort();
  try{const snap=await refresh();if(snap&&session===current)connectStream(snap.event_cursor);}
  catch{if(session===current)retry=setTimeout(reconnect,2500);}
}
function stop(){session?.abort();stream?.abort();session=null;stream=null;pendingSignal=null;pendingRefresh=null;clearTimeout(retry);clearTimeout(refreshTimer);clearInterval(poll);refreshTimer=null;detailVersion++;}
async function start(){
  stop();session=new AbortController();status('Подключение…');
  const snapshot=await refresh();if(snapshot){connectStream(snapshot.event_cursor);poll=setInterval(()=>refresh().catch(()=>{}),15000);}
}
function logout(message=''){
  stop();Object.assign(state,{token:'',vehicles:[],predictions:[],issues:[],snapshot:null,selected:null,detail:null});render();notice('');status('Ожидание входа');$('token').value='';$('login-error').textContent=message;if(!$('login').open)$('login').showModal();
}
$('login').addEventListener('cancel',e=>e.preventDefault());
$('login-form').onsubmit=async e=>{e.preventDefault();state.token=$('token').value.trim();$('connect').disabled=true;$('login-error').textContent='';try{await start();if(state.snapshot){$('token').value='';$('login').close();}}catch(e){$('login-error').textContent=e.status===401?'Неверный токен доступа':'Бэкенд недоступен. Проверьте запуск сервера.';stop();}finally{$('connect').disabled=false;}};
$('run-form').onsubmit=async e=>{e.preventDefault();const run=$('run').value.trim();if(!run||!state.token)return;state.run=run;state.selected=null;state.detail=null;state.vehicles=[];state.predictions=[];state.issues=[];state.snapshot=null;render();try{await start();}catch{poll=setInterval(()=>reconnect(),15000);}};
$('logout').onclick=()=>logout();$('refresh').onclick=()=>refresh().catch(()=>{});
$('search').oninput=e=>{state.query=e.target.value;render();};
document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{state.filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button));render();});
document.querySelectorAll('[data-page]').forEach(button=>button.onclick=()=>{for(const page of ['monitor','predictions','issues'])$(`${page}-page`).hidden=page!==button.dataset.page;document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===button.dataset.page));map.render();});
$('zoom-in').onclick=()=>map.step(1);$('zoom-out').onclick=()=>map.step(-1);$('map-reset').onclick=()=>map.reset();
$('export').onclick=()=>{
  const cell=x=>'"'+String(x??'').replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';
  const rows=[['ТС','Время расчёта','Задержка (с)','Статус','Причина','Модель'],...state.predictions.map(p=>[p.tr_id,p.as_of,p.delay_s,p.status,p.error_code,p.model_version])];
  const url=URL.createObjectURL(new Blob(['\uFEFF'+rows.map(r=>r.map(cell).join(';')).join('\r\n')],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='transport-predictions.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
window.addEventListener('beforeunload',stop);logout();
