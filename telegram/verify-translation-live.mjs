import assert from 'node:assert/strict';
import {appsScriptTranslator, translateMessage} from './translate-message.mjs';
const request=appsScriptTranslator(process.env.TITLE_TRANSLATION_URL);
const cases=[
  ['ウルトラマンテオって戦い方が多彩で見てて面白いよね','Ultraman'],
  ['【仮面ライダーギーツ】レーザーブーストとブーストフォームマーク２、どっちが好き？','Kamen Rider'],
  ['スパイダーマン→蜘蛛に噛まれた、バットマン→…？','Batman'],
  ['スーパー戦隊','Super Sentai'],
];
for(const [title,name] of cases){
  const result=await request(title);
  assert.equal(result.ok,true,JSON.stringify(result));
  assert.ok(result.title.includes(name),result.title);
  console.log(JSON.stringify({title,english:result.title}));
  const url='https://hero-news.com/archives/1789381677.html';
  const message=await translateMessage(title+'\n#Tokusatsu\n'+url,request);
  assert.ok(message.endsWith('\n#Tokusatsu\n'+url));
  const cached=await request(title);
  assert.equal(cached.cached,true,JSON.stringify(cached));
  console.log('Cached title and final message verified.');
}
assert.equal((await request('')).ok,false);
assert.equal((await request('a'.repeat(501))).ok,false);
assert.equal((await request('one\ntwo')).ok,false);
console.log('Live translation, cache, and invalid-input rejection passed.');
