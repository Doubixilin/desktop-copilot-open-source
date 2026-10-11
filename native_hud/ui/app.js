"use strict";
// 原生 HUD 页面（宿主：NativeHud.exe / WebView2）。
// 文件搜索经窄 JSON 桥接走真实引擎（listary_engine）；日程、待办、系统指标、
// DSH 对话均为合成演示数据。无网络请求（CSP connect-src 'none'）。
const $ = (id) => document.getElementById(id);
const bridge = window.chrome?.webview ?? null;
const post = (msg) => { if (bridge) bridge.postMessage(msg); };
const icons = {
  search: '<svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.7"/><path d="m16 16 4.5 4.5"/></svg>',
  spark: '<svg viewBox="0 0 24 24"><path d="M12 2.5c.6 6.3 3.2 8.9 9.5 9.5-6.3.6-8.9 3.2-9.5 9.5C11.4 15.2 8.8 12.6 2.5 12 8.8 11.4 11.4 8.8 12 2.5Z"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/></svg>',
  down: '<svg class="chevron" viewBox="0 0 16 16"><path d="m4 6 4 4 4-4"/></svg>',
  send: '<svg viewBox="0 0 24 24"><path d="M12 19V5m-5 5 5-5 5 5"/></svg>',
  back: '<svg viewBox="0 0 20 20"><path d="m11 5-5 5 5 5m-5-5h10"/></svg>',
  attach: '<svg viewBox="0 0 24 24"><path d="m8 12 6-6a3 3 0 0 1 4 4l-8 8a4.3 4.3 0 0 1-6-6l8-8"/></svg>',
  calendar: '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="16" rx="3"/><path d="M8 3v4m8-4v4M4 11h16m-11 4h2m3 0h2"/></svg>',
  pulse: '<svg viewBox="0 0 24 24"><path d="M2 12h5l3-7 4 14 3-7h5"/></svg>',
  arrow: '<svg viewBox="0 0 20 20"><path d="m8 5 5 5-5 5"/></svg>',
};
const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const today = {
  scenario:'connected',
  tasks:[
    {id:'review',title:'完成方案评审',list:'Aurora',due:'昨天到期',overdue:true,done:false,pending:false},
    {id:'purchase',title:'确认采购清单',list:'工作',due:'今天',done:false,pending:false},
    {id:'notes',title:'整理本周工作笔记',list:'个人',due:'今天',done:false,pending:false},
    {id:'reply',title:'回复视觉稿反馈',list:'Studio',due:'今天',done:false,pending:false},
    {id:'plan',title:'梳理下周优先事项',list:'个人',due:'今天',done:false,pending:false},
  ],
  events:[
    {start:'14:30',end:'15:00',title:'产品方案讨论',location:'Aurora · 线上会议',next:true},
    {start:'16:00',end:'16:30',title:'视觉方案评审',location:'Studio · 线上会议'},
    {start:'17:30',end:'18:00',title:'本周回顾',location:'个人 · 专注时间'},
  ],
};
const state = {
  visible:false, mode:'files', drafts:{files:'',chat:''}, selected:0,
  peek:null, detailFile:null, picker:false, activity:false,
  attachment:null, conversationId:null, scene:null, sceneKey:'', sceneHistory:[], sceneProbe:null, sceneAttached:true,
  sceneId:'', sceneGen:0,
  hub:{connected:null, reason:'', data:null},
  run:null, permission:null, composing:false, dev:false, todayPendingId:null,
  // B01/B02/R09：提交身份与在途状态——草稿保留到受理回执；未知结果沿用同一 request_id
  submit:null, pendingRetry:null, reqSeq:0,
  search:{gen:0, itemsGen:0, status:'idle', items:[], total:0, error:null, engine:''},
};
function newRequestId() {
  try { if (window.crypto && crypto.randomUUID) return 'req-' + crypto.randomUUID(); } catch (e) {}
  state.reqSeq += 1;
  return `req-${Date.now().toString(36)}-${state.reqSeq}`;
}
// 只有与当前代次一致且已接受的 ok 结果才可执行动作（防旧结果误开）
function resultsActionable() {
  const s = state.search;
  return s.status === 'ok' && s.itemsGen === s.gen && s.items.length > 0;
}
let toastTimer;
let undoAction = null;
let debounceTimer = 0;
function toast(message, undo = null) {
  undoAction = undo;
  $('toast').innerHTML = `${esc(message)}${undo ? '<button id="undo-action">撤销</button>' : ''}`;
  $('toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').classList.remove('show'); undoAction = null; }, undo ? 6500 : 3000);
}
// ---------- 宿主可见性（真实隐藏由宿主完成，这里只维护页面态） ----------
// ---------- 现场行（M1：L0 前台身份流，宿主 scene-update 推送） ----------
function fmtSceneTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
// ---------- 现场观测代次（票02：scene_id 变化即新一轮，旧代次迟到结果整体丢弃） ----------
function sceneRoundAccept(m) {
  const sid = m && typeof m === 'object' && typeof m.scene_id === 'string' && m.scene_id ? m.scene_id : null;
  const gen = m && Number(m.generation) > 0 ? Number(m.generation) : 0;
  if (!sid) return true; // 无代次标识（旧宿主/测试桩）：按当前轮接受
  if (sid === state.sceneId) return true;
  if (gen > state.sceneGen) {
    // 新代次：默认不携带上一现场材料与身份（R04）；等本轮采集到达，纯文字可立即发送
    state.sceneId = sid;
    state.sceneGen = gen;
    state.sceneProbe = { materials: [] };
    state.sceneAttached = true;
    state.scene = null;
    state.sceneKey = '';
    return true;
  }
  return false; // 旧代次迟到：丢弃，不得污染当前现场
}
// 同代次材料合并（R05）：按 kind+文本哈希去重，保留独立来源；replaceKinds 整类替换
function materialKey(x) { return `${x.kind || ''}|${x.text || x.preview || ''}`; }
function mergeSceneMaterials(incoming, replaceKinds) {
  const cur = state.sceneProbe && Array.isArray(state.sceneProbe.materials) ? state.sceneProbe.materials : [];
  const base = replaceKinds && replaceKinds.length
    ? cur.filter((x) => !(x && typeof x === 'object' && replaceKinds.includes(x.kind)))
    : cur.filter((x) => x && typeof x === 'object');
  const seen = new Set(base.map(materialKey));
  for (const m of incoming) {
    if (!m || typeof m !== 'object') continue;
    const k = materialKey(m);
    if (seen.has(k)) continue; // 重复内容（fast/full 重叠、重复 OCR）不堆叠
    seen.add(k);
    base.push(m);
  }
  return base;
}
// 现场行由 当前身份 + 当前代次材料摘要 统一渲染（重复探测不重复追加）
function renderSceneLine() {
  const el = $('scene-line');
  const cur = state.scene;
  if (!cur || typeof cur !== 'object' || !cur.title) {
    el.hidden = true;
    el.removeAttribute('title');
    return;
  }
  const name = (cur.process || '').replace(/\.exe$/i, '') || `pid ${cur.pid}`;
  const parts = sceneSummaryParts();
  el.textContent = `◆ 现场 ${name} — ${cur.title}${parts.length ? ` ◦ ${parts.join(' · ')}` : ''}`;
  const hist = state.sceneHistory
    .map((h) => `${fmtSceneTime(h.ts)} ${(h.process || '').replace(/\.exe$/i, '')} ${h.title}`)
    .join('\n');
  el.title = `${cur.exe || name} · ${fmtSceneTime(cur.ts)}${hist ? `\n\n最近现场：\n${hist}` : ''}`;
  const p = state.sceneProbe || {};
  const cov = p.coverage === 'full' ? '材料覆盖：完整' : p.coverage === 'partial' ? '材料覆盖：部分' : '';
  const lines = (Array.isArray(p.materials) ? p.materials : [])
    .filter((x) => x && typeof x === 'object')
    .map((x) => `${SCENE_KIND_LABELS[x.kind] || x.kind}(${x.chars || 0}字)：${x.preview || ''}`);
  const extra = [cov, p.note || ''].filter(Boolean).concat(lines);
  if (extra.length) el.title += `${el.title ? '\n' : ''}${extra.join('\n')}`;
  el.hidden = false;
}
function sceneSummaryParts() {
  return state.sceneProbe && Array.isArray(state.sceneProbe.materials)
    ? state.sceneProbe.materials
        .filter((x) => x && typeof x === 'object' && SCENE_KIND_LABELS[x.kind])
        .map((x) => `${SCENE_KIND_LABELS[x.kind]} ${x.chars || 0}字`)
    : [];
}
function onHostScene(m) {
  if (!sceneRoundAccept(m)) return;
  const el = $('scene-line');
  const cur = m && typeof m === 'object' ? m.current : null;
  if (!cur || typeof cur !== 'object' || !cur.title) {
    state.scene = null;
    renderSceneLine();
    return;
  }
  const key = `${cur.pid}|${cur.title}`;
  if (!state.scene || state.sceneKey !== key) {
    state.scene = cur;
    state.sceneKey = key;
    state.sceneHistory.unshift({ title: cur.title, process: cur.process || '', ts: cur.ts });
    state.sceneHistory = state.sceneHistory.slice(0, 5);
  } else {
    state.scene = cur;
  }
  renderSceneLine();
}
// F08：宿主呼出时发布的代次重置——立即废弃旧材料/旧身份（等待新一轮采集，
// 期间允许纯文字发送）。sceneRoundAccept 已完成重置，这里只刷新展示。
function onHostSceneRound(m) {
  if (!sceneRoundAccept(m)) return;
  renderSceneLine();
  renderSceneStrip();
}
// ---------- 探测结果（M2：召唤即对 prevFg 做 L1 内容拉取） ----------
const SCENE_KIND_LABELS = {
  file: '文件', selection: '选区', dialog_text: '弹窗文本',
  dialog_buttons: '按钮', terminal_tail: '终端尾部', screen_text: '屏幕文字',
  clipboard: '剪贴板',
};
function onHostClipboard(m) {
  // M7：剪贴板信号。只接文件列表（文本内容宿主侧不出境）；同代次整类替换不堆叠。
  if (!m || typeof m !== 'object') return;
  if (!sceneRoundAccept(m)) return;
  const files = Array.isArray(m.files) ? m.files.filter((f) => typeof f === 'string' && f) : [];
  if (!files.length) return;
  const names = files.map((f) => String(f).split(/[\\/]/).pop());
  const clipMaterial = {
    kind: 'clipboard', source: 'win32:clipboard',
    chars: files.join('\n').length,
    preview: `${files.length} 个文件：${names.slice(0, 3).join('、')}${files.length > 3 ? '…' : ''}`,
    text: files.join('\n'),
  };
  if (!state.sceneProbe || typeof state.sceneProbe !== 'object') state.sceneProbe = { materials: [] };
  if (!Array.isArray(state.sceneProbe.materials)) state.sceneProbe.materials = [];
  state.sceneProbe.materials = mergeSceneMaterials([clipMaterial], ['clipboard']);
  state.sceneAttached = true;
  renderSceneStrip();
  renderSceneLine();
  toast(`已带上剪贴板 ${files.length} 个文件`);
}
function onHostCapture(m) {
  // M4 L2 兜底材料到达：并入现场材料并显性提示（仅文本，不存图像）
  if (!m || typeof m !== 'object' || !m.material || typeof m.material !== 'object') return;
  if (!sceneRoundAccept(m)) return;
  const mat = m.material;
  if (!mat.kind || typeof mat.text !== 'string' || !mat.text) return;
  if (!state.sceneProbe || typeof state.sceneProbe !== 'object') state.sceneProbe = { materials: [] };
  if (!Array.isArray(state.sceneProbe.materials)) state.sceneProbe.materials = [];
  state.sceneProbe.materials = mergeSceneMaterials([
    { kind: mat.kind, source: mat.source || 'ocr', chars: mat.chars || mat.text.length, preview: mat.preview || '', text: mat.text },
  ]);
  state.sceneAttached = true;
  renderSceneStrip();
  renderSceneLine();
  toast(`已识别当前窗口文字 ${mat.chars || mat.text.length} 字（仅文本，不存图像）`);
}
function onHostProbe(m) {
  if (!m || typeof m !== 'object') return;
  if (!sceneRoundAccept(m)) return; // 旧代次探测：整个消息丢弃（R04）
  // R05：探测材料并入当前代次（fast→full 重叠按文本去重），不清掉剪贴板/OCR 材料
  const mats = Array.isArray(m.materials) ? m.materials.filter((x) => x && typeof x === 'object') : [];
  const prev = state.sceneProbe && typeof state.sceneProbe === 'object' ? state.sceneProbe : {};
  state.sceneProbe = {
    scenario: typeof m.scenario === 'string' ? m.scenario : (prev.scenario || ''),
    coverage: typeof m.coverage === 'string' ? m.coverage : (prev.coverage || ''),
    note: typeof m.note === 'string' ? m.note : (prev.note || ''),
    identity: m.identity && typeof m.identity === 'object' ? m.identity : (prev.identity || null),
    materials: mergeSceneMaterials(mats),
  };
  state.sceneAttached = true; // 新现场重新默认附带，用户可再次显式移除
  if (m.identity && typeof m.identity === 'object' && m.identity.title)
    onHostScene({ type: 'scene-update', scene_id: m.scene_id, generation: m.generation, current: m.identity });
  else
    renderSceneLine();
  renderSceneStrip();
  requestAnimationFrame(fitBody);
}
function onHostVisibility(visible) {
  if (visible === state.visible) return;
  state.visible = visible;
  if (visible) {
    $('hud').inert = false;
    $('hud').classList.remove('is-hidden');
    // F08：呼出即废弃上一现场材料与身份——新宿主随后发布 scene-round 代次；
    // 旧宿主无该消息时页面侧也保证不携带旧材料（采集完成前允许纯文字发送）
    state.sceneProbe = { materials: [] };
    state.sceneAttached = true;
    state.scene = null;
    state.sceneKey = '';
    renderSceneLine();
    render();
    $('command').focus({preventScroll:true});
    // 呼出时工作区/DPI 可能已变化：下一帧主动重报面板几何（背板跟随）
    requestAnimationFrame(reportLayout);
    // 恢复时按当前状态刷新：有查询词则重新搜索，结果可能已过期
    if (state.mode === 'files' && state.drafts.files.trim()) issueSearch(true);
  } else {
    // 隐藏让旧搜索失效；迟到响应不得覆盖恢复后的界面；同时失效宿主侧动作映射
    state.search.gen++;
    clearTimeout(debounceTimer);
    post({type:'cancel-search'});
    $('hud').classList.add('is-hidden');
    $('hud').inert = true;
  }
}
function requestHide() { post({type:'hide'}); }
// ---------- 模式 ----------
function switchMode(mode) {
  if (mode === state.mode) { state.peek=null; render(); $('command').focus(); return; }
  state.drafts[state.mode] = $('command').value;
  state.mode = mode;
  state.picker = false;
  state.activity = false;
  state.detailFile = null;
  state.peek = null;
  state.search.gen++; // 切换模式让在途搜索失效
  clearTimeout(debounceTimer);
  post({type:'cancel-search'});
  $('command').value = state.drafts[mode];
  render();
  // 返回文件模式且有查询词：重发查询（旧响应已被丢弃，searching 需要有完成途径）
  if (mode === 'files' && state.drafts.files.trim()) issueSearch(true);
  $('command').focus();
}
// ---------- 真实文件搜索（桥接） ----------
function issueSearch(immediate = false) {
  const query = state.drafts.files.trim();
  const gen = ++state.search.gen;
  clearTimeout(debounceTimer);
  // 新输入/清空立即失效宿主侧旧动作映射（不等防抖、不等请求）
  post({type:'cancel-search'});
  if (!query) {
    state.search.status = 'idle';
    state.search.items = [];
    state.selected = 0;
    renderFiles();
    return;
  }
  state.search.status = 'searching';
  if (!immediate) renderFiles();
  debounceTimer = setTimeout(() => {
    if (gen !== state.search.gen) return;
    post({type:'search', requestId:gen, query});
  }, immediate ? 0 : 150);
}
function onSearchResult(msg) {
  if (msg.requestId !== state.search.gen) return; // 迟到/旧代次：丢弃
  const s = state.search;
  s.status = msg.status === 'ok' ? 'ok' : msg.status;
  s.items = Array.isArray(msg.items) ? msg.items : [];
  s.itemsGen = msg.requestId; // 记录被接受结果对应的代次；动作只认这一代
  s.total = typeof msg.total === 'number' ? msg.total : s.items.length;
  s.error = msg.error || null;
  s.engine = msg.engine || '';
  state.selected = 0;
  // 打开中的文件详情随新结果重解析标识；结果中已不存在则退回列表
  if (state.detailFile) {
    state.detailFile = s.items.find(it => it.path === state.detailFile.path) || null;
  }
  if (state.mode === 'files' && !state.peek) renderFiles();
}
function fileExt(item) {
  if (item.kind === 'folder') return 'DIR';
  const m = /\.([A-Za-z0-9]{1,6})$/.exec(item.name);
  return m ? m[1].toUpperCase() : 'FILE';
}
function parentDir(path) {
  const i = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'));
  return i > 0 ? path.slice(0, i) : path;
}
function fmtSize(bytes) {
  if (!bytes || bytes <= 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
  return (bytes / 1073741824).toFixed(2) + ' GB';
}
function renderFiles() {
  if (state.detailFile) { renderFileDetail(); return; }
  if (state.peek) { renderDetail(); return; }
  const query = state.drafts.files.trim();
  if (!query) { renderGlance(); return; }
  const s = state.search;
  if (s.status === 'searching') {
    $('content').innerHTML = `<div class="searching"><span class="thinking-dot"></span>正在搜索…</div>`;
    return;
  }
  if (s.status === 'unavailable') {
    $('content').innerHTML = `<div class="empty">${icons.search}<p>搜索引擎不可用</p><small>Everything / Listary 本地接口未连接，与“没有结果”不同；启动引擎后重试</small></div>`;
    return;
  }
  if (s.status === 'error') {
    $('content').innerHTML = `<div class="empty">${icons.search}<p>搜索出错</p><small>${esc(s.error || '未知错误')}</small></div>`;
    return;
  }
  if (s.status === 'empty' || !s.items.length) {
    $('content').innerHTML = `<div class="empty">${icons.search}<p>没有找到相关文件</p><small>引擎在线，当前查询无匹配</small></div>`;
    return;
  }
  state.selected = Math.max(0, Math.min(state.selected, s.items.length - 1));
  const engineLabel = s.engine === 'everything' ? 'Everything' : s.engine === 'listary' ? 'Listary' : '引擎';
  $('content').innerHTML = `<div class="section-label">文件 <span>前 ${s.items.length} 条 · 共 ${s.total} 个结果 · ${engineLabel}</span></div><div class="result-list" role="listbox" aria-label="文件搜索结果">${s.items.map((f, i) => `<button id="result-${f.id}" class="result-row ${i === state.selected ? 'selected' : ''}" data-idx="${i}" role="option" aria-selected="${i === state.selected}"><span class="file-symbol ${f.kind === 'folder' ? 'dir' : ''}">${esc(fileExt(f))}</span><span class="file-text"><span class="file-name">${esc(f.name)}</span><span class="file-path">${esc(parentDir(f.path))}</span></span><span class="file-meta">${esc(fmtSize(f.sizeBytes))}</span><span class="row-detail" data-detail="${i}">详情</span><kbd class="result-enter">↵</kbd></button>`).join('')}</div>`;
  $('command').setAttribute('aria-activedescendant', `result-${s.items[state.selected].id}`);
  requestAnimationFrame(fitBody);
}
function selectFile(index) {
  const s = state.search;
  if (!s.items.length || state.detailFile) return;
  state.selected = (index + s.items.length) % s.items.length;
  renderFiles();
  document.querySelector('.result-row.selected')?.scrollIntoView({block:'nearest'});
}
function openFileDetail(index) {
  if (!resultsActionable()) return; // 搜索中/旧代次不可进入详情
  const item = state.search.items[index];
  if (!item) return;
  state.detailFile = item;
  renderFiles();
  requestAnimationFrame(() => $('content').querySelector('.back-button')?.focus({preventScroll:true}));
}
function renderFileDetail() {
  const f = state.detailFile;
  $('command').removeAttribute('aria-activedescendant');
  $('content').innerHTML = `<article class="file-detail"><div class="preview-toolbar"><button class="back-button" data-action="back">${icons.back} 搜索结果</button><span class="preview-badge">真实文件 · 仅元数据</span></div><h2>${esc(f.name)}</h2><div class="file-detail-path">${esc(f.path)}</div><div class="file-detail-meta"><span>${f.kind === 'folder' ? '文件夹' : '文件'}</span>${f.sizeBytes ? `<span>${esc(fmtSize(f.sizeBytes))}</span>` : ''}<span>引擎：${esc(state.search.engine || '未知')}</span></div><div class="file-detail-actions"><button class="suggestion" data-action="open" data-id="${esc(f.id)}">打开 ${icons.arrow}</button><button class="suggestion" data-action="reveal" data-id="${esc(f.id)}">在文件夹中显示 ${icons.arrow}</button><button class="suggestion" data-action="handoff" data-id="${esc(f.id)}">${icons.spark} 把路径交给 DSH</button></div><p class="file-detail-note">首版不读取文件内容；交给 DSH 仅传递路径引用（演示，不调用模型）。</p></article>`;
  requestAnimationFrame(fitBody);
}
function fileAction(action, id) {
  // 统一守卫：标识必须属于当前代次已接受的 ok 结果；等待/旧代次一律拒绝（含详情页按钮与 Enter）
  const s = state.search;
  const valid = s.status === 'ok' && s.itemsGen === s.gen && s.items.some(it => it.id === id);
  if (!valid) {
    toast('结果已更新，请重新搜索');
    return;
  }
  post({type:action, id});
}
function onActionResult(msg) {
  if (msg.ok) {
    requestHide(); // 打开/定位成功后收起，避免遮挡目标窗口
    return;
  }
  const reason = {stale_or_unknown:'结果已失效，请重新搜索', not_found:'文件不存在或已被移动', launch_failed:'系统拒绝了打开请求'}[msg.error] || '操作失败';
  toast(reason);
}
// ---------- 布局 ----------
// 宿主窗口即面板矩形：页面铺满视口，只需上报自然内容高度（宿主定位置与宽度，
// 按此伸缩窗口）。ResizeObserver 在高度过渡期间连续触发，动画结束后的最终
// 高度也会同步。面板可用高度上限由宿主 bounds 消息写入 --panel-max-h。
function reportLayout() {
  const hud = $('hud');
  post({type:'layout', x:0, y:0, width:hud.offsetWidth, height:hud.offsetHeight});
}
new ResizeObserver(reportLayout).observe($('hud'));
// 位置变化（宽高不变）不会触发尺寸观察器：视口/工作区变化与呼出恢复都要主动上报
window.addEventListener('resize', () => { fitBody(); reportLayout(); });
function fitBody() {
  const expanded = !state.picker && !state.activity && Boolean($('content').innerHTML.trim());
  $('body-shell').style.height = expanded ? `${$('content').offsetHeight}px` : '0px';
  $('body-shell').inert = !expanded;
  $('body-shell').setAttribute('aria-hidden', String(!expanded));
  if (state.mode === 'files') $('command').setAttribute('aria-expanded', String(expanded && Boolean(state.drafts.files.trim()) && !state.peek && !state.detailFile));
  else $('command').removeAttribute('aria-expanded');
}
new ResizeObserver(fitBody).observe($('content'));
function render() {
  const chat = state.mode === 'chat';
  $('mode-icon').innerHTML = chat ? icons.spark : icons.search;
  $('command').placeholder = chat ? '交给 DSH，想做些什么？' : '搜索文件，或按 Tab 与 DSH 对话';
  $('command').setAttribute('aria-label', chat ? '向 DSH 输入任务' : '搜索文件');
  $('command').setAttribute('role', chat ? 'textbox' : 'combobox');
  $('command').removeAttribute('aria-activedescendant');
  for (const [id, active] of [['files-mode',!chat],['chat-mode',chat]]) {
    $(id).classList.toggle('active',active);
    $(id).setAttribute('aria-pressed',String(active));
  }
  $('submit').hidden = !chat;
  updateSubmitEnabled();
  $('context-strip').hidden = !chat;
  const conv = currentConv();
  $('project-button').innerHTML = `${icons.folder}${esc(conv ? (conv.title || '会话') : '新会话')}${icons.down}`;
  $('project-button').title = conv ? (conv.cwd || conv.conversation_id) : '下次发送时创建新会话';
  $('project-button').setAttribute('aria-expanded',String(state.picker));
  $('conversation-label').textContent = conv ? (conv.title || conv.conversation_id) : '新会话（发送时创建）';
  $('key-hints').innerHTML = chat ? '<kbd>Tab</kbd> 文件 <span class="hint-divider"></span><kbd>↵</kbd> 发送 <span class="hint-divider"></span><kbd>Esc</kbd> 收起' : '<kbd>Tab</kbd> 切换 DSH <span class="hint-divider"></span><kbd>↵</kbd> 打开 <span class="hint-divider"></span><kbd>Esc</kbd> 收起';
  renderPicker();
  renderAttachment();
  renderSceneStrip();
  const dt = todayData();
  $('demo-tag').textContent = dt.real ? '滴答实时' : '演示数据';
  $('demo-tag').title = dt.real
    ? '今日待办与日程来自滴答清单（官方 MCP，5 分钟轮询；勾选直接写回）'
    : '日程与待办为合成演示数据；文件搜索、系统指标与 DSH 对话为真实数据';
  if (chat) renderChat(); else renderFiles();
  renderActivity();
  renderSystemButton();
  requestAnimationFrame(fitBody);
}
// ---------- 会话选择器（M5：真实会话） ----------
function renderPicker() {
  $('project-picker').hidden = !state.picker;
  if (!state.picker) return;
  const convs = hubConvs();
  const cur = currentConv();
  $('project-picker').innerHTML = `<div class="picker-heading">会话</div><button class="project-option ${!state.conversationId?'current':''}" data-conversation="">${icons.spark}<div><strong>新会话</strong><small>下次发送时创建</small></div><span class="check">${!state.conversationId?'✓':''}</span></button>${convs.map(c=>`<button class="project-option ${cur&&cur.conversation_id===c.conversation_id?'current':''}" data-conversation="${esc(c.conversation_id)}">${icons.folder}<div><strong>${esc(c.title||c.conversation_id)}</strong><small>${fmtSceneTime(c.last_activity_at)} · ${c.session_live?'已连接':'空闲'}</small></div><span class="check">${cur&&cur.conversation_id===c.conversation_id?'✓':''}</span></button>`).join('')}<div class="picker-note">真实会话（任务中枢）。新会话在下次发送时创建。</div>`;
}
function renderAttachment() {
  const visible = state.mode === 'chat' && state.attachment;
  $('attachment-strip').hidden = !visible;
  if (visible) $('attachment-strip').innerHTML = `<span class="attachment-chip">${icons.attach}${esc(state.attachment.name)}<button id="remove-attachment" aria-label="移除文件引用">×</button></span><span>路径引用 · 演示</span>`;
}
// ---------- 任务中枢（M5：server.py /copilot/hud 真通道） ----------
function hubData() { return state.hub.data; }
function hubTasks() { const d = hubData(); return d && d.tasks && Array.isArray(d.tasks.tasks) ? d.tasks.tasks : []; }
function hubStats() { const d = hubData(); return d && d.tasks ? (d.tasks.stats || {}) : {}; }
function hubConvs() { const d = hubData(); return d && Array.isArray(d.conversations) ? d.conversations : []; }
function hubPerms() { const d = hubData(); return d && Array.isArray(d.pending_permissions) ? d.pending_permissions : []; }
function hubActiveRun() { const d = hubData(); return (d && d.active_run) || null; }
function currentConv() {
  // B01：未选会话就是新会话空态——展示不得回退到最近会话（否则界面看似续问、发送却新建）
  const list = hubConvs();
  if (!state.conversationId) return null;
  return list.find((c) => c.conversation_id === state.conversationId) || null;
}
let lastHubJson = '';
function onHubUpdate(m) {
  const raw = String(m.json || '');
  let data = null;
  try { data = JSON.parse(raw); } catch (e) { return; }
  const structurallySame = raw === lastHubJson;
  lastHubJson = raw;
  const prev = state.hub.data;
  state.hub.data = data;
  state.hub.connected = true;
  state.hub.reason = '';
  const active = hubActiveRun();
  if (active && !state.conversationId) state.conversationId = active.conversation_id;
  // B01：已选会话从中枢列表消失（超出总览护栏等）→ 显示与提交一同回新会话态，不悄悄续用旧 ID
  if (state.conversationId) {
    const stillBound = hubConvs().some((c) => c.conversation_id === state.conversationId)
      || (active && active.conversation_id === state.conversationId);
    if (!stillBound) state.conversationId = null;
  }
  if (structurallySame) return;  // 无变化不重渲染——全量 render 会吃掉进行中的点击
  // 会话切换/事件数/在跑态变化才重建对话区（聊天滚动与点击不被 2s 心跳打断）
  const chatChanged = !prev || JSON.stringify([prev.conversations, prev.active_run])
    !== JSON.stringify([data.conversations, data.active_run]);
  renderActivity();
  renderSystemButton();
  if (state.mode === 'chat') {
    if (chatChanged) renderChat();
  } else if (state.peek === 'system') {
    renderDetail();
  } else if (state.peek === 'today') {
    renderDetail();
  } else if (!state.detailFile && !state.drafts.files.trim()) {
    renderGlance();  // 今日速览随 today 段变化刷新（轮询到写回结果等）
    requestAnimationFrame(fitBody);
  }
}
function onHubUnavailable(m) {
  state.hub.connected = false;
  state.hub.reason = String((m && m.reason) || '').slice(0, 120);
  render();
}
function updateSubmitEnabled() {
  $('submit').disabled = Boolean(state.submit) || !state.drafts.chat.trim() || (state.hub.connected === false);
}
function onHubSubmitResult(m) {
  let body = {};
  try { body = JSON.parse(String(m.json)); } catch (e) {}
  const sent = state.submit;
  if (!sent) return; // 无在途提交：回执无可消费，不动任何草稿
  // F06：回执必须对应本次在途请求；不匹配的陈旧回执忽略
  if (m.status === 200 && body.request_id && body.request_id !== sent.request_id) return;
  state.submit = null;
  if (m.status === 200 && body.conversation_id) {
    state.conversationId = body.conversation_id;
    state.pendingRetry = null;
    // F06：回执只消费本次受理的内容——发送期间新写的草稿/新换的附件保留
    if ($('command').value.trim() === sent.text) {
      state.drafts.chat = '';
      $('command').value = '';
    }
    if (!state.attachment || state.attachment.path === sent.attachmentPath) {
      state.attachment = null;
    }
    renderAttachment();
    updateSubmitEnabled();
    if (state.drafts.chat && state.drafts.chat !== sent.text)
      toast('任务已发起；输入框里的新内容已保留');
    else
      toast('任务已发起');
  } else if (m.status === -1 && sent) {
    // 结果未知（连接失败/超时，受理与否不确定）：保留草稿并冻结完整提交快照，
    // 重发原快照（同 request_id 同载荷），服务端幂等返回原受理（R09/F05）
    state.pendingRetry = {request_id: sent.request_id, text: sent.text, payload: sent.payload};
    updateSubmitEnabled();
    toast('发送结果未知：内容已保留，直接重发不会重复执行');
  } else {
    // 明确失败：未受理，草稿保留可继续编辑；原 request_id 作废
    state.pendingRetry = null;
    updateSubmitEnabled();
    const err = body.error || {};
    toast(`发起失败：${err.code || 'HTTP ' + m.status}${err.message ? '（' + String(err.message).slice(0, 90) + '）' : ''}`);
  }
  render();
}
function onHubActionResult(m) {
  toast(m.status === 200 ? '已处理' : `操作失败（HTTP ${m.status}）`);
}
// ---------- 现场材料确认条（M3：发起时随任务携带，可显式移除） ----------
function sceneMaterials() {
  const p = state.sceneProbe;
  return p && Array.isArray(p.materials) ? p.materials.filter((x) => x && typeof x === 'object') : [];
}
function compactScene() {
  const p = state.sceneProbe || {};
  return {
    scene_id: state.sceneId || '', generation: state.sceneGen || 0,
    scenario: p.scenario || '', coverage: p.coverage || '', note: p.note || '',
    identity: p.identity || null,
    materials: sceneMaterials().map((x) => ({ kind: x.kind, source: x.source || '', chars: x.chars || 0, text: x.text || '' })),
  };
}
function renderSceneStrip() {
  const visible = state.mode === 'chat' && state.sceneAttached && sceneMaterials().length > 0;
  const el = $('scene-strip');
  el.hidden = !visible;
  if (!visible) return;
  const chips = sceneMaterials()
    .map((x) => `${SCENE_KIND_LABELS[x.kind] || x.kind} ${x.chars || 0}字`)
    .join(' · ');
  el.innerHTML = `<span class="attachment-chip">◆ 现场${chips ? `：${esc(chips)}` : ''}<button id="remove-scene" aria-label="本次不附带现场材料">×</button></span><span>将随任务附带 · 演示链路</span>`;
}
// ---------- DSH（真实：任务中枢事件流） ----------
function renderChat() {
  if (state.peek) { renderDetail(); return; }
  const panel = $('content');
  const previousScroll = panel.scrollTop;
  const pinnedToBottom = panel.scrollHeight-panel.clientHeight-previousScroll < 45;
  if (state.hub.connected === false) {
    panel.innerHTML = `<div class="chat-welcome"><h2 class="welcome-heading">任务中枢未连接</h2><p class="welcome-sub">${esc(state.hub.reason || 'server.py 未运行，或副驾未启用。')}<br>启动数据服务（server.py）后，这里就是真实的 DSH 对话。</p></div>`;
  } else if (!hubData()) {
    panel.innerHTML = `<div class="chat-welcome"><h2 class="welcome-heading">正在连接任务中枢…</h2></div>`;
  } else if (!hubData().available) {
    panel.innerHTML = `<div class="chat-welcome"><h2 class="welcome-heading">副驾未启用</h2><p class="welcome-sub">${esc(hubData().reason || '检查 copilot 配置。')}</p></div>`;
  } else {
    const c = currentConv();
    if (!c || !(Array.isArray(c.events) && c.events.length)) {
      panel.innerHTML = `<div class="chat-welcome"><h2 class="welcome-heading">${c ? esc(c.title || '会话') : '新会话'}</h2><p class="welcome-sub">把任务交给 DSH——现场材料会自动附带。</p><div class="suggestions"><button class="suggestion" data-prompt="总结当前现场，列出下一步建议">总结现场 <span>↗</span></button><button class="suggestion" data-prompt="根据现场材料继续这个任务">继续现场任务 <span>↗</span></button></div></div>`;
    } else {
      panel.innerHTML = `<div class="chat-log" aria-label="DSH 对话">${renderEvents(c)}</div>`;
    }
  }
  if (pinnedToBottom) panel.scrollTop = panel.scrollHeight; else panel.scrollTop = previousScroll;
  requestAnimationFrame(fitBody);
}
function msgShell(role, inner, time) {
  const label = role === 'user' ? '你' : '<span class="assistant-mark">✦</span> DSH';
  return `<div class="message ${role}"><div class="message-label">${label}${time ? `<time>${esc(time)}</time>` : ''}</div><div class="message-text">${inner}</div></div>`;
}
function escMd(text) {
  const NL = String.fromCharCode(10);
  const paras = String(text || '').split(NL + NL + '+');
  return paras.map((p) => '<p>' + esc(p).split(NL).join('<br>') + '</p>').join('');
}
function renderEvent(ev) {
  const p = (ev && ev.payload) || {};
  if (ev.kind === 'user' || ev.kind === 'user_echo') {
    return msgShell('user', escMd(p.text || ''));
  }
  if (ev.kind === 'assistant') {
    return msgShell('assistant', escMd(p.text || p.content || ''));
  }
  if (ev.kind === 'tool') {
    const name = p.title || p.name || p.tool || '工具调用';
    const status = p.status ? ' <small style="color:#7f97a4">· ' + esc(p.status) + '</small>' : '';
    return msgShell('assistant', '<p>⚙ ' + esc(name) + status + '</p>');
  }
  if (ev.kind === 'permission_request') {
    const opts = Array.isArray(p.options) ? p.options : [];
    return '<div class="message assistant"><div class="message-label"><span class="assistant-mark">✦</span> DSH · 请求许可</div><div class="message-text"><p>' + esc(p.tool_title || p.tool || '操作确认') + '</p><div class="permission-actions" style="margin-top:8px">' + opts.map((o) => '<button data-permission="' + esc(p.permission_id || ev.seq) + '" data-option="' + esc(o.option_id) + '">' + esc(o.name) + '</button>').join('') + '</div></div></div>';
  }
  if (ev.kind === 'note' && p.text) {
    return msgShell('assistant', '<p style="color:#8aa0ad">' + esc(p.text) + '</p>');
  }
  return '';
}
const FEEDBACK_LABELS = { accepted: '✓ 可用', modified: '✎ 改后用', rejected: '✕ 不合用', needs_review: '？ 待核' };
function feedbackRow(runId, current) {
  const btns = Object.entries(FEEDBACK_LABELS).map(([k, label]) =>
    '<button class="reply-action" data-feedback="' + k + '" data-run="' + esc(runId) + '"' + (current === k ? ' disabled' : '') + '>' + label + '</button>').join(' ');
  return '<div class="message assistant"><div class="message-label"><span class="assistant-mark">✦</span> 结果反馈' + (current ? ' · ' + esc(FEEDBACK_LABELS[current] || current) : '') + '</div><div class="permission-actions">' + btns + '</div></div>';
}
function renderEvents(c) {
  const out = [];
  for (const ev of (c.events || [])) out.push(renderEvent(ev));
  const active = hubActiveRun();
  if (active && active.conversation_id === c.conversation_id && !['SETTLED','LIMITED','CANCELLED','FAILED','INTERRUPTED'].includes(active.state)) {
    out.push('<div class="message assistant"><div class="message-label"><span class="assistant-mark">✦</span> DSH</div><div class="thinking"><span class="thinking-dot"></span>' + esc(active.state_label || active.state) + '<button class="stop-button" data-action="stop">停止</button></div></div>');
  } else {
    // 末条助手消息后挂四态反馈（三期）：run_id 取末条 assistant 事件的 run_id
    const evs = c.events || [];
    for (let i = evs.length - 1; i >= 0; i--) {
      if (evs[i].kind === 'assistant' && evs[i].run_id) {
        out.push(feedbackRow(evs[i].run_id, c.feedback && c.feedback.run_id === evs[i].run_id ? c.feedback.verdict : null));
        break;
      }
    }
  }
  return out.join('');
}
function submitTask() {
  const text = $('command').value.trim();
  if (!text || state.composing || state.hub.connected === false) return;
  if (state.submit) return; // 在途：不并发第二条（可靠防重在服务端 request_id 幂等，R09）
  const attachment = state.attachment;
  const currentRef = attachment && attachment.path ? attachment.path : null;
  // F05：结果未知(-1)后的重发必须重发**原快照**（冻结载荷）——文本与附件都
  // 未变才复用；现场/会话变化不进重试载荷。改过内容即新任务、新 request_id
  const retry = state.pendingRetry
    && state.pendingRetry.text === text
    && (state.pendingRetry.payload.context_refs ? state.pendingRetry.payload.context_refs[0] || null : null) === currentRef
    ? state.pendingRetry : null;
  const payload = retry ? retry.payload : {
    request_id: newRequestId(),
    text,
    conversation_id: state.conversationId || null,
    context_refs: attachment && attachment.path ? [attachment.path] : null,
    scene: state.sceneAttached && sceneMaterials().length ? compactScene() : null,
  };
  state.pendingRetry = null;
  state.submit = {
    request_id: payload.request_id,
    text: payload.text,
    attachmentPath: payload.context_refs && payload.context_refs[0] || null,
    payload, // 完整提交快照（含 scene/conversation_id），重试时原样重发
  };
  post({type: 'submit-task', ...payload});
  // B02：草稿与附件保留到受理回执（onHubSubmitResult）；明确失败可继续编辑
  $('submit').disabled = true;
}
function stopTask() {
  const run = hubActiveRun();
  if (run) post({ type: 'hub-cancel', run_id: run.run_id });
}
// ---------- 任务一览（M5：任务中枢真实状态） ----------
const TASK_STATUS_LABELS = { running: '运行中', needs_input: '待确认', ready: '已完成', blocked: '异常', unknown: '未知' };
function renderActivity() {
  const stats = hubStats();
  const running = (stats.running || 0) + (stats.needs_input || 0);
  const perms = hubPerms();
  const offline = state.hub.connected === false;
  $('activity-summary').textContent = offline ? '中枢未连接' : String(running) + ' 项进行中';
  $('permission-summary').textContent = offline ? '—' : (perms.length ? perms.length + ' 项待确认' : '无待确认');
  $('permission-summary').classList.toggle('amber', !offline && perms.length > 0);
  $('activity-panel').hidden = !state.activity;
  $('activity-button').setAttribute('aria-expanded', String(state.activity));
  if (!state.activity) return;
  const tasks = hubTasks();
  const rows = tasks.length ? tasks.map((t) => '<div class="task-row"><span class="live-dot" style="background:' + (t.status === 'blocked' ? '#e0a784' : t.status === 'needs_input' ? '#e8c28b' : t.status === 'ready' ? '#9fd3c4' : '#99cdbb') + '"></span><span class="task-name">' + esc(t.title || t.ref_id) + '</span><span class="task-tool">' + esc(t.agent || '') + '</span><span class="task-status">' + (TASK_STATUS_LABELS[t.status] || t.status) + (t.unread ? ' · 未读' : '') + '</span></div>').join('') : '<div class="task-row"><span class="task-name">暂无任务</span></div>';
  const cards = perms.map((p) => '<div class="permission-card"><div class="permission-heading">需要你确认 · ' + esc(p.tool_title || 'DSH') + '</div><div class="permission-desc">' + esc(p.tool_kind || '') + '<br>许可 ' + esc(String(p.permission_id || '').slice(0, 10)) + '…</div><div class="permission-actions">' + (p.options || []).map((o) => '<button data-permission="' + esc(p.permission_id) + '" data-option="' + esc(o.option_id) + '">' + esc(o.name) + '</button>').join('') + '</div></div>').join('');
  $('activity-panel').innerHTML = '<div class="activity-title">任务一览 <span style="float:right;color:#597789">' + (offline ? '未连接' : '实时 · 任务中枢') + '</span></div>' + rows + cards;
}
// ---------- 今日速览（真数据优先：server /copilot/hud 载荷 today 段，滴答官方 MCP） ----------
function parseDidaTs(s) {
  // "2026-10-06T15:00:00+0800" → Date（补冒号成 RFC 时区）
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?([+-]\d{2})(\d{2})$/.exec(String(s || ''));
  if (!m) return null;
  const t = new Date(`${m[1]}T${m[2]}:${m[3]}:${m[4] || '00'}${m[5]}:${m[6]}`);
  return isNaN(t) ? null : t;
}
function fmtClock(d) { return d ? `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` : ''; }
function todayData() {
  const t = hubData() && hubData().today;
  if (t && Array.isArray(t.tasks)) {
    const now = new Date();
    const todayStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    const tmr = new Date(now.getTime() + 86400000);
    const tomorrowStr = `${tmr.getFullYear()}-${String(tmr.getMonth() + 1).padStart(2, '0')}-${String(tmr.getDate()).padStart(2, '0')}`;
    return { real: true, scenario: 'connected',
      tasks: t.tasks.map((x) => {
        const start = parseDidaTs(x.start);
        const dateStr = start ? `${start.getFullYear()}-${String(start.getMonth() + 1).padStart(2, '0')}-${String(start.getDate()).padStart(2, '0')}` : '';
        const dueLabel = !x.all_day ? (fmtClock(start) || '今天')
          : !dateStr ? '无日期'
          : dateStr === todayStr ? '今天'
          : dateStr === tomorrowStr ? '明天'
          : dateStr < todayStr ? '已逾期'
          : `${start.getMonth() + 1}月${start.getDate()}日`;
        return { id: x.id, projectId: x.project_id, title: x.title, list: x.list || 'Inbox',
          due: dueLabel,
          overdue: Boolean(dateStr && dateStr < todayStr),
          done: false, pending: state.todayPendingId === x.id };
      }),
      events: (Array.isArray(t.events) ? t.events : []).map((e) => ({
        startTs: (parseDidaTs(e.start) || new Date(0)).getTime(),
        endTs: (parseDidaTs(e.due) || parseDidaTs(e.start) || new Date(0)).getTime(),
        start: fmtClock(parseDidaTs(e.start)), end: fmtClock(parseDidaTs(e.due)),
        title: e.title, location: e.list || '',
      })).sort((a, b) => a.startTs - b.startTs) };
  }
  return { ...today, real: false };
}
function eventCountdown(ev) {
  // 未结束最近事件的倒计时文案：>60min 给时刻，否则 N 分钟后/进行中
  const mins = Math.round((ev.startTs - Date.now()) / 60000);
  if (mins > 60) return ev.start;
  if (mins > 0) return `${mins} 分钟后`;
  return Date.now() <= ev.endTs ? '进行中' : '';
}
function sourceAvailable(d) { d = d || todayData(); return ['connected','write-error'].includes(d.scenario); }
function activeTodos(d) { d = d || todayData(); return d.tasks.filter(t=>!t.done); }
function todoControl(task,compact=false) {
  return `<button class="todo-toggle ${task.done?'done':''} ${task.pending?'pending':''} ${compact?'compact':''}" role="checkbox" aria-checked="${task.done}" aria-label="${task.done?'重新打开':'完成'}：${esc(task.title)}" data-todo="${task.id}" ${task.pending?'disabled aria-busy="true"':''}><span class="todo-box" aria-hidden="true">${task.done?'✓':''}</span>${compact?`<span>${esc(task.title)}</span>`:''}</button>`;
}
function renderGlance() {
  const d = todayData();
  if (!sourceAvailable(d)) {
    const empty=d.scenario==='empty';
    const text=empty?'今天没有安排':d.scenario==='disconnected'?'未连接滴答清单':'滴答暂时无法同步';
    $('content').innerHTML=`<section class="glance glance-unavailable" aria-label="今日速览"><span class="glance-label">今日</span><button data-peek="today" class="source-summary">${icons.calendar}<span>${text}</span><small>${empty?'查看详情':'点击查看状态'}</small>${icons.arrow}</button></section>`;
    return;
  }
  const remaining=activeTodos(d),shown=remaining.slice(0,2),done=d.tasks.filter(t=>t.done).length;
  // 下一日程：未结束的第一个（真数据）或标记 next（演示数据）
  const nowTs=Date.now();
  const nextEv=d.events.find(e=>d.real?(e.endTs||e.startTs)>=nowTs-60000:e.next)||null;
  const cd=nextEv&&d.real?eventCountdown(nextEv):(nextEv?'25 分钟后':'');
  const evRow=nextEv?`<div class="glance-row"><button class="glance-label" data-peek="today" aria-label="展开今日日程与待办">今日</button><button class="next-event" data-peek="today" aria-label="查看今日日程：${esc(nextEv.start)} ${esc(nextEv.title)}">${icons.calendar}<time>${esc(nextEv.start)}</time><span class="next-title">${esc(nextEv.title)}</span>${cd?`<span class="event-countdown">${esc(cd)}</span>`:''}<span class="event-arrow">${icons.arrow}</span></button></div>`
    :`<div class="glance-row"><button class="glance-label" data-peek="today" aria-label="展开今日日程与待办">今日</button><button class="next-event" data-peek="today" aria-label="查看今日日程">${icons.calendar}<span class="next-title">未来 24 小时没有日程</span><span class="event-arrow">${icons.arrow}</span></button></div>`;
  $('content').innerHTML=`<section class="glance" aria-label="今日速览">${evRow}<div class="glance-row todo-glance-row"><span class="glance-label subtle">待办</span><div class="todo-chips">${shown.map((t,i)=>`${todoControl(t,true)}${t.overdue?'<span class="overdue-dot" title="昨天到期" aria-label="逾期">逾期</span>':''}${i<shown.length-1?'<span class="todo-divider"></span>':''}`).join('')}${!remaining.length?'<span class="all-clear">✓ 今天的待办都完成了</span>':''}</div><button class="more-todos" data-peek="today" aria-label="查看全部待办，剩余 ${remaining.length} 项">${remaining.length>2?`+${remaining.length-2}`:done?`已完成 ${done}`:'查看全部'} ${icons.arrow}</button></div></section>`;
}
function sourceLabel(d) {
  if ((d || todayData()).real) return '滴答清单 · 实时';
  const sc=(d||todayData()).scenario;
  return ({connected:'滴答 · 演示数据','write-error':'滴答 · 演示数据',disconnected:'未连接滴答',offline:'同步失败',empty:'滴答 · 演示数据'})[sc];
}
function detailTop(title,meta) {
  return `<div class="detail-top"><button class="back-button" data-action="close-detail">${icons.back} 返回</button><span>${title}</span><small>${meta}</small></div>`;
}
function renderDetail() {
  $('command').removeAttribute('aria-activedescendant');
  if(state.peek==='system'){renderSystemDetail();return;}
  const d=todayData();
  const now=new Date();
  const dateMeta=`${now.getMonth()+1} 月 ${now.getDate()} 日`+(d.real?'':' · 演示时钟 14:05');
  if(!sourceAvailable(d)&&d.scenario!=='empty') {
    const disconnected=d.scenario==='disconnected';
    $('content').innerHTML=`<section class="detail-panel">${detailTop('今日',dateMeta)}<div class="source-empty">${icons.calendar}<h2>${disconnected?'还未连接滴答清单':'暂时无法同步'}</h2><p>${disconnected?'连接后，可以在这里查看日程和待办。':'目前无法确认最新的日程和待办，请稍后重试。'}<br>演示场景，不发起真实授权。</p></div></section>`;
    return;
  }
  const isEmpty=d.scenario==='empty';
  const remaining=isEmpty?[]:activeTodos(d),completed=isEmpty?[]:d.tasks.filter(t=>t.done),events=isEmpty?[]:d.events;
  const nowTs=Date.now();
  const nextIdx=d.real?events.findIndex(x=>(x.endTs||x.startTs)>=nowTs-60000):events.findIndex(e=>e.next);
  $('content').innerHTML=`<section class="detail-panel today-detail">${detailTop('今日',dateMeta)}<div class="detail-section-heading"><span>日程</span><small>${events.length} 项安排</small></div>${events.length?`<div class="agenda-list">${events.map((e,i)=>{const isNext=i===nextIdx;return `<div class="agenda-row ${isNext?'up-next':''}"><div class="agenda-time"><time>${esc(e.start)}</time><small>${esc(e.end)}</small></div><div class="agenda-marker"></div><div class="agenda-copy"><strong>${esc(e.title)}</strong><small>${esc(e.location)}</small></div>${isNext?`<span class="agenda-soon">${esc(d.real?eventCountdown(e)||'进行中':'25 分钟后')}</span>`:''}</div>`;}).join('')}</div>`:'<p class="section-empty">未来 24 小时没有定时安排。</p>'}<div class="detail-section-heading todo-section-heading"><span>待办</span><small>${remaining.length} 项未完成${completed.length?` · ${completed.length} 项已完成`:''}</small></div><div class="todo-detail-list">${remaining.map(t=>renderTodoRow(t)).join('')}${!remaining.length?`<p class="section-empty">${completed.length?'✓ 今天的待办都完成了。':'今天没有待办。'}</p>`:''}${completed.length?`<details class="completed-todos"><summary>已完成 ${completed.length} 项</summary>${completed.map(t=>renderTodoRow(t)).join('')}</details>`:''}</div><div class="detail-source"><span class="source-dot"></span>${sourceLabel(d)}<span>仅日期截止的任务保留在待办中</span></div></section>`;
}
function renderTodoRow(t) {
  return `<div class="todo-detail-row ${t.done?'is-done':''}">${todoControl(t)}<div class="todo-copy"><span>${esc(t.title)}</span><small>${esc(t.list)}</small></div><span class="todo-due ${t.overdue&&!t.done?'overdue':''}">${t.pending?'正在提交…':t.done?'已完成':esc(t.due)}</span></div>`;
}
function openDetail(view) {
  state.peek=state.peek===view?null:view;
  state.picker=false;state.activity=false;
  render();
  $('content').scrollTop=0;
  if(state.peek)requestAnimationFrame(()=>$('content').querySelector('.back-button')?.focus({preventScroll:true}));
  else $('command').focus();
}
function toggleTodo(id) {
  const d=todayData();
  const task=d.tasks.find(t=>t.id===id);
  if(!task||task.pending||!sourceAvailable(d))return;
  if(d.real){
    // 真数据：写回滴答（complete_task）；结果经 today-complete-result 与下次 hub-update 到达
    state.todayPendingId=id;
    render();
    post({type:'today-complete',task_id:task.id,project_id:task.projectId});
    return;
  }
  const scenarioAtSubmit=today.scenario,previous=task.done;
  task.pending=true;
  render();
  if(document.activeElement===document.body)$('command').focus({preventScroll:true});
  setTimeout(()=>{
    task.pending=false;
    if(scenarioAtSubmit==='write-error'){
      render();toast(`未能${previous?'重新打开':'完成'}「${task.title}」，状态已保留。`);return;
    }
    task.done=!previous;
    render();
    toast(`${previous?'已重新打开':'已完成'}「${task.title}」`,()=>{task.done=previous;render();toast('已撤销本次演示操作。');});
  },700);
}
function onTodayCompleteResult(m) {
  state.todayPendingId=null;
  if(m&&m.status===200){render();toast('已完成，滴答清单已同步');return;}
  let msg='';
  try{msg=(JSON.parse(String(m&&m.json)).error||{}).message||'';}catch(e){}
  render();
  toast(`未能完成：${msg||('HTTP '+(m&&m.status))}`);
}
// ---------- 系统速览（M5：server.py 实时采样） ----------
function sysMetrics() {
  const d = hubData();
  return d && d.system && typeof d.system === 'object' ? d.system : null;
}
function renderSystemButton() {
  const m = sysMetrics();
  $('system-button').setAttribute('aria-expanded', String(state.peek === 'system'));
  if (!m) {
    $('system-button').innerHTML = icons.pulse + '<span>系统 · 未连接</span>';
    $('system-button').classList.remove('load-high');
    $('system-button').setAttribute('aria-label', '系统指标未连接，查看详情');
    return;
  }
  const busy = Number(m.cpu || 0) > 85 || Number(m.memory || 0) > 85;
  $('system-button').innerHTML = icons.pulse + '<span>CPU <b>' + Math.round(Number(m.cpu || 0)) + '%</b></span><span class="metric-dot">·</span><span>内存 <b>' + Math.round(Number(m.memory || 0)) + '%</b></span>';
  $('system-button').classList.toggle('load-high', busy);
  $('system-button').setAttribute('aria-label', '系统速览：CPU ' + Math.round(Number(m.cpu || 0)) + '%，内存 ' + Math.round(Number(m.memory || 0)) + '%');
}
function renderSystemDetail() {
  const m = sysMetrics();
  if (!m) {
    $('content').innerHTML = '<section class="detail-panel">' + detailTop('系统速览', '实时') + '<div class="source-empty">' + icons.pulse + '<h2>暂无系统采样</h2><p>任务中枢（server.py）未连接。</p></div></section>';
    return;
  }
  const metrics = [
    ['CPU', Math.round(Number(m.cpu || 0)), '%', '实时采样'],
    ['内存', Math.round(Number(m.memory || 0)), '%', String(m.memory_gb || '')],
  ];
  if (m.gpu !== undefined) metrics.push(['GPU', Math.round(Number(m.gpu || 0)), '%', '实时采样']);
  const busy = Number(m.cpu || 0) > 85 || Number(m.memory || 0) > 85;
  $('content').innerHTML = '<section class="detail-panel system-detail">' + detailTop('系统速览', '实时') + '<div class="system-overview ' + (busy ? 'load-high' : '') + '"><span class="source-dot"></span><span>' + (busy ? '当前负载较高' : '运行平稳') + '</span></div><div class="metric-grid">' + metrics.map(([name, value, unit, note]) => '<div class="metric-cell"><span>' + name + '</span><div class="metric-value">' + value + '<small>' + unit + '</small></div><div class="metric-track"><i class="' + (value > 85 ? 'warm' : '') + '" style="width:' + value + '%"></i></div><small>' + esc(note) + '</small></div>').join('') + '</div><p class="system-note">来自 server.py 实时采样（任务中枢通道）。</p></section>';
}
// ---------- 事件 ----------
$('system-button').addEventListener('click',()=>openDetail('system'));
$('today-scenario').addEventListener('change',(e)=>{ today.scenario=e.target.value; render(); });
$('dev-button').addEventListener('click',()=>{ $('dev-panel').hidden=!$('dev-panel').hidden; });
$('exit-button').addEventListener('click',()=>{ post({type:'exit'}); });
$('toast').addEventListener('click',(e)=>{
  if(e.target.closest('#undo-action')&&undoAction){const action=undoAction;undoAction=null;clearTimeout(toastTimer);action();}
});
$('submit').innerHTML=icons.send;
$('files-mode').addEventListener('click',()=>switchMode('files'));
$('chat-mode').addEventListener('click',()=>switchMode('chat'));
$('submit').addEventListener('click',submitTask);
$('command').addEventListener('compositionstart',()=>{state.composing=true;});
$('command').addEventListener('compositionend',()=>{state.composing=false;});
$('command').addEventListener('input',()=>{
  state.drafts[state.mode]=$('command').value;
  const wasDetail=Boolean(state.peek);
  state.peek=null;
  if(state.mode==='files'){
    state.detailFile=null;state.selected=0;state.activity=false;
    issueSearch(); // 内部含防抖与空查询恢复速览
  }
  else if(wasDetail)render();
  else updateSubmitEnabled();
});
$('command').addEventListener('keydown',(e)=>{
  // 中文组词期间不拦截任何功能键：候选确认不得视为发送/打开/收起
  if(e.isComposing||state.composing||e.keyCode===229)return;
  if(e.key==='Tab'&&!e.ctrlKey&&!e.altKey&&!e.metaKey){e.preventDefault();switchMode(state.mode==='files'?'chat':'files');}
  else if(e.key==='ArrowDown'&&state.mode==='files'&&!state.detailFile){e.preventDefault();selectFile(state.selected+1);}
  else if(e.key==='ArrowUp'&&state.mode==='files'&&!state.detailFile){e.preventDefault();selectFile(state.selected-1);}
  else if(e.key==='ArrowRight'&&state.mode==='files'&&!state.detailFile&&resultsActionable()&&state.search.items[state.selected]){e.preventDefault();openFileDetail(state.selected);}
  else if(e.key==='ArrowLeft'&&state.mode==='files'&&state.detailFile){e.preventDefault();state.detailFile=null;renderFiles();}
  else if(e.key==='Enter'){
    e.preventDefault();
    if(state.mode==='chat'){submitTask();return;}
    if(state.detailFile){fileAction(e.ctrlKey?'reveal':'open',state.detailFile.id);return;}
    if(!resultsActionable())return; // 新查询在途：旧结果不可打开
    const item=state.search.items[state.selected];
    if(state.drafts.files.trim()&&item)fileAction(e.ctrlKey?'reveal':'open',item.id);
  }
});
$('content').addEventListener('click',(e)=>{
  const fb=e.target.closest('[data-feedback]');
  if(fb){post({type:'hub-feedback',run_id:fb.dataset.run,verdict:fb.dataset.feedback});toast('反馈已提交：'+(FEEDBACK_LABELS[fb.dataset.feedback]||fb.dataset.feedback));return;}
  const detail=e.target.closest('[data-detail]');
  if(detail){e.stopPropagation();openFileDetail(Number(detail.dataset.detail));return;}
  const peek=e.target.closest('[data-peek]');if(peek){openDetail(peek.dataset.peek);return;}
  const todo=e.target.closest('[data-todo]');if(todo){toggleTodo(todo.dataset.todo);return;}
  const prompt=e.target.closest('[data-prompt]');if(prompt){state.drafts.chat=prompt.dataset.prompt;$('command').value=state.drafts.chat;updateSubmitEnabled();$('command').focus();return;}
  const row=e.target.closest('.result-row[data-idx]');
  if(row){const i=Number(row.dataset.idx);if(i===state.selected)openFileDetail(i);else{state.selected=i;renderFiles();}return;}
  const actionEl=e.target.closest('[data-action]');
  const action=actionEl?.dataset.action;
  if(action==='back'){state.detailFile=null;renderFiles();$('command').focus();}
  if(action==='open'||action==='reveal'){fileAction(action,actionEl.dataset.id);}
  if(action==='handoff'){
    const f=state.detailFile||state.search.items[state.selected];
    if(f){switchMode('chat');state.attachment={name:f.name,path:f.path};state.drafts.chat=`基于这个文件继续：${f.name}`;$('command').value=state.drafts.chat;render();$('command').focus();}
  }
  if(action==='stop')stopTask();
  if(action==='close-detail'){state.peek=null;render();$('command').focus();}
});
$('project-button').addEventListener('click',()=>{
  state.picker=!state.picker;state.activity=false;state.peek=null;render();
  if(state.picker)$('project-picker').querySelector('.current')?.focus();
});
$('project-picker').addEventListener('click',(e)=>{
  const b=e.target.closest('[data-conversation]');if(!b)return;
  state.conversationId=b.dataset.conversation||null; // 空串=新会话
  state.picker=false;render();$('command').focus();
});
$('project-picker').addEventListener('keydown',(e)=>{
  if(!['ArrowDown','ArrowUp'].includes(e.key))return;
  const options=[...$('project-picker').querySelectorAll('button')];
  const index=options.indexOf(document.activeElement);e.preventDefault();
  options[(index+(e.key==='ArrowDown'?1:-1)+options.length)%options.length].focus();
});
$('new-chat').addEventListener('click',()=>{
  state.peek=null;state.picker=false;state.activity=false;
  state.conversationId=null;state.attachment=null;state.drafts.chat='';$('command').value='';
  render();$('command').focus();
});
$('attachment-strip').addEventListener('click',(e)=>{if(e.target.closest('#remove-attachment')){state.attachment=null;renderAttachment();}});
$('scene-strip').addEventListener('click',(e)=>{if(e.target.closest('#remove-scene')){state.sceneAttached=false;renderSceneStrip();}});
$('activity-button').addEventListener('click',()=>{state.activity=!state.activity;state.picker=false;state.peek=null;$('dev-panel').hidden=true;render();});
$('activity-panel').addEventListener('click',(e)=>{
  const decision=e.target.closest('[data-permission]');
  if(decision){post({type:'hub-permission',permission_id:decision.dataset.permission,option_id:decision.dataset.option});toast('已提交确认…');$('activity-button').focus();}
});
document.addEventListener('keydown',(e)=>{
  if(e.isComposing||state.composing||e.keyCode===229)return;
  if(e.key==='Escape'){
    e.preventDefault();
    if(!state.visible)return;
    if(!$('dev-panel').hidden){$('dev-panel').hidden=true;return;}
    if(state.picker){state.picker=false;render();$('project-button').focus();}
    else if(state.activity){state.activity=false;render();$('activity-button').focus();}
    else if(state.peek){const oldPeek=state.peek;state.peek=null;render();if(oldPeek==='system')$('system-button').focus();else $('command').focus();}
    else if(state.detailFile){state.detailFile=null;renderFiles();$('command').focus();}
    else requestHide();
  }
});
// 点击面板外区域收起。IME 候选窗点击不会到达页面，不会误触发。
document.addEventListener('pointerdown',(e)=>{
  if(!state.visible)return;
  if(!e.target.closest('#hud, #toast'))requestHide();
});
// ---------- 宿主消息 ----------
if (bridge) {
  bridge.addEventListener('message',(e)=>{
    const m=e.data;
    if(!m||typeof m!=='object')return;
    if(m.type==='visibility')onHostVisibility(Boolean(m.visible));
    else if(m.type==='bounds'){const h=Number(m.maxPanelHeight);if(h>0)document.documentElement.style.setProperty('--panel-max-h',h+'px');}
    else if(m.type==='search-result')onSearchResult(m);
    else if(m.type==='action-result')onActionResult(m);
    else if(m.type==='scene-update')onHostScene(m);
    else if(m.type==='scene-round')onHostSceneRound(m);
    else if(m.type==='scene-probe')onHostProbe(m);
    else if(m.type==='scene-capture')onHostCapture(m);
    else if(m.type==='scene-clipboard')onHostClipboard(m);
    else if(m.type==='hub-update')onHubUpdate(m);
    else if(m.type==='hub-unavailable')onHubUnavailable(m);
    else if(m.type==='hub-submit-result')onHubSubmitResult(m);
    else if(m.type==='hub-action-result')onHubActionResult(m);
    else if(m.type==='today-complete-result')onTodayCompleteResult(m);
    else if(m.type==='hotkey-error')toast(String(m.message||'快捷键注册失败'));
  });
} else {
  toast('未检测到原生宿主桥接（请在 NativeHud 中打开）');
}
// ---------- 启动 ----------
state.dev=new URLSearchParams(location.search).has('dev');
$('dev-button').hidden=!state.dev;
render();
post({type:'ready'});
