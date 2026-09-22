import test from 'node:test';
import assert from 'node:assert/strict';
import {articles, relay} from './relay.mjs';
import {translateMessage} from './translate-message.mjs';

globalThis.fetch = async () => { throw Error('Network forbidden in tests'); };
const source = text => ({posts:{synthetic:{stage:'confirmed',platform:'bluesky',identifiers:['https://hero-news.com/archives/123.html'],payload:{text}}}});

test('removing a generated footer preserves footer-like article content', () => {
  const link = 'https://hero-news.com/archives/123.html';
  for (const title of ['Batman\n読む', 'Batman\nhttps://example.invalid/reference',
    'Batman\n\n記事を読む', 'Batman\n#独自タグ',
    '仮面ライダー\n#特撮 #仮面ライダー\n\n記事を読む']) {
    const tags = title.includes('仮面ライダー') ? '#Tokusatsu #KamenRider' : '';
    const footer = tags ? '\n#特撮 #仮面ライダー\n\n記事を読む' : '\n\n記事を読む';
    const input = source('新着記事: ' + title + footer);
    const snapshot = structuredClone(input);
    const expected = ['新着記事: ' + title, tags, link].filter(Boolean).join('\n');
    assert.equal(articles(input)[0].text, expected);
    assert.equal(articles(input)[0].text, expected);
    assert.deepEqual(input, snapshot);
  }
});

test('old and new formats retain every persisted delivery status', async () => {
  for (const status of ['baseline', 'confirmed', 'sending']) {
    const key = articles(source('新着記事: 仮面ライダー\n読む'))[0].key;
    const state = {version:1,channel:'@heronewscom',posts:{activation:{status:'confirmed'},[key]:{status}}};
    const snapshot = structuredClone(state);
    const job = relay({source:source('新着記事: 仮面ライダー\n#特撮 #仮面ライダー\n\n記事を読む'),
      read:async()=>({state,sha:'fixture'}),
      translate:async()=>{throw Error('Unexpected translation')},
      save:async()=>{throw Error('Unexpected write')},send:async()=>{throw Error('Unexpected send')}});
    if (status === 'sending') await assert.rejects(job, /reconciliation/);
    else assert.equal(await job, 0);
    assert.deepEqual(state, snapshot);
  }
});

test('new Bluesky footer never leaks into English Telegram messages', async () => {
  for (const [title, footer, tags] of [
    ['仮面ライダーの話', '\n#特撮 #仮面ライダー', '#Tokusatsu #KamenRider'],
    ['ウルトラマン 戦隊 ゴジラ', '\n#特撮 #ウルトラマン #スーパー戦隊 #ゴジラ', '#Tokusatsu #Ultraman #SuperSentai #Godzilla'],
    ['Batman', '', ''],
  ]) {
    const before = articles(source('新着記事: '+title+'\n読む'))[0];
    const after = articles(source('新着記事: '+title+footer+'\n\n記事を読む'))[0];
    assert.deepEqual(after, before);
    const english = await translateMessage(after.text, async original => {
      assert.equal(original, title);
      return {ok:true,title:'English title'};
    });
    assert.equal(english, ['English title',tags,'https://hero-news.com/archives/123.html'].filter(Boolean).join('\n'));
  }
});

test('existing Telegram state prevents replay after the format change', async () => {
  const oldSource=source('新着記事: 仮面ライダー\n読む');
  const key=articles(oldSource)[0].key;
  const state={version:1,channel:'@heronewscom',posts:{activation:{status:'confirmed'},[key]:{status:'confirmed'}}};
  await relay({source:source('新着記事: 仮面ライダー\n#特撮 #仮面ライダー\n\n記事を読む'),
    read:async()=>({state,sha:'synthetic'}),save:async()=>{throw Error('Unexpected write')},send:async()=>{throw Error('Unexpected resend')}});
});

test('Threads source is not modified by Bluesky footer normalization', () => {
  const ledger=source('新着記事: 仮面ライダー\n#特撮 #仮面ライダー\n\n記事を読む');
  ledger.posts.threads={...ledger.posts.synthetic,platform:'threads',payload:{text:'新着記事: ウルトラマン\nhttps://hero-news.com/archives/123.html'}};
  assert.equal(articles(ledger)[0].text,'新着記事: ウルトラマン\n#Tokusatsu #Ultraman\nhttps://hero-news.com/archives/123.html');
});

test('Threads topic footer is removed once before English Telegram translation', async () => {
  const link='https://hero-news.com/archives/123.html';
  for (const [tag,english] of [['仮面ライダー','#KamenRider'],['ウルトラマン','#Ultraman'],['戦隊','#SuperSentai'],['ゴジラ','#Godzilla'],['ガメラ','#Gamera']]) {
    const title=tag+'の記事';
    const input=source(title+'\n#'+tag+'\n\n'+link);
    input.posts.synthetic.platform='threads';
    input.posts.synthetic.payload.topic_tag=tag;
    const before=structuredClone(input);
    const entry=articles(input)[0];
    assert.equal(entry.text,[title,'#Tokusatsu '+english,link].join('\n'));
    const translated=await translateMessage(entry.text,async original=>{
      assert.equal(original,title);return {ok:true,title:'English title'};
    });
    assert.equal(translated,['English title','#Tokusatsu '+english,link].join('\n'));
    assert.deepEqual(input,before);
  }
});

test('Threads topic cleanup preserves title endings and requires matching metadata', () => {
  const link='https://hero-news.com/archives/123.html';
  for (const title of ['仮面ライダー\n読む','仮面ライダー\nhttps://example.invalid/reference','仮面ライダー\n#仮面ライダー']) {
    const input=source(title+'\n#仮面ライダー\n\n'+link);
    input.posts.synthetic.platform='threads';
    input.posts.synthetic.payload.topic_tag='仮面ライダー';
    assert.equal(articles(input)[0].text,[title,'#Tokusatsu #KamenRider',link].join('\n'));
    delete input.posts.synthetic.payload.topic_tag;
    assert.ok(articles(input)[0].text.startsWith(title+'\n#仮面ライダー'));
  }
});

test('Threads new topic format retains Telegram identity and skips previously sent articles', async () => {
  const link='https://hero-news.com/archives/123.html';
  const old=source('仮面ライダー\n'+link);old.posts.synthetic.platform='threads';
  const current=structuredClone(old);
  current.posts.synthetic.payload={text:'仮面ライダー\n#仮面ライダー\n\n'+link,topic_tag:'仮面ライダー'};
  assert.deepEqual(articles(current),articles(old));
  const key=articles(old)[0].key;
  for (const status of ['confirmed','baseline','sending']) {
    const state={version:1,channel:'@heronewscom',posts:{activation:{status:'confirmed'},[key]:{status}}};
    const job=relay({source:current,read:async()=>({state,sha:'fixture'}),
      translate:async()=>{throw Error('Unexpected translation')},
      save:async()=>{throw Error('Unexpected write')},send:async()=>{throw Error('Unexpected send')}});
    if(status==='sending') await assert.rejects(job,/reconciliation/);
    else assert.equal(await job,0);
  }
});
