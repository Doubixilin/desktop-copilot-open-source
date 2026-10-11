// CDP 驱动 NativeHud 内嵌 WebView2（需以 --remote-debugging-port=9223 启动）。
// 仅作验收工具：求值页面 JS、截屏。用法：
//   node cdp.mjs list
//   node cdp.mjs eval "1+1"
//   node cdp.mjs shot out.png
//   node cdp.mjs evalfile script.js
import { writeFileSync } from 'node:fs';

const port = process.env.CDP_PORT || '9223';
const [cmd, ...rest] = process.argv.slice(2);

const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const page = targets.find(t => t.type === 'page');
if (cmd === 'list') {
  console.log(JSON.stringify(targets.map(t => ({ type: t.type, url: t.url, title: t.title })), null, 2));
  process.exit(0);
}
if (!page) { console.error('no page target'); process.exit(1); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
let seq = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const p = pending.get(m.id);
    pending.delete(m.id);
    m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
  }
};
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

if (cmd === 'eval' || cmd === 'evalfile') {
  const expr = cmd === 'eval' ? rest.join(' ') : String(await import('node:fs').then(fs => fs.readFileSync(rest[0])));
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  console.log(JSON.stringify(r.result?.value !== undefined ? r.result.value : r, null, 2));
} else if (cmd === 'shot') {
  await send('Page.enable');
  const r = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(rest[0], Buffer.from(r.data, 'base64'));
  console.log('saved: ' + rest[0]);
} else {
  console.error('unknown command');
  process.exit(1);
}
ws.close();
process.exit(0);
