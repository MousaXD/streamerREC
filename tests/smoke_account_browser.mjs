// Run against a disposable StreamRec server with the fixture account below.
// Never run against a personal account: this test changes the fixture password.
const port=process.argv[2],base=process.argv[3]||'http://127.0.0.1:18080';
if(base!=='http://127.0.0.1:18080')throw Error('Use the disposable test port only');
const targets=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true})});
let seq=0;const pending=new Map();ws.addEventListener('message',event=>{const d=JSON.parse(event.data);if(d.id&&pending.has(d.id)){const {resolve,reject}=pending.get(d.id);pending.delete(d.id);d.error?reject(Error(d.error.message)):resolve(d.result)}});
const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}))});
const ev=async expression=>{const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value};
const wait=async expression=>{const end=Date.now()+12000;while(Date.now()<end){if(await ev(expression))return;await new Promise(r=>setTimeout(r,150))}throw Error('Timed out: '+expression)};
try{
 await call('Page.enable');await call('Page.navigate',{url:base});
 await wait(`document.getElementById('accLogin')?.style.display==='flex'`);
 await ev(`document.getElementById('alUser').value='fixture';document.getElementById('alPass').value='fixture-passphrase-123';document.querySelector('#accLogin .btn-acc').click()`);
 await wait(`document.getElementById('hdName').textContent==='fixture'&&!document.getElementById('accOv').classList.contains('open')`);
 await ev(`document.querySelector('.user-chip').click()`);
 await wait(`document.getElementById('accOv').classList.contains('open')&&document.getElementById('accProfile').style.display==='flex'`);
 console.log('Actual account-chip click opens account dialog: PASS');
 await ev(`document.getElementById('pcCur').value='fixture-passphrase-123';document.getElementById('pcNew').value='fixture-new-password-456';document.getElementById('pcNew2').value='fixture-mismatch';document.getElementById('pcSave').click()`);
 await wait(`document.getElementById('toast').textContent==='New passwords do not match'`);
 await ev(`document.getElementById('pcNew2').value='fixture-new-password-456';document.getElementById('pcSave').click()`);
 await wait(`document.getElementById('accLogin').style.display==='flex'&&document.getElementById('toast').textContent==='Password changed — sign in again'`);
 if(await ev(`fetch('/api/channels').then(r=>r.status)`)!==401)throw Error('Old session not revoked');
 if(await ev(`fetch('/api/account/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'fixture',password:'fixture-passphrase-123'})}).then(r=>r.status)`)!==401)throw Error('Old password accepted');
 await ev(`document.getElementById('alUser').value='fixture';document.getElementById('alPass').value='fixture-new-password-456';document.querySelector('#accLogin .btn-acc').click()`);
 await wait(`document.getElementById('hdName').textContent==='fixture'&&!document.getElementById('accOv').classList.contains('open')`);
 console.log('Password confirmation, change, old session/password rejection, new-password login: PASS');
}finally{ws.close()}
