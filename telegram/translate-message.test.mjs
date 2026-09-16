import test from 'node:test';
import assert from 'node:assert/strict';
import {translateMessage,appsScriptTranslator} from './translate-message.mjs';
test('only title is translated; original tags and URL retained',async()=>{
  const text='ウルトラマンの映画\n#Tokusatsu #Ultraman\nhttps://hero-news.com/archives/123.html';
  let sent;const result=await translateMessage(text,async title=>{sent=title;return {ok:true,title:'An Ultraman movie'}});
  assert.equal(sent,'ウルトラマンの映画');assert.equal(result,'An Ultraman movie\n#Tokusatsu #Ultraman\nhttps://hero-news.com/archives/123.html');
});
test('American comics gain no tokusatsu tag',async()=>assert.equal(await translateMessage('バットマン\nhttps://hero-news.com/archives/123.html',async()=>({ok:true,title:'Batman'})),'Batman\nhttps://hero-news.com/archives/123.html'));
test('failed or malformed translation never published as English',async()=>{
  for(const value of [{ok:false},{ok:true,title:''},{ok:true,title:'日本語'},{ok:true,title:'one\ntwo'},{ok:true,title:'a'.repeat(1501)}]) await assert.rejects(translateMessage('仮面ライダー\nurl',async()=>value));
});
test('request contains no token or article body',async()=>{
  let body;const fn=appsScriptTranslator('https://script.google.com/macros/s/test/exec',async(url,options)=>{body=JSON.parse(options.body);return {ok:true,json:async()=>({ok:true,title:'Test'})}});await fn('題名');assert.deepEqual(body,{title:'題名'});
});
test('temporary service failures retry without publishing; quota refusal does not retry',async()=>{
  let attempts=0;
  const fn=appsScriptTranslator('https://script.google.com/macros/s/test/exec',async()=>({ok:true,json:async()=>++attempts===1?{ok:false,error:'busy'}:{ok:true,title:'English'}}),async()=>{});
  assert.equal((await fn('題名')).title,'English');assert.equal(attempts,2);
  attempts=0;
  const capped=appsScriptTranslator('https://script.google.com/macros/s/test/exec',async()=>{attempts++;return {ok:true,json:async()=>({ok:false,error:'daily_limit'})}},async()=>{});
  assert.equal((await capped('題名')).error,'daily_limit');assert.equal(attempts,1);
});
test('transport errors and login pages withheld',async()=>{for(const fetcher of [async()=>{throw Error('network')},async()=>({ok:true,json:async()=>{throw Error('HTML')}})])await assert.rejects(appsScriptTranslator('https://script.google.com/macros/s/test/exec',fetcher)('題名'),/unavailable/)});
