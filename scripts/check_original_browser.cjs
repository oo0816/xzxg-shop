const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'shopping-browser-'));
const browser = ['C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'].find(p => fs.existsSync(p));
assert(browser, 'Install Chrome or Edge to run browser checks');
const chrome = spawn(browser, [
  '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank',
], {windowsHide: true, stdio: 'ignore'});
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const report = {checks: [], errors: [], posts: []};
let ws;
(async () => {
  let active;
  for (let n=0;n<100;n++) {try{active=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8');break;}catch{} await sleep(100);}
  assert(active, 'Chrome debugging endpoint');
  const port = active.split('\n')[0];
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {method:'PUT'})).json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject) => {ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
  let seq=0;
  const pending=new Map();
  ws.addEventListener('message', (event) => {
    const m=JSON.parse(event.data);
    if (m.id) {const handler=pending.get(m.id); if(handler){pending.delete(m.id); m.error?handler.reject(new Error(JSON.stringify(m.error))):handler.resolve(m.result);}}
    if (m.method==='Runtime.exceptionThrown') report.errors.push(m.params.exceptionDetails.text);
    if (m.method==='Network.requestWillBeSent' && m.params.request.url.includes('/api/') && m.params.request.method==='POST') report.posts.push(JSON.parse(m.params.request.postData));
  });
  function call(method,params={}) {return new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});}
  async function evaluate(expression) {
    const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
    if(r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  }
  async function until(expression,label) {
    for(let n=0;n<300;n++){if(await evaluate(expression)){report.checks.push(label);return;}await sleep(100);}
    throw new Error('Timed out: '+label+' | '+await evaluate('document.body.innerText'));
  }

  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride',{width:1360,height:980,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:'http://127.0.0.1:5173/'});
  await until("document.querySelector('.login-form')!==null",'original login page');
  await evaluate("document.querySelector('.login-form').requestSubmit()");
  await until("document.body.innerText.includes('平台管理') && document.querySelector('.account-card')!==null",'admin login through Vite proxy');
  await evaluate("[...document.querySelectorAll('nav button')].find(b=>b.textContent.includes('商品巡检')).click()");
  await until("document.body.innerText.includes('X Phone 12')",'admin can view database products');
  fs.writeFileSync(path.join(root,'.local','admin-products.png'),Buffer.from((await call('Page.captureScreenshot',{format:'png'})).data,'base64'));
  await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='退出').click()");
  await until("document.querySelector('.login-form')!==null",'logout returns to login');
  await evaluate("[...document.querySelectorAll('.demo-account')].find(b=>b.textContent.includes('商家端')).click()");
  await until("document.querySelector('.login-form input').value==='merchant'",'merchant form updated');
  await evaluate("document.querySelector('.login-form').requestSubmit()");
  await until("document.body.innerText.includes('商家工作台') && document.querySelector('.account-card')!==null",'merchant login');
  await evaluate("[...document.querySelectorAll('nav button')].find(b=>b.textContent.includes('商品预览')).click()");
  await until("document.body.innerText.includes('X Phone 12')",'merchant product preview');
  assert.equal(report.errors.length,0);
  report.checks.push('no uncaught browser exceptions');
  delete report.posts;
  fs.writeFileSync(path.join(root,'.local','original-browser.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
})().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(async()=>{if(ws)ws.close();chrome.kill();});
