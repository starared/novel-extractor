'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(__dirname+'/workflow.js','utf8');
function harness(api){
  const nodes=new Map();
  const element=()=>({value:'',replaceChildren(){},append(){},removeAttribute(){}});
  const context={Date,AbortSignal,project:null,names:{opening:'开场白',extract:'提取'},
    $:id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id)},
    document:{querySelectorAll:()=>[]},renderLog(){},render(){},statusText(){},
    endpoint:()=>'/flows/test',setTimeout:f=>f(),api};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function show(state)'),source.indexOf('async function perform')),context);
  return context;
}
const state=()=>({step:'opening',status:'waiting',message:'done',title:'书',entries:[],opening:'服务端文本',description:'服务端描述',history:[],direct_download:true});
test('failed generation preserves edits; successful generation replaces them',()=>{
  const ctx=harness();
  ctx.$('opening').value='手工开场';ctx.$('description').value='手工描述';
  ctx.show({...state(),failed:true});
  assert.equal(ctx.$('opening').value,'手工开场');
  assert.equal(ctx.project.description,'手工描述');
  ctx.show({...state(),failed:false});
  assert.equal(ctx.$('opening').value,'服务端文本');
  assert.equal(ctx.$('description').value,'服务端描述');
});
test('polling recovers after more than four connection failures without resubmitting',async()=>{
  let calls=0;
  const ctx=harness(async(url,options)=>{
    assert.equal(url,'/flows/test');assert.ok(options.signal);
    if(++calls<=6)throw Error('offline');
    return {json:async()=>state()};
  });
  await ctx.follow({...state(),status:'running'});
  assert.equal(calls,7);
  assert.equal(ctx.$('opening').value,'服务端文本');
});
test('missing or inaccessible tasks stop polling',async()=>{
  for(const status of [401,403,404]){
    let calls=0;
    const ctx=harness(async()=>{calls++;throw Object.assign(Error('gone'),{status})});
    await assert.rejects(ctx.follow({...state(),status:'running'}),/无法继续读取任务/);
    assert.equal(calls,1);
  }
});
