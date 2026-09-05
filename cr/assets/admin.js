'use strict';
let statsLoaded=false,monitorBusy=false;
const shanghaiValue=ms=>new Date(ms+8*3600000).toISOString().slice(0,16);
const shanghaiTime=seconds=>new Date(seconds*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});
function setStatsRange(kind){
  const now=Date.now(), local=new Date(now+8*3600000);
  let start=Date.UTC(local.getUTCFullYear(),local.getUTCMonth(),local.getUTCDate())-8*3600000;
  if(kind==='week')start-=((local.getUTCDay()+6)%7)*86400000;
  if(kind==='seven')start=now-7*86400000;
  $('statsStart').value=shanghaiValue(start);
  $('statsEnd').value=shanghaiValue(Math.floor(now/60000)*60000+60000);
}
async function loadStatistics(){
  const start=Date.parse($('statsStart').value+':00+08:00')/1000;
  const end=Date.parse($('statsEnd').value+':00+08:00')/1000;
  if(!Number.isFinite(start)||!Number.isFinite(end)||start>=end)throw Error('请选择有效时间段，结束时间须晚于开始时间');
  const result=await api('/api/admin/statistics?'+new URLSearchParams({start,end}));
  const total=result.users.reduce((sum,u)=>sum+u.total_tokens,0);
  $('statsSummary').textContent='合计 '+fmt(total)+' token · 缓存已包含在输入中。待核对请求可能尚有未结算用量。';
  $('statsRows').replaceChildren();
  for(const u of result.users){
    const tr=el('tr');
    for(const value of [u.name,fmt(u.total_tokens),fmt(u.input_tokens),fmt(u.cached_tokens),fmt(u.output_tokens),fmt(u.request_count),fmt(u.unresolved)])tr.append(el('td',value));
    $('statsRows').append(tr);
  }
}
async function loadMonitor(manual=false){
  if(monitorBusy)return;
  monitorBusy=true;
  try{
    const data=await api('/api/admin/upstream/'+(manual?'sample':'monitor'),manual?'POST':'GET');
    const errors={authentication_required:'尚未登录上游账号',upstream_unavailable:'上游暂时不可用',rate_limits_unavailable:'官方额度数据暂不可用',account_identity_unavailable:'无法确认上游账号'};
    $('sampleStatus').textContent=(data.last_success?'最近成功采样：'+shanghaiTime(data.last_success):'正在等待首次成功采样')+' · 每 '+data.interval_seconds+' 秒采样'+(data.error?' · '+(errors[data.error]||'采样失败'):'')+(data.stale?' · 当前数据未更新':'');
    $('monitorWindows').replaceChildren();
    for(const w of data.windows){
      const c=el('article',undefined,'window-card');
      const duration=w.minutes>=1440?(w.minutes/1440)+' 天':(w.minutes/60)+' 小时';
      c.append(el('h3',w.name+' · '+duration+'窗口'),el('div',fmt(w.remaining_percent)+'% 剩余','number'));
      const status={collecting:'等待累计至少 1% 的变化',unmapped_bucket:'此额度池没有可对应的本网关 token 统计',no_relay_usage:'上游额度已变化，但本网关没有新增 token'};
      c.append(el('p',w.tokens_per_percent===null?status[w.status]:('约 '+fmt(Math.round(w.tokens_per_percent))+' token / 1%'),'estimate'));
      c.append(el('p','观察区间：'+shanghaiTime(w.sample_start)+' — '+shanghaiTime(w.sample_end),'muted'));
      c.append(el('p','本网关新增 '+fmt(w.delta_tokens)+' token · 上游消耗 '+fmt(w.delta_percent)+' 个百分点','muted'));
      c.append(el('p','额度重置：'+shanghaiTime(w.resets_at),'muted'));
      $('monitorWindows').append(c);
    }
  }finally{monitorBusy=false;}
}
async function refreshAdminTools(){
  $('adminTools').hidden=role!=='admin';
  if(role!=='admin')return;
  if(!statsLoaded){setStatsRange('today');statsLoaded=true;}
  await Promise.all([loadStatistics(),loadMonitor()]);
}
$('statsForm').onsubmit=event=>{event.preventDefault();loadStatistics().catch(e=>notify(e.message));};
for(const b of document.querySelectorAll('[data-range]'))b.onclick=()=>{setStatsRange(b.dataset.range);loadStatistics().catch(e=>notify(e.message));};
$('bulkBudgetForm').onsubmit=async event=>{
  event.preventDefault();const b=event.submitter,budget=Number($('allBudget').value);
  if(!Number.isSafeInteger(budget)||budget<0||budget>1e12)return notify('请输入有效的非负整数预算');
  b.disabled=true;
  try{await api('/api/admin/budget/all','PATCH',{budget});await refresh();notify('所有用户的预算已设为 '+fmt(budget)+' token');}
  catch(e){notify(e.message);}finally{b.disabled=false;}
};
$('resetAllUsage').onclick=async()=>{
  if(!confirm('将所有用户已用额度归零。历史统计和在途／待核对预扣保留。确认重置？'))return;
  const b=$('resetAllUsage');b.disabled=true;
  try{await api('/api/admin/usage/reset','POST');await refresh();notify('所有用户已用额度已归零，历史记录已保留');}
  catch(e){notify(e.message);}finally{b.disabled=false;}
};
$('sampleNow').onclick=async()=>{const b=$('sampleNow');b.disabled=true;try{await loadMonitor(true);}catch(e){notify(e.message);}finally{b.disabled=false;}};
setInterval(()=>{if(key&&role==='admin'&&!document.hidden)loadMonitor().catch(()=>{});},15000);
