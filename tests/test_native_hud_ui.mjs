// app.js 搜索状态机跨层回归（F01/F03/F04）：最小 DOM 桩 + 假桥接，node --test 运行。
// 覆盖：新查询在途时 Enter 不发动作、返回文件模式重发查询、详情随新响应重解析标识。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', hidden: false, disabled: false,
    inert: false, style: {}, scrollTop: 0, scrollHeight: 0, offsetHeight: 100, offsetWidth: 730,
    offsetLeft: 0, offsetTop: 0,
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, toggle(c, v) { v ? this._s.add(c) : this._s.delete(c); }, contains(c) { return this._s.has(c); } },
    listeners: {},
    addEventListener(t, fn) { (this.listeners[t] ??= []).push(fn); },
    dispatch(t, ev) { (this.listeners[t] ?? []).forEach(fn => fn(ev)); },
    setAttribute() {}, removeAttribute() {}, focus() {}, click() {},
    querySelector() { return null; }, querySelectorAll() { return []; }, closest() { return null; },
  };
}

function loadApp() {
  const els = {};
  const posted = [];
  const doc = makeEl('#document');
  doc.getElementById = (id) => (els[id] ??= makeEl(id));
  doc.addEventListener = () => {};
  doc.querySelector = () => null;
  const rafCallbacks = [];
  const sandbox = {
    document: doc,
    window: {
      listeners: {},
      addEventListener(t, fn) { (this.listeners[t] ??= []).push(fn); },
      dispatch(t, ev) { (this.listeners[t] ?? []).forEach(fn => fn(ev ?? {})); },
      chrome: { webview: { postMessage: (m) => posted.push(m), addEventListener: () => {} } },
    },
    location: { search: '' },
    navigator: { userAgent: 'probe' },
    ResizeObserver: class { observe() {} },
    requestAnimationFrame: (fn) => rafCallbacks.push(fn),
    setTimeout, clearTimeout, URLSearchParams, console,
  };
  sandbox.globalThis = sandbox;
  const src = readFileSync(new URL('../native_hud/ui/app.js', import.meta.url), 'utf8');
  const fn = new Function(...Object.keys(sandbox), src + `
    return { state, issueSearch, onSearchResult, switchMode, resultsActionable, renderFiles, fileAction, onHostVisibility, onHostScene, onHostSceneRound, onHostProbe, onHostCapture, onHostClipboard, onHubUpdate, onHubUnavailable, onHubSubmitResult, submitTask, switchMode, reportLayout, renderActivity, state, FEEDBACK_LABELS, toggleTodo, onTodayCompleteResult, todayData };`);
  const app = fn(...Object.values(sandbox));
  const input = doc.getElementById('command');
  const flushRaf = () => { const cbs = rafCallbacks.splice(0); cbs.forEach(cb => cb()); };
  return { app, els, posted, input, flushRaf, win: sandbox.window };
}

function type(app, els, text) {
  const input = els['command'];
  input.value = text;
  input.dispatch('input', { bubbles: true });
}

function respond(app, requestId, paths) {
  app.onSearchResult({
    type: 'search-result', requestId, status: 'ok', total: paths.length,
    items: paths.map((p, i) => ({ id: `r${requestId}_${i}`, name: p.split('\\\\').pop(), path: p, kind: 'file', sizeBytes: 1, modifiedAt: '' })),
    error: null, engine: 'stub',
  });
}

test('F01: 新查询在途时 Enter 不发 open 动作', async () => {
  const { app, els, posted, input } = loadApp();
  type(app, els, 'query-a');
  await new Promise(r => setTimeout(r, 250)); // 防抖后发出 req1
  respond(app, 1, ['D:\\\\t\\\\a.txt']);
  assert.equal(app.resultsActionable(), true);
  // 输入 B，响应未到：旧结果不得可动
  type(app, els, 'query-b');
  assert.equal(app.resultsActionable(), false);
  const before = posted.filter(m => m.type === 'open').length;
  input.dispatch('keydown', { key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
  assert.equal(posted.filter(m => m.type === 'open').length, before, '搜索中 Enter 不得发 open');
  // B 响应到达后可动
  await new Promise(r => setTimeout(r, 250));
  respond(app, 2, ['D:\\\\t\\\\b.txt']);
  assert.equal(app.resultsActionable(), true);
  input.dispatch('keydown', { key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
  const opens = posted.filter(m => m.type === 'open');
  assert.equal(opens.length, before + 1);
  assert.equal(opens.at(-1).id, 'r2_0', '必须打开 B 的标识');
});

test('F03: 切回文件模式重发查询', async () => {
  const { app, els, posted } = loadApp();
  type(app, els, 'keep-me');
  await new Promise(r => setTimeout(r, 250));
  respond(app, 1, ['D:\\\\t\\\\a.txt']);
  app.switchMode('chat');
  app.switchMode('files');
  await new Promise(r => setTimeout(r, 50));
  const searches = posted.filter(m => m.type === 'search');
  assert.equal(searches.at(-1).query, 'keep-me', '返回文件模式应重发当前查询');
  assert.ok(searches.at(-1).requestId > 1);
});

test('F04: 新响应到达后详情重解析标识', async () => {
  const { app, els, posted } = loadApp();
  type(app, els, 'x');
  await new Promise(r => setTimeout(r, 250));
  respond(app, 1, ['D:\\\\t\\\\a.txt']);
  app.state.detailFile = app.state.search.items[0]; // 打开详情
  // 恢复重搜：发出新代次后，新响应换标识
  app.issueSearch(true);
  respond(app, 2, ['D:\\\\t\\\\a.txt']);
  assert.equal(app.state.detailFile.id, 'r2_0', '详情应重解析为新代次标识');
  // 结果中消失则退回列表
  app.issueSearch(true);
  respond(app, 3, ['D:\\\\t\\\\other.txt']);
  assert.equal(app.state.detailFile, null);
});

test('F02配套: 隐藏与切模式发送 cancel-search', () => {
  const { app, els, posted } = loadApp();
  app.switchMode('chat');
  assert.ok(posted.some(m => m.type === 'cancel-search'), '切模式应通知宿主失效映射');
});

test('R2-03: 详情动作在等待阶段不得绕过代次守卫', async () => {
  const { app, els, posted, input } = loadApp();
  type(app, els, 'a');
  await new Promise(r => setTimeout(r, 250));
  respond(app, 1, ['D:\\\\t\\\\a.txt']);
  app.state.detailFile = app.state.search.items[0]; // 打开详情（r1_0）
  app.issueSearch(true);                            // 收起/呼出触发的重搜在途（gen=2，未响应）
  const before = posted.filter(m => m.type === 'open').length;
  app.fileAction('open', 'r1_0');                   // 详情按钮路径
  input.dispatch('keydown', { key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} }); // 详情 Enter
  assert.equal(posted.filter(m => m.type === 'open').length, before, '等待阶段详情动作必须被拦截');
  // 新响应重解析后可用新标识
  respond(app, 2, ['D:\\\\t\\\\a.txt']);
  app.fileAction('open', app.state.detailFile.id);
  const opens = posted.filter(m => m.type === 'open');
  assert.equal(opens.at(-1).id, 'r2_0');
});

test('R2-04: 清空查询立即通知宿主失效，迟到响应不复活', async () => {
  const { app, els, posted } = loadApp();
  type(app, els, 'a');
  await new Promise(r => setTimeout(r, 250));
  respond(app, 1, ['D:\\\\t\\\\a.txt']);
  const base = posted.filter(m => m.type === 'cancel-search').length;
  type(app, els, '');                                // 清空
  assert.ok(posted.filter(m => m.type === 'cancel-search').length > base, '清空应立即 cancel-search');
  app.onSearchResult({ type: 'search-result', requestId: 1, status: 'ok', total: 1,
    items: [{ id: 'r1_0', name: 'a.txt', path: 'D:\\\\t\\\\a.txt', kind: 'file', sizeBytes: 1 }], error: null });
  assert.equal(app.state.search.items.length, 0, '清空后旧代次响应不得恢复结果');
  assert.equal(app.resultsActionable(), false);
});

test('R2-04b: 隐藏时页面通知宿主失效', () => {
  const { app, posted } = loadApp();
  app.onHostVisibility(true);
  const base = posted.filter(m => m.type === 'cancel-search').length;
  app.onHostVisibility(false);
  assert.ok(posted.filter(m => m.type === 'cancel-search').length > base, '隐藏应 cancel-search');
});

test('R3-01: 视口 resize 与呼出恢复都主动上报面板几何', () => {
  const { posted, win, flushRaf } = loadApp();
  const layoutCount = () => posted.filter(m => m.type === 'layout').length;
  const before = layoutCount();
  win.dispatch('resize');          // 仅位置变化（宽高不变）也必须上报
  assert.ok(layoutCount() > before, 'resize 应触发 layout 上报');
  const m = posted.filter(x => x.type === 'layout').at(-1);
  assert.ok(Number.isFinite(m.x) && Number.isFinite(m.y) && m.width > 0 && m.height > 0,
    'layout 必须含 x/y/width/height');
  // 呼出恢复：下一帧重报（宿主工作区/比例可能已变）
  const { app, posted: p2, flushRaf: fl2 } = loadApp();
  app.onHostVisibility(true);
  const beforeShow = p2.filter(x => x.type === 'layout').length;
  fl2();
  assert.ok(p2.filter(x => x.type === 'layout').length > beforeShow, '呼出后应重报 layout');
});

test('S01: 现场行——scene-update 正常展示、坏消息隐藏不炸', () => {
  const { app, els } = loadApp();
  app.onHostScene({ type: 'scene-update', current: { pid: 123, process: 'wps.exe', exe: 'D:\WPS\wps.exe', title: '合同评审.docx - WPS Office', class: 'Qt5156QWindowIcon', ts: 1791200000.5 } });
  const el = els['scene-line']; // 懒注册：首次 onHostScene 后才存在
  assert.equal(el.hidden, false);
  assert.ok(el.textContent.includes('wps') && el.textContent.includes('合同评审'));
  assert.ok(el.title.includes('D:\WPS\wps.exe'));
  // 去重：同 (pid,title) 不重复入历史
  app.onHostScene({ type: 'scene-update', current: { pid: 123, process: 'wps.exe', title: '合同评审.docx - WPS Office', ts: 1791200005 } });
  assert.equal(app.state.sceneHistory.length, 1);
  // 换现场入历史
  app.onHostScene({ type: 'scene-update', current: { pid: 9, process: 'WindowsTerminal.exe', title: 'pytest', ts: 1791200010 } });
  assert.equal(app.state.sceneHistory.length, 2);
  assert.equal(app.state.sceneHistory[0].title, 'pytest');
  // 坏消息：null/缺 title/非对象 → 隐藏不炸
  app.onHostScene({ type: 'scene-update', current: null });
  assert.equal(el.hidden, true);
  assert.doesNotThrow(() => app.onHostScene({ type: 'scene-update' }));
  assert.doesNotThrow(() => app.onHostScene(null));
  assert.equal(el.hidden, true);
});

test('S02: 探测结果富化现场行——材料摘要/覆盖度/坏消息不炸', () => {
  const { app, els } = loadApp();
  app.onHostScene({ type: 'scene-update', current: { pid: 123, process: 'Code.exe', exe: 'D:\Code\Code.exe', title: 'server.py — VS Code', ts: 1791200100 } });
  app.onHostProbe({
    type: 'scene-probe', scenario: 'ide', coverage: 'full', note: '',
    identity: null,
    materials: [
      { kind: 'file', source: 'title', chars: 10, preview: 'server.py' },
      { kind: 'selection', source: 'uia:text', chars: 128, preview: "print('hi')" },
    ],
  });
  const el = els['scene-line'];
  assert.ok(el.textContent.includes('server.py'), '现场行含标题');
  assert.ok(el.textContent.includes('选区 128字'), '材料摘要追加');
  assert.ok(el.title.includes('完整'), '覆盖度进 tooltip');
  // 坏消息：非对象/缺字段不炸、不覆盖已有展示
  assert.doesNotThrow(() => app.onHostProbe(null));
  assert.doesNotThrow(() => app.onHostProbe({ type: 'scene-probe' }));
  assert.doesNotThrow(() => app.onHostProbe({ type: 'scene-probe', materials: 'bad' }));
  assert.ok(el.textContent.includes('选区 128字'), '坏消息不清除已展示材料');
});

test('S03: 提交携带现场材料——契约消息/确认条/移除后不携带', () => {
  const { app, els, posted } = loadApp();
  app.onHostProbe({
    type: 'scene-probe', scenario: 'ide', coverage: 'full', note: '',
    identity: { pid: 1, process: 'Code.exe', title: 'server.py — VS Code', ts: 1 },
    materials: [
      { kind: 'file', source: 'title', chars: 10, preview: 'server.py', text: 'server.py' },
      { kind: 'selection', source: 'uia:text', chars: 128, preview: 'print(1)', text: 'print(1)\nprint(2)' },
    ],
  });
  app.switchMode('chat');
  // 确认条出现且含材料摘要
  const strip = els['scene-strip'];
  assert.equal(strip.hidden, false, 'chat 模式且有材料 → 确认条可见');
  assert.ok(strip.innerHTML.includes('选区 128字'));
  // 提交 → submit-task 携带 scene（含全文，不含 preview 之外的杂字段）
  els['command'].value = '解释这段';
  app.submitTask();
  const msg = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.ok(msg, '应有 submit-task 消息');
  assert.equal(msg.text, '解释这段');
  assert.equal(msg.scene.materials.length, 2);
  assert.equal(msg.scene.materials[1].text, 'print(1)\nprint(2)');
  assert.equal(msg.scene.scenario, 'ide');
  // 移除附带 → 再提交 scene 为 null（先送达受理回执清除在途态，票 01 起
  // 连续 submitTask 在途会被忽略）
  app.onHubSubmitResult({ status: 200, json: JSON.stringify({ conversation_id: 'conv-1' }) });
  app.state.sceneAttached = false;
  els['command'].value = '再来一次';
  app.submitTask();
  const msg2 = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.strictEqual(msg2.scene, null, '移除后不得携带现场');
});

test('S04: L2 兜底材料并入现场并显性提示', () => {
  const { app, els, posted } = loadApp();
  app.onHostProbe({
    type: 'scene-probe', scenario: 'generic', coverage: 'none', note: '',
    identity: { pid: 5, process: 'someapp.exe', title: '某窗口', ts: 1 }, materials: [],
  });
  app.switchMode('chat');
  app.onHostCapture({
    type: 'scene-capture', note: '',
    material: { kind: 'screen_text', source: 'ocr', chars: 66, preview: '窗口里的一些文字…', text: '窗口里的一些文字' },
  });
  // 确认条出现屏幕文字材料
  const strip = els['scene-strip'];
  assert.equal(strip.hidden, false, '兜底材料后确认条可见');
  assert.ok(strip.innerHTML.includes('屏幕文字 66字'), '确认条含屏幕文字');
  // toast 显性提示
  assert.ok(els['toast'].innerHTML.includes('仅文本，不存图像'), '显性提示');
  // 提交时随任务携带（含兜底材料）
  els['command'].value = '处理一下';
  app.submitTask();
  const msg = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.equal(msg.scene.materials.length, 1);
  assert.equal(msg.scene.materials[0].kind, 'screen_text');
});
test('S05: 任务中枢真实渲染——事件对话/任务面板/许可/系统指标/断连诚实', () => {
  const { app, els, posted } = loadApp();
  const payload = {
    available: true,
    tasks: { stats: { running: 1, needs_input: 1, ready: 1 }, tasks: [
      { task_id: 't1', source: 'acp', ref_id: 'run-1', agent: 'dsh', title: '整理文档', status: 'running', unread: false },
      { task_id: 't2', source: 'acp', ref_id: 'run-2', agent: 'dsh', title: '审查合同', status: 'needs_input', unread: true },
    ], agents: [] },
    conversations: [{ conversation_id: 'conv-1', title: '工作', cwd: 'D:/x', last_activity_at: 1791200000,
      events: [
        { seq: 1, kind: 'user', payload: { text: '帮我整理' } },
        { seq: 2, kind: 'assistant', payload: { text: '开始处理' } },
        { seq: 3, kind: 'tool', payload: { title: 'read_file', status: 'done' } },
      ] }],
    active_run: { run_id: 'run-1', conversation_id: 'conv-1', state: 'RUNNING', state_label: '正在执行' },
    pending_permissions: [{ permission_id: 'perm-9', tool_title: '写入文件', tool_kind: 'write',
      options: [{ option_id: 'allow', name: '允许' }, { option_id: 'deny', name: '拒绝' }] }],
    system: { cpu: 12, memory: 54, memory_gb: '17.3/32 GB' },
  };
  app.onHubUpdate({ type: 'hub-update', json: JSON.stringify(payload) });
  // 对话渲染真实事件 + 在跑指示
  app.switchMode('chat');
  const content = els['content'];
  assert.ok(content.innerHTML.includes('帮我整理'), '用户消息渲染');
  assert.ok(content.innerHTML.includes('开始处理'), '助手消息渲染');
  assert.ok(content.innerHTML.includes('read_file'), '工具事件渲染');
  assert.ok(content.innerHTML.includes('正在执行'), '在跑状态行');
  assert.equal(app.state.conversationId, 'conv-1', '自动跟随活跃会话');
  // 任务面板真实
  app.state.activity = true;
  app.renderActivity();
  const panel = els['activity-panel'];
  assert.ok(panel.innerHTML.includes('整理文档') && panel.innerHTML.includes('审查合同'), '任务行');
  assert.ok(panel.innerHTML.includes('待确认'), '许可计数');
  assert.ok(panel.innerHTML.includes('data-permission="perm-9"'), '许可按钮带真 id');
  // 系统指标真实
  assert.ok(els['system-button'].innerHTML.includes('12'), 'CPU 实时');
  // 提交携带会话 id
  els['command'].value = '继续';
  app.submitTask();
  const msg = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.equal(msg.conversation_id, 'conv-1');
  // 断连诚实显示
  app.onHubUnavailable({ type: 'hub-unavailable', reason: 'HTTP -1 connection refused' });
  app.switchMode('chat');
  assert.ok(els['content'].innerHTML.includes('任务中枢未连接'), '断连面板');
});

test('S06: 四态反馈——末条助手消息挂反馈行并提交', () => {
  const { app, els, posted } = loadApp();
  app.onHubUpdate({ type: 'hub-update', json: JSON.stringify({
    available: true,
    tasks: { stats: {}, tasks: [] },
    conversations: [{ conversation_id: 'conv-1', title: '工作', last_activity_at: 1,
      events: [
        { seq: 1, run_id: 'run-7', kind: 'user', payload: { text: '查一下' } },
        { seq: 2, run_id: 'run-7', kind: 'assistant', payload: { text: '结果是……' } },
      ] }],
    active_run: null, pending_permissions: [], system: null,
  }) });
  app.state.conversationId = 'conv-1'; // B01 起：须显式选中才展示该会话
  app.switchMode('chat');
  const html = els['content'].innerHTML;
  assert.ok(html.includes('data-feedback="accepted"'), '四态按钮渲染');
  assert.ok(html.includes('data-run="run-7"'), '按钮带 run_id');
  // 已反馈态：payload feedback → 当前态高亮（禁用其他）
  app.onHubUpdate({ type: 'hub-update', json: JSON.stringify({
    available: true, tasks: { stats: {}, tasks: [] },
    conversations: [{ conversation_id: 'conv-1', title: '工作', last_activity_at: 1,
      feedback: { run_id: 'run-7', verdict: 'accepted' },
      events: [
        { seq: 2, run_id: 'run-7', kind: 'assistant', payload: { text: '结果是……' } },
      ] }],
    active_run: null, pending_permissions: [], system: null,
  }) });
  assert.ok(els['content'].innerHTML.includes('✓ 可用'), '当前反馈态显示');
  // 点击提交 hub-feedback
  const btn = { dataset: { feedback: 'modified', run: 'run-7' }, closest: (sel) => (sel === '[data-feedback]' ? btn : null) };
  els['content'].dispatch('click', { target: btn });
  const msg = posted.filter((m) => m.type === 'hub-feedback').at(-1);
  assert.deepEqual([msg.run_id, msg.verdict], ['run-7', 'modified']);
});

test('S07: 剪贴板信号——文件列表合并进现场材料，幂等不堆叠', () => {
  const { app, els } = loadApp();
  app.onHostScene({ type: 'scene-update', current: { pid: 1, process: 'explorer.exe', exe: '', title: '下载', class: 'CabinetWClass', ts: 1 } });
  app.onHostClipboard({ type: 'scene-clipboard', files: ['D:\dl\a.pdf', 'D:\dl\b.docx'], hasText: false });
  let clip = app.state.sceneProbe.materials.filter((x) => x.kind === 'clipboard');
  assert.equal(clip.length, 1);
  assert.equal(clip[0].text, 'D:\dl\a.pdf\nD:\dl\b.docx');
  assert.ok(clip[0].preview.includes('2 个文件'));
  // 重复推送：替换不堆叠
  app.onHostClipboard({ type: 'scene-clipboard', files: ['D:\dl\c.zip'], hasText: true });
  clip = app.state.sceneProbe.materials.filter((x) => x.kind === 'clipboard');
  assert.equal(clip.length, 1);
  assert.ok(clip[0].text.includes('c.zip'));
  // 纯文本剪贴板不出材料（红线：文本内容不出境）
  app.state.sceneProbe = { materials: [] };
  app.onHostClipboard({ type: 'scene-clipboard', files: [], hasText: true });
  assert.equal(app.state.sceneProbe.materials.length, 0);
  // 坏消息不炸
  app.onHostClipboard(null);
  app.onHostClipboard({ type: 'scene-clipboard', files: 'junk' });
  assert.equal(app.state.sceneProbe.materials.length, 0);
});

test('T01: 滴答真数据——hub today 段驱动速览，勾选走写回桥接', () => {
  const { app, els, posted } = loadApp();
  const todayStr = new Date().toISOString().slice(0, 10);
  const payload = { tasks: { tasks: [], stats: {} }, conversations: [], pending_permissions: [],
    today: { fetched_at: 1,
      tasks: [{ id: 't1', project_id: 'inbox1', title: '回邮件', list: 'Inbox', start: `${todayStr}T00:00:00+0800`, due: `${todayStr}T00:00:00+0800`, all_day: true, priority: 0 }],
      events: [{ id: 't2', project_id: 'inbox1', title: '评审会', list: '工作', start: `${todayStr}T23:00:00+0800`, due: `${todayStr}T23:30:00+0800`, all_day: false, priority: 0 }] } };
  app.onHubUpdate({ type: 'hub-update', json: JSON.stringify(payload) });
  assert.equal(app.todayData().real, true);
  assert.ok(els['content'].innerHTML.includes('回邮件'), '速览应含真实待办');
  assert.ok(els['content'].innerHTML.includes('评审会'), '速览应含真实日程');
  assert.equal(els['demo-tag'].textContent, '演示数据'); // 未经 render() 不刷新
  // 勾选：真实路径发 today-complete 桥接，不发假 setTimeout
  app.toggleTodo('t1');
  const msgs = posted.filter((m) => m.type === 'today-complete');
  assert.equal(msgs.length, 1);
  assert.deepEqual([msgs[0].task_id, msgs[0].project_id], ['t1', 'inbox1']);
  assert.equal(app.state.todayPendingId, 't1');
  app.onTodayCompleteResult({ status: 200, json: '{}' });
  assert.equal(app.state.todayPendingId, null);
});

test('T02: 无 today 段时回退演示数据', () => {
  const { app } = loadApp();
  app.onHubUpdate({ type: 'hub-update', json: JSON.stringify({ tasks: {}, conversations: [] }) });
  assert.equal(app.todayData().real, false);
});

// ---------- 票 01：提交与会话连续性（B01/B02/B03/R01/R02/R09） ----------

function hubPayloadWith(conversations, activeRun = null) {
  return JSON.stringify({
    available: true, tasks: { stats: {}, tasks: [] },
    conversations, active_run: activeRun, pending_permissions: [], system: null,
  });
}

test('B01/R01: 未选会话展示新会话空态——不回退最近会话，提交与展示同为新建', () => {
  const { app, els, posted } = loadApp();
  app.onHubUpdate({ type: 'hub-update', json: hubPayloadWith([
    { conversation_id: 'conv-old', title: '旧会话', last_activity_at: 1,
      events: [
        { seq: 1, kind: 'user', payload: { text: '旧消息' } },
        { seq: 2, kind: 'assistant', payload: { text: '旧回复可见性检查' } },
      ] },
  ]) });
  app.switchMode('chat');
  const html = els['content'].innerHTML;
  assert.ok(html.includes('新会话'), '未选会话应为新会话空态');
  assert.ok(!html.includes('旧回复可见性检查'), '不得回退展示最近会话的旧回复');
  assert.equal(app.state.conversationId, null);
  // 提交使用与展示一致的标识：null = 新建
  els['command'].value = '全新任务';
  app.submitTask();
  const msg = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.equal(msg.conversation_id, null, '展示为新会话时提交必须是新建');
  // 受理后绑定新会话：展示与提交引用同一 ID
  app.onHubSubmitResult({ status: 200, json: JSON.stringify({ conversation_id: 'conv-new' }) });
  assert.equal(app.state.conversationId, 'conv-new');
  els['command'].value = '继续这个';
  app.submitTask();
  const msg2 = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.equal(msg2.conversation_id, 'conv-new', '已选会话提交引用同一会话');
});

test('B01: 已选会话从中枢列表消失 → 显示与提交一同回新会话态', () => {
  const { app, els, posted } = loadApp();
  app.onHubUpdate({ type: 'hub-update', json: hubPayloadWith([
    { conversation_id: 'conv-1', title: '工作', last_activity_at: 1, events: [
      { seq: 1, kind: 'assistant', payload: { text: '内容' } }] },
  ]) });
  app.state.conversationId = 'conv-1';
  // 会话从中枢消失（超出总览护栏）
  app.onHubUpdate({ type: 'hub-update', json: hubPayloadWith([]) });
  assert.equal(app.state.conversationId, null, '失效选择不得悄悄续用');
  app.switchMode('chat');
  assert.ok(els['content'].innerHTML.includes('新会话'), '空态而非残页');
  // 活跃轮次所在会话例外：跟随在跑不算失效
  app.onHubUpdate({ type: 'hub-update', json: hubPayloadWith([],
    { run_id: 'run-9', conversation_id: 'conv-live', state: 'RUNNING' }) });
  assert.equal(app.state.conversationId, 'conv-live');
});

test('B02/R02: 明确失败保留草稿与附件；未知结果沿用同一 request_id 重发', () => {
  const { app, els, posted } = loadApp();
  app.onHubUpdate({ type: 'hub-update', json: hubPayloadWith([]) });
  app.switchMode('chat');
  app.state.attachment = { name: 'a.txt', path: 'D:/t/a.txt' };
  els['command'].value = '带附件任务';
  app.submitTask();
  const m1 = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.deepEqual(m1.context_refs, ['D:/t/a.txt'], 'B03: 结构化引用随提交发出');
  assert.ok(m1.request_id, '提交携带请求身份');
  // 明确失败（HTTP 400）：草稿与附件保留，可继续编辑
  app.onHubSubmitResult({ status: 400, json: JSON.stringify({
    error: { code: 'invalid_context_ref', message: '附件路径不存在: D:/t/a.txt' } }) });
  assert.equal(els['command'].value, '带附件任务', '失败后草稿不得清空');
  assert.ok(app.state.attachment, '失败后附件保留');
  assert.ok(els['toast'].innerHTML.includes('发起失败'), '失败有提示');
  // 明确失败后重发 = 新请求身份（原 ID 未受理，作废）
  app.submitTask();
  const m2 = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.notEqual(m2.request_id, m1.request_id);
  // 未知结果（-1 连接失败/超时）：保留草稿，重发沿用同一 request_id
  app.onHubSubmitResult({ status: -1, json: '' });
  assert.equal(els['command'].value, '带附件任务', '结果未知不清草稿');
  assert.ok(app.state.pendingRetry && app.state.pendingRetry.request_id === m2.request_id,
    '记录待核对请求身份');
  app.submitTask();
  const m3 = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.equal(m3.request_id, m2.request_id, '未知结果重发必须沿用同一 request_id（R09）');
  assert.equal(m3.text, '带附件任务');
  // 受理回执到达：才清空草稿与附件
  app.onHubSubmitResult({ status: 200, json: JSON.stringify({
    conversation_id: 'conv-r', request_id: m3.request_id }) });
  assert.equal(els['command'].value, '', '受理后清空');
  assert.equal(app.state.attachment, null, '受理后清空附件');
  assert.equal(app.state.pendingRetry, null);
});

test('R09: 在途提交被忽略——不把纯前端防重当唯一手段，但不主动并发', () => {
  const { app, els, posted } = loadApp();
  app.onHubUpdate({ type: 'hub-update', json: hubPayloadWith([]) });
  app.switchMode('chat');
  els['command'].value = '双击场景';
  app.submitTask();
  const count = posted.filter((m) => m.type === 'submit-task').length;
  app.submitTask(); // 在途：直接忽略
  app.submitTask();
  assert.equal(posted.filter((m) => m.type === 'submit-task').length, count,
    '在途期间不得发出第二条');
  assert.ok(els['submit'].disabled, '在途时发送按钮禁用');
});

// ---------- 票 02：现场版本与材料一致性（R04/R05 交错注入） ----------

test('R04: 新代次默认不携带旧材料；旧窗口迟到 OCR/probe 整体丢弃；等待期可纯文字发送', () => {
  const { app, els, posted } = loadApp();
  // 第一轮：旧窗口探测带材料
  app.onHostProbe({ type: 'scene-probe', scene_id: 'scene-1-aa', generation: 1,
    scenario: 'ide', coverage: 'partial',
    identity: { pid: 1, process: 'Old.exe', title: '旧文档 — Old', ts: 1 },
    materials: [{ kind: 'selection', source: 'uia:text', chars: 10, preview: '旧选区', text: '旧选区内容' }] });
  assert.equal(app.state.sceneId, 'scene-1-aa');
  assert.equal(app.state.sceneProbe.materials.length, 1);
  // 目标变化：新窗口身份（代次 2）到达
  app.onHostScene({ type: 'scene-update', scene_id: 'scene-2-bb', generation: 2,
    current: { pid: 2, process: 'New.exe', title: '新窗口 — New', ts: 2 } });
  assert.equal(app.state.sceneId, 'scene-2-bb');
  assert.equal(app.state.sceneProbe.materials.length, 0, '新代次默认不携带旧材料');
  assert.equal(app.state.scene.title, '新窗口 — New');
  // 旧窗口的迟到 OCR（代次 1）不得污染当前现场
  app.onHostCapture({ type: 'scene-capture', scene_id: 'scene-1-aa', generation: 1,
    material: { kind: 'screen_text', source: 'ocr', chars: 20, preview: '旧OCR', text: '旧窗口迟到的OCR文字' } });
  assert.equal(app.state.sceneProbe.materials.length, 0, '旧代次材料必须丢弃');
  // 旧代次的迟到 probe 同样丢弃，且身份不得覆盖新现场
  app.onHostProbe({ type: 'scene-probe', scene_id: 'scene-1-aa', generation: 1,
    scenario: 'ide', coverage: 'full',
    identity: { pid: 1, process: 'Old.exe', title: '旧文档 — Old', ts: 3 },
    materials: [{ kind: 'file', source: 'title', chars: 5, preview: '旧', text: '旧文档' }] });
  assert.equal(app.state.scene.title, '新窗口 — New', '旧代次身份不得覆盖');
  assert.equal(app.state.sceneProbe.materials.length, 0);
  // 新代次材料未到也允许纯文字发送（scene=null），不自动附带旧材料
  app.switchMode('chat');
  els['command'].value = '纯文字任务';
  app.submitTask();
  const msg = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.strictEqual(msg.scene, null, '等待期不得自动附带旧材料');
  app.onHubSubmitResult({ status: 200, json: JSON.stringify({ conversation_id: 'c1' }) });
  // 新代次探测到达后正常携带（含 scene_id/generation）
  app.onHostProbe({ type: 'scene-probe', scene_id: 'scene-2-bb', generation: 2,
    scenario: 'generic', coverage: 'partial',
    identity: { pid: 2, process: 'New.exe', title: '新窗口 — New', ts: 4 },
    materials: [{ kind: 'screen_text', source: 'ocr', chars: 8, preview: '新', text: '新材料A' }] });
  els['command'].value = '带上新现场';
  app.submitTask();
  const msg2 = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.equal(msg2.scene.scene_id, 'scene-2-bb', '冻结现场带 scene_id');
  assert.equal(msg2.scene.generation, 2, '冻结现场带 generation');
  assert.equal(msg2.scene.materials.length, 1);
  assert.equal(msg2.scene.materials[0].text, '新材料A');
});

test('R05: 同代次合并——full probe 不清剪贴板，fast/full 重叠去重，OCR 不堆叠', () => {
  const { app } = loadApp();
  const R1 = { scene_id: 'scene-1-x', generation: 1 };
  // 剪贴板先到
  app.onHostClipboard({ type: 'scene-clipboard', ...R1, files: ['D:/a.pdf', 'D:/b.docx'] });
  assert.equal(app.state.sceneProbe.materials.length, 1);
  // full probe 后到：不得清掉剪贴板材料（旧实现整包替换会丢）
  app.onHostProbe({ type: 'scene-probe', ...R1, scenario: 'explorer', coverage: 'partial',
    identity: { pid: 9, process: 'explorer.exe', title: '下载', ts: 1 },
    materials: [
      { kind: 'file', source: 'title', chars: 2, preview: '下载', text: '下载' },
      { kind: 'selection', source: 'uia:text', chars: 6, preview: '选', text: '选中的文件名' },
    ] });
  const kinds1 = app.state.sceneProbe.materials.map((m) => m.kind);
  assert.deepEqual(kinds1.slice().sort(), ['clipboard', 'file', 'selection'],
    '剪贴板材料保留，探测材料并入（不清同代次其他来源）');
  // fast/full 重叠：同 kind 同文本再来不堆叠；新文本并入
  app.onHostProbe({ type: 'scene-probe', ...R1, scenario: 'explorer', coverage: 'full',
    identity: { pid: 9, process: 'explorer.exe', title: '下载', ts: 2 },
    materials: [
      { kind: 'file', source: 'title', chars: 2, preview: '下载', text: '下载' },
      { kind: 'dialog_text', source: 'win32:static', chars: 5, preview: '按钮', text: '按钮文本' },
    ] });
  assert.equal(app.state.sceneProbe.materials.length, 4, '重复材料去重，新增并入');
  // OCR 后到：不同 kind 的相同文本按材料标识各自保留（材料标识=kind+文本哈希）
  app.onHostCapture({ type: 'scene-capture', ...R1,
    material: { kind: 'screen_text', source: 'ocr', chars: 6, text: '选中的文件名' } });
  assert.equal(app.state.sceneProbe.materials.length, 5, 'OCR 新 kind 并入');
  // 重复 OCR（同 kind 同文本）：去重不堆叠
  app.onHostCapture({ type: 'scene-capture', ...R1,
    material: { kind: 'screen_text', source: 'ocr', chars: 6, text: '选中的文件名' } });
  assert.equal(app.state.sceneProbe.materials.length, 5, '重复 OCR 不堆叠');
  // 剪贴板整类替换：新文件列表替换旧列表，其他来源保留
  app.onHostClipboard({ type: 'scene-clipboard', ...R1, files: ['D:/c.zip'] });
  const clip = app.state.sceneProbe.materials.filter((m) => m.kind === 'clipboard');
  assert.equal(clip.length, 1);
  assert.ok(clip[0].text.includes('c.zip'));
  assert.equal(app.state.sceneProbe.materials.length, 5, '其余材料不受剪贴板更新影响');
  // 关闭重开（新代次）：全部材料重置，附带默认恢复 true
  app.state.sceneAttached = false;
  app.onHostScene({ type: 'scene-update', scene_id: 'scene-2-y', generation: 2,
    current: { pid: 3, process: 'X.exe', title: '重开窗口', ts: 5 } });
  assert.equal(app.state.sceneProbe.materials.length, 0, '新代次材料重置');
  assert.equal(app.state.sceneAttached, true, '附带默认恢复');
});

// ---------- 复核修复回归（F05/F06/F08） ----------

test('F05: 结果未知重发冻结快照——现场变化与自动绑定会话不改变重试载荷', () => {
  const { app, els, posted } = loadApp();
  app.onHubUpdate({ type: 'hub-update', json: hubPayloadWith([]) });
  app.switchMode('chat');
  app.onHostProbe({ type: 'scene-probe', scene_id: 'scene-1', generation: 1,
    scenario: 'generic', coverage: 'full',
    identity: { pid: 1, title: 'A', process: 'A', ts: 1 },
    materials: [{ kind: 'uia', source: 's', text: 'A', chars: 1 }] });
  els['command'].value = 'same text';
  app.submitTask();
  const first = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.equal(first.conversation_id, null);
  assert.equal(first.scene.materials[0].text, 'A');
  app.onHubSubmitResult({ status: -1, json: '' });
  // 结果未知期间：现场换新 + 中枢轮询把会话自动绑定到活跃轮次
  app.onHostProbe({ type: 'scene-probe', scene_id: 'scene-2', generation: 2,
    scenario: 'generic', coverage: 'full',
    identity: { pid: 2, title: 'B', process: 'B', ts: 2 },
    materials: [{ kind: 'uia', source: 's', text: 'B', chars: 1 }] });
  app.onHubUpdate({ type: 'hub-update', json: hubPayloadWith([],
    { run_id: 'run-live', conversation_id: 'conv-auto', state: 'RUNNING' }) });
  assert.equal(app.state.conversationId, 'conv-auto');
  els['command'].value = 'same text';
  app.submitTask();
  const retry = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.equal(retry.request_id, first.request_id, '同文本重试沿用同一 request_id');
  assert.deepEqual(retry.scene, first.scene, '重试载荷必须冻结为首投快照（现场不变）');
  assert.equal(retry.conversation_id, null, '重试载荷不得改用自动绑定的会话');
  // 改文本 = 新任务：新 request_id，不冒用未知结果的旧 ID
  app.onHubSubmitResult({ status: 200, json: JSON.stringify({ conversation_id: 'conv-auto', request_id: retry.request_id }) });
  els['command'].value = 'edited new task';
  app.submitTask();
  const fresh = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.notEqual(fresh.request_id, first.request_id);
  assert.equal(fresh.conversation_id, 'conv-auto');
});

test('F06: 受理回执只消费本次内容——发送期间新写的草稿与新附件保留', () => {
  const { app, els } = loadApp();
  app.onHubUpdate({ type: 'hub-update', json: hubPayloadWith([]) });
  app.switchMode('chat');
  els['command'].value = 'submitted A';
  app.submitTask();
  // 在途期间用户继续输入 B、换了新附件（均未发送）
  type(app, els, 'unsent draft B');
  app.state.attachment = { name: 'new.txt', path: 'D:/new.txt' };
  app.onHubSubmitResult({ status: 200, json: JSON.stringify({ conversation_id: 'c1' }) });
  assert.equal(els['command'].value, 'unsent draft B', '新草稿保留');
  assert.equal(app.state.drafts.chat, 'unsent draft B');
  assert.equal(app.state.attachment.path, 'D:/new.txt', '新附件保留');
  assert.ok(els['toast'].innerHTML.includes('已保留'), '显性提示');
  // 未修改的常规路径：回执正常清空
  els['command'].value = 'plain';
  app.submitTask();
  app.onHubSubmitResult({ status: 200, json: JSON.stringify({ conversation_id: 'c1' }) });
  assert.equal(els['command'].value, '');
  assert.equal(app.state.attachment, null);
  // 无在途时的陈旧回执：不消费、不动草稿
  els['command'].value = 'draft C';
  app.state.drafts.chat = 'draft C';
  app.onHubSubmitResult({ status: 200, json: JSON.stringify({ conversation_id: 'c9' }) });
  assert.equal(els['command'].value, 'draft C', '陈旧回执不得动草稿');
});

test('F08: 呼出 scene-round 立即废弃旧材料——采集等待期提交不带旧现场', () => {
  const { app, els, posted } = loadApp();
  app.switchMode('chat');
  app.onHostProbe({ type: 'scene-probe', scene_id: 'scene-1', generation: 1,
    scenario: 'generic', coverage: 'full',
    identity: { pid: 1, title: 'OLD', process: 'OLD', ts: 1 },
    materials: [{ kind: 'uia', source: 's', text: 'OLD MATERIAL', chars: 11 }] });
  // 隐藏 → 呼出：宿主在 visibility 后立即发布新代次重置（scene-round）
  app.onHostVisibility(false);
  app.onHostVisibility(true);
  app.onHostSceneRound({ type: 'scene-round', scene_id: 'scene-2-x', generation: 2 });
  // 新一轮采集尚未到达（探测可能失败/延迟）：此时发送
  type(app, els, 'synthetic new task');
  app.submitTask();
  const sent = posted.filter((m) => m.type === 'submit-task').at(-1);
  assert.strictEqual(sent.scene, null, '等待期不得携带上一现场材料');
  // 旧代次迟到材料仍被丢弃
  app.onHostCapture({ type: 'scene-capture', scene_id: 'scene-1', generation: 1,
    material: { kind: 'screen_text', source: 'ocr', text: 'OLD LATE', chars: 8 } });
  assert.equal(app.state.sceneProbe.materials.length, 0);
});
