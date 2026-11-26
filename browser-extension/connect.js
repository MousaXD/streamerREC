'use strict';
const platformConfig={
  tiktok:{login:'https://www.tiktok.com/login',domains:['tiktok.com'],origins:['https://*.tiktok.com/*']},
  bigo:{login:'https://www.bigo.tv/',domains:['bigo.tv','bigo.sg'],origins:['https://*.bigo.tv/*','https://*.bigo.sg/*']}
};
const $=id=>document.getElementById(id);
function updateLogin(){$('login').href=platformConfig[$('platform').value].login}
function status(message,error=false){$('status').textContent=message;$('status').style.color=error?'#fbbf24':'#34d399'}
$('platform').addEventListener('change',updateLogin);
(async()=>{const {pending}=await chrome.storage.session.get('pending');if(pending&&pending.expires>Date.now()&&platformConfig[pending.platform]){$('platform').value=pending.platform;$('code').value=pending.code;updateLogin();status('Pairing ready. Sign in on the official site, then import.')}else{await chrome.storage.session.remove('pending')}})().catch(()=>{});
$('connect').addEventListener('click',async()=>{
  const platform=$('platform').value,code=$('code').value.trim(),cfg=platformConfig[platform];
  if(!/^[A-Za-z0-9_-]{32}$/.test(code)){status('Generate or paste a fresh pairing code from StreamRec.',true);return}
  $('connect').disabled=true;let granted=false;
  try{
    // This request is directly triggered by the user's click. No broad host permission.
    granted=await chrome.permissions.request({origins:cfg.origins});
    if(!granted){status('Permission was declined; nothing was imported.',true);return}
    const cookies=(await Promise.all(cfg.domains.map(domain=>chrome.cookies.getAll({domain})))).flat().filter(c=>!c.partitionKey);
    if(!cookies.length){status('No platform cookies found. Sign in first.',true);return}
    const response=await fetch('http://127.0.0.1:8080/api/platform-accounts/connect',{method:'POST',credentials:'omit',cache:'no-store',redirect:'error',headers:{'Content-Type':'application/json','X-StreamRec-Pairing':code},body:JSON.stringify({platform,cookies})});
    if(!response.ok){status('Connection rejected. Generate a fresh pairing code and try again.',true);return}
    await chrome.storage.session.remove('pending');$('code').value='';status('Session imported into StreamRec. Return to Connected Accounts to see its status. Actual login validity depends on the platform.');
  }catch{status('Could not connect to the local recorder. Make sure StreamRec is open.',true)}
  finally{if(granted){try{await chrome.permissions.remove({origins:cfg.origins})}catch{}}$('connect').disabled=false}
});
