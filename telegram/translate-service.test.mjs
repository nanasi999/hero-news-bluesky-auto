import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
const code=readFileSync(new URL('./translate-title.gs',import.meta.url),'utf8');
function service(seed={}){
  const data={...seed};let calls=0,held=false;
  const props={getProperty:k=>data[k]??null,setProperty:(k,v)=>{data[k]=v},getProperties:()=>({...data}),deleteProperty:k=>{delete data[k]}};
  const ctx=vm.createContext({ContentService:{MimeType:{JSON:'json'},createTextOutput:text=>({setMimeType:()=>JSON.parse(text)})},LockService:{getScriptLock:()=>({tryLock:()=>{held=true;return true},hasLock:()=>held,releaseLock:()=>{held=false}})},PropertiesService:{getScriptProperties:()=>props},Utilities:{DigestAlgorithm:{SHA_256:'sha256'},computeDigest:(_,s)=>createHash('sha256').update(s).digest(),base64EncodeWebSafe:b=>b.toString('base64url')},LanguageApp:{translate:s=>{calls++;return s==='仮題'?'Title':s}},Date});
  vm.runInContext(code,ctx);
  return {post:title=>ctx.doPost({postData:{contents:JSON.stringify({title})}}),data,calls:()=>calls,held:()=>held};
}
test('service caches identical titles and releases lock',()=>{const s=service();assert.equal(s.post('仮題').title,'Title');assert.equal(s.post('仮題').cached,true);assert.equal(s.calls(),1);assert.equal(s.held(),false)});
test('100-call ceiling rejects new translation and resets only after 24 hours',()=>{
  const s=service({quota:JSON.stringify({since:Date.now(),count:100})});assert.equal(s.post('仮題').error,'daily_limit');assert.equal(s.calls(),0);
  s.data.quota=JSON.stringify({since:Date.now()-86400001,count:100});assert.equal(s.post('仮題').ok,true);assert.equal(s.calls(),1);
});
test('bad input never calls translation',()=>{const s=service();for(const x of ['',null,123,'a'.repeat(501),'a\nb'])assert.equal(s.post(x).ok,false);assert.equal(s.calls(),0)});
test('known franchise names survive placeholder restoration',()=>{const s=service();assert.equal(s.post('仮面ライダー').title,'Kamen Rider');assert.equal(s.post('スーパー戦隊').title,'Super Sentai')});
test('cache remains bounded',()=>{const seed={};for(let i=0;i<200;i++)seed['t_'+i]=JSON.stringify({text:'Title',at:i});const s=service(seed);assert.equal(s.post('仮題').ok,true);assert.equal(Object.keys(s.data).filter(k=>k.startsWith('t_')).length,176)});
