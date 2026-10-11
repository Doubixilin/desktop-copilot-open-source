// Optional screenshot tool: npm install --no-save playwright, then npx playwright install chromium.
// Start demo/serve.py first. This only captures the synthetic browser preview.
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const base = process.argv[2] || 'http://127.0.0.1:18766';
  const url = new URL(base);
  if (url.hostname !== '127.0.0.1' || url.protocol !== 'http:') throw Error('Only loopback demo URLs are accepted');
  const browser = await chromium.launch({headless:true,...(process.env.DEMO_BROWSER_PATH ? {executablePath:process.env.DEMO_BROWSER_PATH} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1120,height:800},deviceScaleFactor:1});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === url.origin ? route.continue() : route.abort());
    await page.goto(base);
    const frame = page.frameLocator('#hud-frame');
    await frame.locator('#hud:not(.is-hidden)').waitFor();
    await frame.locator('#system-button').filter({hasText:'18'}).waitFor();
    const dir = path.resolve(__dirname,'../docs/images');
    fs.mkdirSync(dir,{recursive:true});
    const shot = async(name) => {
      await page.waitForTimeout(400);
      const box = await frame.locator('#hud').boundingBox();
      await page.setViewportSize({width:1120,height:Math.max(600,Math.ceil(box.height+280))});
      await page.waitForTimeout(150);
      await page.screenshot({path:path.join(dir,`hud-${name}.png`)});
    };
    await shot('overview');
    await frame.locator('#command').fill('设计');
    await frame.locator('.result-row').first().waitFor();
    if (await frame.locator('.result-row').count() !== 3) throw Error('Synthetic search did not return three items');
    await shot('files');
    await frame.locator('#command').fill('');
    await frame.locator('#chat-mode').click();
    await frame.locator('#project-button').click();
    await frame.locator('[data-conversation="demo-conversation"]').click();
    const hud = page.frames().find(f=>new URL(f.url()).pathname === '/hud.html');
    await hud.evaluate(()=>window.demoScene());
    await frame.locator('#scene-strip').waitFor();
    await frame.locator('.chat-log').waitFor();
    await shot('copilot');
    await frame.locator('#files-mode').click();
    await frame.locator('#system-button').click();
    await frame.locator('.system-detail').waitFor();
    await shot('system');
    for (const target of ['/.git/config','/server.py','/copilot/hud','/../LICENSE','/api-token']) {
      const r = await page.request.get(base+target);
      if(r.status() !== 404) throw Error(`Demo exposed ${target}`);
    }
    if(errors.length) throw Error(errors.join('\n'));
    console.log('4 screenshots; synthetic search, scene, chat, system and HTTP allowlist verified; no page errors.');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
