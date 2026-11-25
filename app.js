'use strict';
const $=s=>document.querySelector(s);
const fmt=v=>v==null?'—':Number(v).toLocaleString('ru-RU',{maximumFractionDigits:4});
const reasonNames={NO_ELIGIBLE_ROUTE:'Проверенные DE/US-маршруты запрещают открытие позиций',ACCESS_CHECK_STALE:'Проверка доступа требует обновления',TRADING_ACCOUNT_NOT_CONNECTED:'Торговый аккаунт не подключён'};
let snapshot=null,selection='',context=null,busy=false;
function text(tag,value,cls){const e=document.createElement(tag);e.textContent=value??'—';if(cls)e.className=cls;return e;}
async function api(path,options={}){
 const r=await fetch('/polymarket/api/'+path,{credentials:'same-origin',...options,headers:{'content-type':'application/json',...(options.headers||{})}});
 const j=await r.json();if(!r.ok)throw Error(j.error||(j.reasons||[]).map(x=>reasonNames[x]||x).join('; ')||'Ошибка '+r.status);return j;
}
function view(which){
 $('#stats').hidden=which!=='stats';$('#orders').hidden=which!=='orders';
 $('#show-stats').setAttribute('aria-pressed',which==='stats');$('#show-orders').setAttribute('aria-pressed',which==='orders');
}
function chosen(){const [id,name]=selection.split('|');const p=snapshot?.papers.find(x=>x.id===id);return {p,a:p?.accounts.find(x=>x.name===name)};}
function renderBank(){
 const {p,a}=chosen();if(!p||!a){$('#metrics').replaceChildren();$('#paper-health').textContent='Данные банка недоступны.';$('#fills').replaceChildren();return;}
 const metrics=[['Чистый итог закрытых, $',fmt(a.net_settled)],['Средства по учёту, $',fmt(a.cost_equity??a.cash)],['Закрытые сделки',fmt(a.settled)],['Прибыльные сделки',fmt(a.wins)]];
 $('#metrics').replaceChildren(...metrics.map(([label,value])=>{const dl=text('dl','','metric');dl.append(text('dt',label),text('dd',value));return dl;}));
 const when=p.at?new Date(p.at*1000).toLocaleString('ru-RU'):'неизвестно';
 $('#paper-health').textContent=(p.phase==='FAILED'?'Остановлен с ошибкой. ':p.phase==='RUNNING'?'Процесс сообщает RUNNING. ':'Состояние: '+p.phase+'. ')+(p.stale?'Данные устарели. ':'')+(p.read_error?'Ошибка чтения: '+p.read_error+'. ':'')+'Срез: '+when+(p.last_error?' Последняя записанная ошибка: '+p.last_error:'')+(p.source_active_error?' Активная ошибка источника: '+p.source_active_error:'');
 const rows=p.activity.filter(x=>x.account===a.name);
 $('#fills').replaceChildren(...(rows.length?rows.map(x=>{const tr=document.createElement('tr');for(const v of [x.slug||x.condition,x.account,fmt(x.shares),fmt(x.cost),fmt(x.fee),fmt(x.payout),'Paper: наблюдение стакана'])tr.append(text('td',v));return tr;}):[emptyRow(7,'Операций для этого банка в последних 60 записях нет.')]));
}
function emptyRow(cols,message){const tr=document.createElement('tr'),td=text('td',message);td.colSpan=cols;tr.append(td);return tr;}
function renderOrders(){
 const stateNames={DRAFT:'Черновик — не отправлен',CANCELLED_LOCAL:'Черновик отменён',UNKNOWN:'Исход неизвестен — нужна сверка',LIVE:'Стоит на бирже',PARTIAL:'Частично исполнена',FILLED:'Исполнена',CANCEL_PENDING:'Отмена не подтверждена',CANCELLED:'Отменена',REJECTED:'Отклонена',SUBMITTING:'Отправляется'};
 $('#order-rows').replaceChildren(...(snapshot.orders.length?snapshot.orders.map(x=>{
  const p=JSON.parse(x.payload),tr=document.createElement('tr');
  for(const v of [new Date(x.created*1000).toLocaleString('ru-RU'),p.side,fmt(p.price),fmt(p.size),stateNames[x.state]||x.state,fmt(x.filled)])tr.append(text('td',v));
  const td=document.createElement('td');
  if(x.state==='DRAFT'){const b=text('button','Отменить черновик');b.type='button';b.onclick=async()=>{b.disabled=true;try{await api('cancel',{method:'POST',body:JSON.stringify({id:x.id})});await refresh();}catch(e){$('#form-result').textContent=e.message;b.disabled=false;}};td.append(b);}else td.textContent='—';tr.append(td);return tr;
 }):[emptyRow(7,'Заявок из этого модуля ещё нет.')]));
 $('#access').textContent='Реальная отправка недоступна: '+snapshot.live.reasons.map(x=>reasonNames[x]||x).join('; ')+'.';
}
async function refresh(){
 if(busy)return;busy=true;$('#refresh').disabled=true;
 try{
  snapshot=await api('snapshot');$('#load-error').hidden=true;
  const choices=snapshot.papers.flatMap(p=>p.accounts.map(a=>({value:p.id+'|'+a.name,label:p.title+' · '+a.name+(p.phase==='FAILED'?' · остановлен':'')})));
  if(!choices.some(x=>x.value===selection))selection=choices.find(x=>x.value==='btc|bank5pct')?.value||choices[0]?.value||'';
  $('#bank').replaceChildren(...choices.map(x=>{const o=text('option',x.label);o.value=x.value;return o;}));$('#bank').value=selection;
  renderBank();renderOrders();
  $('#updated').textContent='Данные прочитаны '+new Date(snapshot.at*1000).toLocaleString('ru-RU')+'. Реальные ордера этим модулем не отправлялись.';
  const routes=snapshot.connection.routes||[];
  $('#network').textContent=routes.map(x=>x.route+' · '+x.endpoint+': '+(x.data?.country||'')+(x.data?.blocked===true?' · открытие заблокировано':'')+' · '+x.elapsed_ms+' мс').join(' / ');
 }catch(e){$('#load-error').hidden=false;$('#load-error').textContent='Не удалось обновить данные: '+e.message+'. Предыдущие значения могут быть устаревшими.';}finally{busy=false;$('#refresh').disabled=false;}
}
function orderData(){
 const f=$('#order-form'),d=Object.fromEntries(new FormData(f));
 return {...d,post_only:f.elements.post_only.checked,expiration:d.order_type==='GTD'?Math.floor(new Date($('#expiry').value).getTime()/1000):0};
}
async function checkMarket(){
 const token=$('#token'),condition=$('#condition');
 if(!token.reportValidity()||!condition.reportValidity())return;
 $('#check-market').disabled=true;context=null;
 try{context=await api('market?token_id='+encodeURIComponent(token.value)+'&condition='+encodeURIComponent(condition.value));$('#market-info').textContent='Шаг цены '+context.tick_size+'; минимум '+context.min_order_size+' долей; bid '+fmt(context.best_bid)+' / ask '+fmt(context.best_ask)+'. '+(context.accepting_orders?'Рынок принимает заявки.':'Приём заявок не подтверждён.');}
 catch(e){$('#market-info').textContent='Проверка не пройдена: '+e.message;}finally{$('#check-market').disabled=false;}
}
$('#refresh').onclick=refresh;$('#bank').onchange=e=>{selection=e.target.value;renderBank();};
$('#show-stats').onclick=()=>view('stats');$('#show-orders').onclick=()=>view('orders');$('#check-market').onclick=checkMarket;
$('#order-type').onchange=e=>{$('#expiry-field').hidden=e.target.value!=='GTD';$('#expiry').required=e.target.value==='GTD';};
$('#order-form').onsubmit=async e=>{
 e.preventDefault();const button=e.submitter;button.disabled=true;$('#form-result').textContent='';
 try{
  const d=orderData();d.client_id=crypto.randomUUID();
  const result=await api('drafts',{method:'POST',body:JSON.stringify(d)});
  $('#form-result').textContent='Черновик сохранён: '+result.id+'. На биржу ничего не отправлено.';await refresh();
 }catch(e){$('#form-result').textContent='Черновик не сохранён: '+e.message;}finally{button.disabled=false;}
};
for(const e of document.querySelectorAll('input')){e.setAttribute('aria-invalid','false');const sync=()=>e.setAttribute('aria-invalid',e.matches(':user-invalid')?'true':'false');e.addEventListener('blur',sync);e.addEventListener('input',sync);e.addEventListener('invalid',sync);}
const initial=new URL(location.href).searchParams.get('view');view(initial==='orders'?'orders':'stats');
refresh();
let timer=setInterval(()=>{if(!document.hidden)refresh();},15000);
addEventListener('pagehide',()=>clearInterval(timer),{once:true});
