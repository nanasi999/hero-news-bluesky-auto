import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {relay, articles} from './relay.mjs';
const row = (id, platform = 'threads') => ({stage:'confirmed',platform,identifiers:['https://hero-news.com/archives/' + id],payload:{text:'記事' + id + '\nhttps://hero-news.com/archives/' + id}});
const source = (...rows) => ({posts:Object.fromEntries(rows.map((r,i)=>[i,r]))});
function harness(initial = null) {
  let state = initial, revision = 0;
  const messages = [];
  return {messages, read: async()=>({state:structuredClone(state),sha:revision}), save:async(sha,next)=>{assert.equal(sha,revision);state=structuredClone(next);return ++revision;}, send:async text=>{messages.push(text);return messages.length;}, state:()=>state};
}
test('first activation skips history; next run forwards once',async()=>{
  const h=harness(); await relay({...h,source:source(row(1))}); assert.equal(h.messages.length,1);
  await relay({...h,source:source(row(1),row(2))}); assert.equal(h.messages.length,2);
  await relay({...h,source:source(row(1),row(2))}); assert.equal(h.messages.length,2);
});
test('both social platforms share one article identity',()=>assert.equal(articles(source(row(1),row(1,'bluesky'))).length,1));
test('only confirmed supported articles',()=>assert.equal(articles(source({...row(1),stage:'sending'},{...row(2),identifiers:['https://evil.invalid/a']})).length,0));
test('ambiguous sends are reserved and never repeated',async()=>{
  const h=harness();await relay({...h,source:source()});
  await assert.rejects(relay({...h,source:source(row(2)),send:async()=>{throw Error('timeout')}}));
  await assert.rejects(relay({...h,source:source(row(2))}),/reconciliation/); assert.equal(h.messages.length,1);
});
test('checkpoint failure prevents transmission',async()=>{
  const h=harness();await assert.rejects(relay({...h,source:source(),save:async()=>{throw Error('conflict')}}));assert.equal(h.messages.length,0);
});
test('invalid stored state cannot initialize or send',async()=>{const h=harness({version:2,posts:{}});await assert.rejects(relay({...h,source:source()}));assert.equal(h.messages.length,0)});
test('WordPress numeric links accepted and archive preferred',()=>{
  const r=row(1);r.identifiers=['https://hero-news.com/?p=42',...r.identifiers];
  assert.equal(articles(source(r))[0].text.endsWith('/archives/1'),true);
  r.identifiers=['https://hero-news.com/?p=42'];assert.equal(articles(source(r)).length,1);
});
test('failure saving send result cannot cause duplicate',async()=>{
  const h=harness();let saves=0;
  await assert.rejects(relay({...h,source:source(),save:async(...args)=>{if(++saves===3)throw Error('lost checkpoint');return h.save(...args)}}));
  await assert.rejects(relay({...h,source:source()}));assert.equal(h.messages.length,1);
});
test('concurrent runs cannot both claim same publication',async()=>{
  const h=harness();await relay({...h,source:source()});
  await Promise.allSettled([relay({...h,source:source(row(3))}),relay({...h,source:source(row(3))})]);assert.equal(h.messages.length,2);
});
test('real saved social data yields title and article URL without network',async()=>{
  let fixture;try {fixture=JSON.parse(await readFile(new URL('./source-ledger.fixture.json',import.meta.url),'utf8'));} catch(error){if(error.code==='ENOENT')return;throw error;}
  const entries=articles(fixture);assert.ok(entries.length>0);assert.ok(entries.every(e=>e.text.length<4096 && /https:\/\/hero-news.com\//.test(e.text)));
  const h=harness();await relay({...h,source:fixture});assert.equal(h.messages.length,1);assert.equal(h.messages[0],entries.at(-1).text);
});
