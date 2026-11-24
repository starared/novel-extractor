'use strict';
const names={source:'素材',extract:'提取',world:'世界书',opening:'开场白',export:'导出',done:'完成'};
const kinds=['文风','角色','时间','地点','叙事逻辑','人物关系','世界规则','事件'];
let flow=null, clearedThrough=0;
// Remove progress and review drafts left by earlier releases on this device.
try{
  sessionStorage.removeItem('workflowId');
  for(const key of Object.keys(sessionStorage)){
    if(/^[A-Za-z0-9_-]{32}events$/.test(key))sessionStorage.removeItem(key);
  }
}catch{}
try{window.indexedDB?.deleteDatabase('shu-world-drafts')}catch{}
window.addEventListener('pagehide',event=>{
  if(flow && !event.persisted){
    navigator.sendBeacon(endpoint('release'),new Blob(['{}'],{type:'application/json'}));
  }
});
const endpoint=op=>'/flows/'+encodeURIComponent(flow)+(op?'/'+op:'');
const post=(url,data={})=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
let logSignature='', logState, visibleEvents=[], visibleCalls=[], modalVersion=0;
const timeLabel=e=>e?new Date(e).toLocaleTimeString('zh-CN',{hour12:false}):'—';
function renderLog(state){
  logState=state;
  $('exchangeWrap').hidden=state.exchange_count===undefined;
  const events=!flow?[]:state.events?.length?state.events:[...(state.history||[]).map(h=>({message:names[h.step]+' · '+h.decision})),{message:state.message||''}];
  const sig=JSON.stringify([flow,events,state.status,state.failed,state.exchange_count,clearedThrough]);
  if(sig===logSignature)return;
  logSignature=sig;
  visibleEvents=events.filter(e=>!clearedThrough||(e.time||0)>clearedThrough);
  const eventCalls=new Map();
  const calls=[];let current,part='';
  for(const e of events){
    const msg=e.message||'';
    if(msg.startsWith('已开始'))part='';
    if(/^第 \d+ 段.*提交给/.test(msg))part=msg.split('已提交给')[0];
    if(msg.startsWith('model.request ')){
      current={time:e.time,part,params:{},index:calls.length};calls.push(current);
    }
    if(!current)continue;
    if(msg.startsWith('model.') || e.level==='ERROR')eventCalls.set(e,current);
    if(/^model\.(request|response|finish) /.test(msg)){
      for(const m of msg.matchAll(/(\w+)=([^ ]+)/g))current.params[m[1]]=m[2];
    }
    if(msg.startsWith('model.upstream_error '))current.error=msg.slice('model.upstream_error '.length);
    if(e.level==='ERROR')current.error=current.error||msg;
  }
  // Archive indices and retained events may each start at a different offset.
  for(const c of calls)c.fromEnd=calls.length-c.index;
  visibleCalls=[...new Set(visibleEvents.map(e=>eventCalls.get(e)).filter(Boolean))];
  const list=$('eventList');list.replaceChildren();
  for(const e of [...visibleEvents].reverse()){
    const c=eventCalls.get(e),error=e.level==='ERROR'||e.message?.startsWith('model.upstream_error');
    const row=el(c?'button':'div');row.className='logRow '+(error?'error':e.level==='WARNING'?'warn':'');
    if(c){row.type='button';row.classList.add('callRow');row.onclick=()=>openCall(c);row.title='查看这次请求与上游回复'}
    row.append(el('span',error?'❌':e.level==='WARNING'?'⚠️':c?'🔗':'ℹ️'),el('time',timeLabel(e.time)),el('span',e.message));
    list.append(row);
  }
  $('eventCount').textContent=visibleEvents.length;
  if(!visibleEvents.length)list.append(el('small','暂无日志'));

}
async function callDetail(c,state,token){
  const v=c.params;
  const sections=[['调用信息',[['任务',state.log_id],['时间',timeLabel(c.time)],['模型',v.model],['段落',c.part],['流式',v.stream],['HTTP',v.http],['耗时（响应头）',v.elapsed],['停止原因',v.reason],['输出 tokens',v.completion_tokens]].filter(([,value])=>value).map(pair=>pair.join('：')).join('\n')]];
  if(c.error||v.reason==='content_filter')sections.push(['上游返回',c.error||JSON.stringify({finish_reason:v.reason})]);
  if(state.exchange_count===undefined)return sections;
  const fetchRecord=async data=>(await post('/flows/'+encodeURIComponent(token)+'/exchange',data)).json();
  const archive=(await fetchRecord({})).slice(0,state.exchange_count);
  const starts=archive.flatMap(([type],i)=>type==='request'?[i]:[]);
  const pos=starts.length-c.fromEnd;
  if(pos<0)return sections;
  for(let i=starts[pos];i<(starts[pos+1]??archive.length);i++){
    const type=archive[i][0];
    if(!['request','reply','error','usage'].includes(type))continue;
    const value=await fetchRecord({index:i});
    if(type==='request'){
      const {messages=[],...params}=value;
      sections.push(['请求参数',JSON.stringify(params,null,2)],...messages.map(m=>['请求消息 · '+m.role,m.content]));
    }else if(type==='usage'){
      sections.push(['上游用量',Object.keys(value).length?JSON.stringify(value,null,2):'上游未报告']);
    }else if(type==='reply'||!c.error)sections.push([type==='reply'?'模型原始回复':'上游返回',value]);
  }
  return sections;
}
async function openCall(c){
  const ticket=++modalVersion,body=$('callBody');body.textContent='读取中…';$('callDialog').showModal();
  try{
    const sections=await callDetail(c,logState,flow);
    if(ticket!==modalVersion)return;
    body.replaceChildren();
    for(const [label,text] of sections)body.append(el('h4',label),el('pre',text));
  }catch(err){if(ticket===modalVersion)body.textContent=err.message}
}
$('closeCall').onclick=()=>$('callDialog').close();
$('callDialog').onclick=e=>{if(e.target===$('callDialog'))$('callDialog').close()};
function downloadLog(name,text){
  const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'})),a=el('a');a.href=url;a.download=name+'_'+new Date().toISOString().slice(0,10)+'.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('exportEvents').onclick=async()=>{
  const state=logState,token=flow;
  const parts=visibleEvents.map(e=>[e.time?new Date(e.time).toISOString():'',e.level||'INFO',e.message].join(' '));
  try{
    for(const c of visibleCalls){
      const sections=await callDetail(c,state,token);
      parts.push('',...sections.map(([label,text])=>label+'\n'+text));
    }
    downloadLog('日志',parts.join('\n'));
  }catch(err){statusText(err.message)}
};
$('clearEvents').onclick=()=>{
  if(!confirm('清空本标签页已显示的日志？'))return;
  const latest=Math.max(1,...(logState.events||[]).map(e=>e.time||0));
  clearedThrough=latest;logSignature='';renderLog(logState);
};
function revealResults(){
  const panel=document.querySelector('[data-step="world"]');
  panel.open=true;
  const first=$('rows').querySelector('details');
  if(project.entries.length===1 && first)first.open=true;
  panel.scrollIntoView?.({block:'start'});
}
function busy(value){$('workspace').disabled=value;$('changeBook').disabled=value}
const pasted=()=>$('pasteSource').value;
$('pasteSource').oninput=()=>{
  if(pasted())$('novel').value='';
  $('filePreview').textContent='';
  $('localFile').textContent=pasted()?'已粘贴 '+pasted().length.toLocaleString()+' 个字符。':'';
};
$('novel').onchange=async()=>{
  const file=$('novel').files[0];
  $('localFile').textContent=file?'本机选中：'+file.name+'，'+file.size.toLocaleString()+' 字节。':'';
  $('filePreview').textContent='';
  if(!file)return;
  $('pasteSource').value='';
  try{
    const bytes=new Uint8Array(await file.slice(0,8192).arrayBuffer());
    let enc=bytes[0]===255&&bytes[1]===254?'utf-16le':bytes[0]===254&&bytes[1]===255?'utf-16be':'utf-8';
    let text;
    try{text=new TextDecoder(enc,{fatal:true}).decode(bytes,{stream:bytes.length<file.size})}
    catch{text=new TextDecoder('gb18030').decode(bytes)}
    if($('novel').files[0]===file)$('filePreview').textContent='文件开头预览\n'+text.slice(0,600);
  }catch{$('localFile').textContent+=' 无法读取本地预览。'}
};
let exchangeVersion=0;
async function displayExchange(){
  const ticket=++exchangeVersion,token=flow;
  const index=$('exchangeList').value;
  if(index==='')return;
  const value=await(await post(endpoint('exchange'),{index:Number(index)})).json();
  if(ticket===exchangeVersion&&token===flow)$('exchangeText').textContent=typeof value==='string'?value:JSON.stringify(value,null,2);
}
async function refreshExchange(){
  const token=flow,records=await(await post(endpoint('exchange'))).json();
  if(token!==flow)return;
  const labels={request:'实际请求',reply:'原始回复',error:'上游错误',cache:'本地复用的候选',usage:'上游用量',accepted:'有效候选'};
  $('exchangeList').replaceChildren(...records.map(([type,part],i)=>new Option((i+1)+' · '+(part?'第 '+part+' 段 · ':'')+(labels[type]||type),i)));
  if(records.length){$('exchangeList').selectedIndex=records.length-1;await displayExchange()}
  else $('exchangeText').textContent='尚无模型调用记录。';
}
function inspectExchange(fn){return fn().catch(err=>{$('exchangeText').textContent=err.message})}

$('exchangeWrap').ontoggle=()=>{if($('exchangeWrap').open)inspectExchange(refreshExchange)};
$('refreshExchange').onclick=()=>inspectExchange(refreshExchange);
$('exchangeList').onchange=()=>inspectExchange(displayExchange);
$('arrange').onclick=()=>step('arrange',{entries:project.entries,instructions:$('arrangeWishes').value});
function showSource(){
  show({step:'source',status:'waiting',message:'选择 TXT 或粘贴正文即可上传。',title:'',
        entries:[],opening:'',description:'',history:[]});
}
function beginSource(){
  if(flow)return;
  const bytes=crypto.getRandomValues(new Uint8Array(16));
  flow=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
}
function show(state){
  if(state.step==='opening' && state.failed){
    state={...state,opening:$('opening').value,description:$('description').value};
  }
  $('changeBook').hidden=state.step==='source';
  $('arrangeBox').hidden=state.step!=='world'||state.exchange_count===undefined;
  $('arrangeBudget').textContent='每次最多编排 12 条，每批调用模型一次。编排只更新位置、顺序、深度、身份和建议理由。';
  renderLog(state);
  const finalPanel=['opening','export','done'].includes(state.step);
  const extracted=state.history.some(h=>h.step==='extract'&&h.decision==='模型提取');
  $('taskState').textContent=(extracted?'提取已完成 · '+state.entries.length+' 条世界书条目。':'')+
    (finalPanel?(state.opening?'开场白已就绪。':'可手写或生成开场白。'):'当前等待你操作：'+names[state.step]+'。');
  const stats=state.cache_stats||{};
  const details=[];
  if(state.source)details.push('服务器收到 '+state.source.size.toLocaleString()+' 字节');
  if(state.part_count)details.push('共 '+state.part_count+' 段');
  if(stats.local)details.push('本地复用 '+stats.local+' 段（未请求上游）');
  if(stats.requests){
    details.push('上游请求 '+stats.requests+' 次');
    details.push(stats.reported?'上游缓存命中 '+stats.tokens.toLocaleString()+' 个输入 token（'+stats.reported+'/'+stats.requests+' 次请求报告）':'上游缓存命中：未报告');
  }
  $('runReason').textContent=details.join(' · ');
  const headerOnly=state.source?.size<256 && /(?:简介|内容简介)[：:]\s*$/.test(state.source.head.trim());
  $('nextAction').textContent=headerOnly?'收到的短文本在空的“简介”处结束。要提取整本，请选择包含章节正文的 TXT。':
    state.step==='world'?'请在「世界书」区域审阅并确认条目。':'';
  const failedGeneration=state.failed || state.status==='waiting' && state.step==='extract' &&
    /模型未返回有效 JSON|模型服务返回 HTTP|无法连接模型|没有取得可回溯|节点失败/.test(state.message);
  if(failedGeneration){
    $('taskState').textContent='提取失败 · 本次未生成新的世界书条目';
    $('nextAction').textContent=state.message+(/过滤|拒绝/.test(state.message)?'。上游未提供具体触发规则。':'。请展开运行日志查看对应请求。');
  }
  $('receipt').hidden=!state.source;
  if(state.source){
    $('receiptSize').textContent=state.source.name+' · 实收 '+state.source.size.toLocaleString()+' 字节。'+(state.source.size<256?'文件很短，请确认选中了完整小说。':'');
  }
  if(!state.source){
    const oldReceipt=(state.events||[]).find(e=>e.message.startsWith('已读取 '));
    if(oldReceipt){$('receipt').hidden=false;$('receiptSize').textContent=oldReceipt.message.split('。')[0]+'。'}
  }
  const result=(state.history.some(h=>h.step==='extract'&&h.decision==='模型提取')?'提取完成，':'')+'当前有 '+state.entries.length+' 条候选。展开条目可编辑；缺少内容可按段补提，审阅后点击「确认世界书」。';
  $('worldFeedback').textContent=state.step==='world'?result:'';
  $('extractFeedback').textContent=state.step==='world'?result:(state.step==='extract'?state.message:'');
  project=state;$('previewContext').hidden=state.revision===undefined;$('supplementBox').hidden=!(state.part_count && state.can_retry);$('partNumber').max=state.part_count||1;$('workspace').hidden=false;
  document.querySelectorAll('details[data-step]').forEach(panel=>{
    const active=panel.dataset.step===(finalPanel?'opening':state.step);
    panel.querySelector('.stage').disabled=!active;
    if(active){panel.open=true;panel.querySelector('.stage').append($('skip'))}
  });
  $('skip').hidden=finalPanel;$('title').value=state.title;
  $('generate').disabled=$('previewContext').disabled=finalPanel && state.step!=='opening';
  const legacyLocked=finalPanel && state.step!=='opening' && !state.direct_download;
  $('opening').readOnly=$('description').readOnly=legacyLocked;
  $('png').disabled=$('world').disabled=state.step==='done'&&!state.download_after_finish;
  $('history').textContent=state.history.filter(h=>!['opening','export','done'].includes(h.step)).map(h=>names[h.step]+'：'+h.decision).concat(finalPanel?'开场白':names[state.step]).join(' → ');
  $('description').value=state.description;$('opening').value=state.opening;
  $('retryExtract').hidden=!state.can_retry;statusText(finalPanel && !state.failed && (state.opening||state.step!=='opening')?'可直接下载角色卡或世界书。':state.message);render();
}
async function follow(state){let since=Date.now(),misses=0;while(state.status==='running'){
    const last=state.events?.at(-1);
    $('runReason').textContent=[state.source?'实收 '+state.source.size.toLocaleString()+' 字节':'',last?.message||state.message].filter(Boolean).join(' · ');
    $('nextAction').textContent=misses?'连接中断，正在自动重连；请保持页面打开，无需再次提交。':'任务正在执行，无需再次提交。';$('taskState').textContent='进行中 · '+names[state.step]+' · '+state.message;renderLog(state);const box=$('streamPreview');box.textContent=state.preview||'';box.hidden=!state.preview;box.scrollTop=box.scrollHeight;
    const bar=$('generationProgress');bar.hidden=false;
    if(state.progress){bar.max=state.progress[1];bar.value=state.progress[0]}else bar.removeAttribute('value');
    const elapsed=' · 已等待 '+Math.max(0,Math.floor((Date.now()-(last?.time||since))/1000))+' 秒';
    if(state.step==='extract')$('extractFeedback').textContent=state.message+elapsed;
    statusText(misses?'状态连接中断，正在自动重连…':state.message+elapsed+(state.progress?'（已完成 '+state.progress.join(' / ')+' 段）':''));
    await new Promise(r=>setTimeout(r,misses?Math.min(1000*2**Math.min(misses,4),10000):500));
    try{state=await(await api(endpoint(),{signal:AbortSignal.timeout(15000)})).json();misses=0}
    catch(error){
      if([401,403,404].includes(error.status))throw Error('无法继续读取任务：'+error.message);
      misses++;
      statusText('状态连接中断，正在重连…');
    }
  }show(state)}
async function perform(fn){busy(true);$('generationProgress').removeAttribute('value');$('generationProgress').hidden=false;try{await fn()}catch(e){$('taskState').textContent='操作未完成';$('nextAction').textContent=e.message;statusText(e.message);$('eventList').prepend(el('p','❌ '+e.message));if(project?.step==='extract')$('extractFeedback').textContent=e.message}finally{busy(false);$('generationProgress').hidden=true;$('streamPreview').hidden=true;$('streamPreview').textContent=''}}
function step(mode,options={}){return perform(async()=>{if(project.step==='source' && mode==='skip')beginSource();const r=await post(endpoint('action'),{step:project.step,mode,title:$('title').value,...options});await follow(await r.json());if(project.step==='world' && ['generate','supplement'].includes(mode))revealResults()})}
for(const k of kinds){const b=el('input');b.type='checkbox';b.value=k;b.checked=true;field($('kinds'),k,b)}
$('changeBook').onclick=()=>perform(async()=>{
  if(!confirm('清除本次制作进度，换一本小说？'))return;
  const response=await fetch(endpoint('discard'),{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  if(!response.ok && response.status!==404)throw Error((await response.json()).error||'清除进度失败');
  clearedThrough=0;
  flow=null;
  for(const id of ['partText','contextList','exchangeText','localFile','filePreview'])$(id).textContent='';
  $('exchangeList').replaceChildren();
  $('partNumber').value=1;
  for(const id of ['novel','pasteSource','arrangeWishes','focus','wishes','roleWishes','filter'])$(id).value='';
  showSource();
});
$('upload').onclick=()=>perform(async()=>{const file=$('novel').files[0]||(pasted().trim()?new File([pasted()],($('title').value||'粘贴正文')+'.txt',{type:'text/plain'}):null);if(!file||!file.name.toLowerCase().endsWith('.txt'))throw Error('请选择 TXT 文件。');if(!file.size)throw Error('请选择非空的 TXT 文件。');beginSource();statusText('上传中，正在分块写入服务器临时文件…');const r=await api(endpoint('upload'),{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Filename':encodeURIComponent(file.name),'X-File-Size':String(file.size)},body:file});show(await r.json())});
$('skip').onclick=()=>{if(confirm('跳过「'+names[project.step]+'」？这一步尚未确认的内容会被跳过。'))step('skip')};
$('extract').onclick=()=>{
  if(project.source?.size<256 && /(?:简介|内容简介)[：:]\s*$/.test(project.source.head.trim()) &&
     !confirm('收到的文件只有 '+project.source.size+' 字节，末尾是空简介。仍要提取这份文件吗？'))return;
  return step('generate',{kinds:[...$('kinds').querySelectorAll(':checked')].map(x=>x.value),limit:Number($('chunkSize').value),instructions:$('focus').value})};
$('addRow').onclick=()=>{project.entries.unshift({kind:'手动',name:'新条目',content:'',keys:[],constant:false,enabled:true,position:1,order:100,depth:4,role:0,evidence:[]});$('filter').value='';render();$('rows').firstChild.open=true};
$('retryExtract').onclick=()=>{if(confirm('重新提取会替换当前条目及手动编辑，继续吗？'))step('retry')};
$('confirmWorld').onclick=()=>step('confirm',{entries:project.entries});
$('generate').onclick=()=>{if(($('opening').value||$('description').value)&&!confirm('成功生成后将替换当前草稿。继续吗？'))return;step('generate',{instructions:$('wishes').value,role_requirements:$('roleWishes').value})};
for(const fmt of ['png','world'])$(fmt).onclick=()=>perform(async()=>{
  const title=$('title').value;
  const fields={title,opening:$('opening').value,description:$('description').value};
  if(!project.direct_download && project.step==='opening'){
    show(await(await post(endpoint('action'),{step:'opening',mode:'confirm',...fields})).json());
  }
  const response=await post(endpoint(fmt),fields);
  const a=el('a');
  a.href=URL.createObjectURL(await response.blob());
  a.download=(title.replace(/[\\/:*?"<>|]/g,'_')||'小说')+(fmt==='png'?'.png':'.world.json');
  a.click();setTimeout(()=>URL.revokeObjectURL(a.href),2000);
});
function resetModels(){ $('models').replaceChildren(new Option('点击拉取可用模型','')) }
for(const id of ['baseUrl','apiKey','clearKey'])$(id).addEventListener('input',resetModels);
$('models').onchange=()=>{if($('models').value)$('modelName').value=$('models').value};
$('fetchModels').onclick=async()=>{$('settingsFields').disabled=true;resetModels();$('settingsStatus').textContent='正在获取模型列表…';try{const r=await post('/settings/models',{base_url:$('baseUrl').value,api_key:$('apiKey').value,clear_key:$('clearKey').checked});const j=await r.json();$('models').replaceChildren(new Option('请选择模型',''),...j.models.map(m=>new Option(m,m)));$('settingsStatus').textContent=`已获取 ${j.models.length} 个模型，选择后请保存。`}catch(e){$('settingsStatus').textContent=e.message}finally{$('settingsFields').disabled=false}};
for(const k of kinds)$('supplementKind').add(new Option(k,k));
$('partNumber').oninput=()=>{$('partText').textContent=''};
$('inspectPart').onclick=()=>perform(async()=>{
  const data=await(await post(endpoint('part'),{part:Number($('partNumber').value)})).json();
  $('partText').textContent=data.text;
});
$('supplement').onclick=()=>step('supplement',{part:Number($('partNumber').value),kinds:[$('supplementKind').value],entries:project.entries});
$('previewContext').onclick=()=>perform(async()=>{
  const rows=await(await post(endpoint('context'),{instructions:$('wishes').value,role_requirements:$('roleWishes').value})).json();
  $('contextList').textContent=rows.map(([kind,name,content])=>kind+' · '+name+'：'+content).join('\n')||'未选入世界书设定';
});
for(const id of ['wishes','roleWishes'])$(id).addEventListener('input',()=>{$('contextList').textContent=''});
showSource();
