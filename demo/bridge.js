"use strict";
// Browser-only synthetic host. It cannot read files, call models or write real tasks.
(() => {
  const listeners = [];
  const emit = (data) => listeners.forEach(fn => fn({data}));
  const files = [
    {name:'Aurora 界面设计稿.pdf',path:'C:/example/Aurora/Aurora 界面设计稿.pdf',sizeBytes:2488320},
    {name:'Aurora 设计说明.md',path:'C:/example/Aurora/Aurora 设计说明.md',sizeBytes:13312},
    {name:'交互设计清单.docx',path:'C:/example/Studio/交互设计清单.docx',sizeBytes:67584},
  ];
  const hub = {
    available:true,
    tasks:{stats:{running:0,needs_input:0,ready:1},agents:[],tasks:[
      {task_id:'demo-task',source:'acp',ref_id:'demo-run',agent:'dsh',title:'整理 Aurora 设计交接清单',status:'ready',unread:false},
    ]},
    conversations:[{conversation_id:'demo-conversation',title:'Aurora 设计交接',cwd:'C:/example/Aurora',session_live:true,last_activity_at:1791698400,events:[
      {seq:1,run_id:'demo-run',kind:'user',payload:{text:'根据当前设计说明，整理交接前需要核对的内容。'}},
      {seq:2,run_id:'demo-run',kind:'tool',payload:{title:'读取合成设计说明',status:'完成'}},
      {seq:3,run_id:'demo-run',kind:'assistant',payload:{text:'已整理合成样例的交接清单：\n\n1. 核对搜索结果的名称、路径与键盘操作。\n2. 确认未连接引擎时显示离线状态。\n3. 保留现场材料来源，避免混入上一窗口的内容。\n\n这是固定演示回复，没有读取你的文件或调用模型。'}},
    ]}],
    active_run:null,pending_permissions:[],runtime:{state:'demo'},
    system:{cpu:18,memory:46,memory_gb:'14.7/32 GB',gpu:24},
  };
  function refresh() { emit({type:'hub-update',json:JSON.stringify(hub)}); }
  function ready() {
    emit({type:'bounds',maxPanelHeight:535});
    refresh();
    emit({type:'visibility',visible:true});
    // Label all synthetic data even when the product UI calls its host metrics real.
    const tag = document.getElementById('demo-tag');
    tag.title = '本演示所有内容为合成样例，不接入真实宿主';
  }
  window.chrome = window.chrome || {};
  window.chrome.webview = {
    addEventListener: (type, fn) => { if(type === 'message') listeners.push(fn); },
    postMessage: (msg) => {
      if(msg.type === 'ready') setTimeout(ready, 0);
      else if(msg.type === 'search') setTimeout(() => emit({type:'search-result',requestId:msg.requestId,status:'ok',total:3,engine:'demo',error:null,items:files.map((f,i)=>({...f,id:`demo-${msg.requestId}-${i}`,kind:'file',modifiedAt:'2026-10-11'}))}), 80);
      else if(msg.type === 'submit-task') {
        hub.conversations[0].events.push({seq:hub.conversations[0].events.length+1,run_id:'demo-run',kind:'user',payload:{text:msg.text}});
        hub.conversations[0].events.push({seq:hub.conversations[0].events.length+1,run_id:'demo-run',kind:'assistant',payload:{text:'合成演示已收到输入。没有读取文件、执行工具或调用模型。'}});
        emit({type:'hub-submit-result',status:200,json:JSON.stringify({request_id:msg.request_id,conversation_id:'demo-conversation'})});
        refresh();
      } else if(msg.type === 'layout') {
        if(window.frameElement && Number(msg.height)>0)
          window.frameElement.style.height = `${Math.ceil(Number(msg.height))}px`;
      } else if(msg.type === 'open' || msg.type === 'reveal') {
        emit({type:'action-result',ok:false,error:'合成演示不执行文件动作'});
      } else if(msg.type === 'hide' || msg.type === 'exit') {
        emit({type:'visibility',visible:false});setTimeout(()=>emit({type:'visibility',visible:true}),700);
      }
    },
  };
  window.demoScene = () => emit({type:'scene-probe',scene_id:'scene-demo-1',generation:1,scenario:'ide',coverage:'partial',identity:{pid:42,process:'Code.exe',title:'设计说明.md — Aurora（合成样例）',ts:1791698400},materials:[{kind:'selection',source:'demo',chars:86,preview:'合成界面与交接要求',text:'合成材料：文件搜索结果需要显示名称、路径和类型；引擎离线时说明原因；现场材料必须属于同一窗口观测。'}]});
})();
