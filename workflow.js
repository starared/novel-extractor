'use strict';
const names={source:'素材',extract:'提取',world:'世界书',opening:'开场白',export:'导出',done:'完成'};
const kinds=['文风','角色','时间','地点','叙事逻辑','人物关系','世界规则','事件'];
let flow=sessionStorage.getItem('workflowId');
const endpoint=op=>'/flows/'+encodeURIComponent(flow)+(op?'/'+op:'');
const post=(url,data={})=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
function busy(value){$('workspace').disabled=value;$('newFlow').disabled=value}
function show(state){
  project=state;$('workspace').hidden=false;
  document.querySelectorAll('[data-step]').forEach(n=>n.hidden=n.dataset.step!==state.step);
  $('skip').hidden=state.step==='done';$('title').value=state.title;
  $('history').textContent=state.history.map(h=>names[h.step]+'：'+h.decision).concat(names[state.step]).join(' → ');
  $('description').value=state.description;$('opening').value=state.opening;
  statusText(state.message);if(state.step==='world')render();
}
async function follow(state){while(state.status==='running'){statusText(state.message);await new Promise(r=>setTimeout(r,1200));state=await(await api(endpoint())).json()}show(state)}
async function perform(fn){busy(true);try{await fn()}catch(e){statusText(e.message)}finally{busy(false)}}
function step(mode,options={}){return perform(async()=>{const r=await post(endpoint('action'),{step:project.step,mode,title:$('title').value,...options});await follow(await r.json())})}
for(const k of kinds){const b=el('input');b.type='checkbox';b.value=k;b.checked=true;field($('kinds'),k,b)}
$('newFlow').onclick=()=>perform(async()=>{if(flow&&project){if(!confirm('放弃当前工作流并清除其数据？'))return;await post(endpoint('discard'))}const j=await(await post('/flows')).json();flow=j.id;sessionStorage.setItem('workflowId',flow);for(const id of ['novel','focus','wishes','filter'])$(id).value='';show(await(await api(endpoint())).json())});
$('upload').onclick=()=>perform(async()=>{const file=$('novel').files[0];if(!file||!file.name.toLowerCase().endsWith('.txt'))throw Error('请选择 TXT 文件。');statusText('上传中，正在分块写入服务器临时文件…');const r=await api(endpoint('upload'),{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Filename':encodeURIComponent(file.name)},body:file});show(await r.json())});
$('skip').onclick=()=>{if(confirm('跳过「'+names[project.step]+'」？本节点的未确认内容不会使用。'))step('skip')};
$('extract').onclick=()=>step('generate',{kinds:[...$('kinds').querySelectorAll(':checked')].map(x=>x.value),limit:Number($('chunkSize').value),instructions:$('focus').value});
$('addRow').onclick=()=>{project.entries.unshift({kind:'手动',name:'新条目',content:'',keys:[],constant:false,enabled:true,position:1,order:100,depth:4,role:0,evidence:[]});$('filter').value='';render();$('rows').firstChild.open=true};
$('confirmWorld').onclick=()=>step('confirm',{entries:project.entries});
$('generate').onclick=()=>{if(($('opening').value||$('description').value)&&!confirm('成功生成后将替换当前草稿。继续吗？'))return;step('generate',{instructions:$('wishes').value})};
$('confirmOpening').onclick=()=>step('confirm',{opening:$('opening').value,description:$('description').value});
$('finish').onclick=()=>step('finish');
for(const fmt of ['png','world'])$(fmt).onclick=()=>perform(async()=>{const r=await post(endpoint(fmt),{title:$('title').value}),a=el('a');a.href=URL.createObjectURL(await r.blob());a.download=($('title').value.replace(/[\\/:*?"<>|]/g,'_')||'小说')+(fmt==='png'?'.png':'.world.json');a.click();setTimeout(()=>URL.revokeObjectURL(a.href),2000)});
function resetModels(){ $('models').replaceChildren(new Option('点击拉取可用模型','')) }
for(const id of ['baseUrl','apiKey','clearKey'])$(id).addEventListener('input',resetModels);
$('models').onchange=()=>{if($('models').value)$('modelName').value=$('models').value};
$('fetchModels').onclick=async()=>{$('settingsFields').disabled=true;resetModels();$('settingsStatus').textContent='正在获取模型列表…';try{const r=await post('/settings/models',{base_url:$('baseUrl').value,api_key:$('apiKey').value,clear_key:$('clearKey').checked});const j=await r.json();$('models').replaceChildren(new Option('请选择模型',''),...j.models.map(m=>new Option(m,m)));$('settingsStatus').textContent=`已获取 ${j.models.length} 个模型，选择后请保存。`}catch(e){$('settingsStatus').textContent=e.message}finally{$('settingsFields').disabled=false}};
if(flow)perform(async()=>{await follow(await(await api(endpoint())).json())});else statusText('点击「新建工作流」开始。');