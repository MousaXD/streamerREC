'use strict';
const allowedOrigins=new Set(['http://127.0.0.1:8080','http://localhost:8080']);
chrome.runtime.onMessageExternal.addListener((message,sender,respond)=>{
  let origin;try{origin=new URL(sender.url).origin}catch{return false}
  if(!allowedOrigins.has(origin)||message?.type!=='streamrec-connect'||!['tiktok','bigo'].includes(message.platform)||!/^[A-Za-z0-9_-]{32}$/.test(message.code||''))return false;
  (async()=>{
    await chrome.storage.session.set({pending:{platform:message.platform,code:message.code,expires:Date.now()+300000}});
    await chrome.tabs.create({url:chrome.runtime.getURL('connect.html')});
    respond({ok:true});
  })().catch(()=>respond({ok:false}));
  return true;
});
