import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {PAGE, Fault, entries, validate, deliver, graphClient, githubStore} from './relay.mjs';

globalThis.fetch = () => { throw Error('Network forbidden in tests'); };
const copy = value => structuredClone(value);
const item = (n, title = '\u4eee\u9762\u30e9\u30a4\u30c0\u30fc test') => ({
  platform: 'bluesky', stage: 'confirmed', identifiers: [`https://hero-news.com/archives/${n}.html`],
  payload: {text: `\u65b0\u7740\u8a18\u4e8b: ${title}\n#\u7279\u64ae #\u4eee\u9762\u30e9\u30a4\u30c0\u30fc\n\n\u8a18\u4e8b\u3092\u8aad\u3080`, createdAt: new Date(1700000000000 + n * 1000).toISOString()}
});
const ledger = (...rows) => ({posts: Object.fromEntries(rows.map((r, i) => [String(i), r]))});
const blank = () => ({version: 1, page: PAGE, initialized: true, posts: {}, incident: null});
function harness(source = ledger(item(1)), initial = null) {
  let state = copy(initial), sha = initial ? 1 : null, time = 1800000000000;
  const sent = [], verified = [];
  const deps = {
    source: async () => copy(source), read: async () => ({state: copy(state), sha}),
    save: async (old, body) => { assert.equal(old, sha); state = copy(body); sha = (sha || 0) + 1; return sha; },
    health: async () => {}, send: async e => { sent.push(copy(e)); return PAGE + '_' + sent.length; },
    verify: async (id, entry) => { verified.push({id, entry: copy(entry)}); }, now: () => time
  };
  return {deps, sent, verified, state: () => copy(state), advance: () => { time += 7 * 3600000; }, source: s => { source = s; }};
}
test('baseline publishes latest timestamp only, not object order or history', async () => {
  const h = harness(ledger(item(9), item(1), item(5)));
  const result = await deliver(h.deps);
  assert.equal(result.sent, 1); assert.ok(h.sent[0].link.endsWith('/9.html'));
  assert.equal(Object.values(h.state().posts).filter(x => x.status === 'baseline').length, 2);
  await deliver(h.deps); assert.equal(h.sent.length, 1);
});
test('source remains immutable and SNS duplicates merge', () => {
  const blue = item(1), threads = {...copy(blue), platform: 'threads', payload: {text: '\u4eee\u9762\u30e9\u30a4\u30c0\u30fc title\n#\u4eee\u9762\u30e9\u30a4\u30c0\u30fc\n\n' + blue.identifiers[0], topic_tag: '\u4eee\u9762\u30e9\u30a4\u30c0\u30fc'}};
  const source = ledger(blue, threads), before = copy(source);
  const result = entries(source);
  assert.equal(result.length, 1); assert.equal(result[0].message, '\u4eee\u9762\u30e9\u30a4\u30c0\u30fc title'); assert.deepEqual(source, before);
});
test('canonical http and fragment duplicates are collapsed', () => {
  const one = item(1), two = copy(one); two.identifiers = ['http://hero-news.com/archives/1.html#top'];
  assert.equal(entries(ledger(one, two)).length, 1);
});
test('foreign URLs and unconfirmed records are ignored', () => {
  const a = item(1); a.identifiers = ['https://evil.test/1', 'https://hero-news.com@evil.test/1'];
  const b = item(2); b.stage = 'publishing';
  assert.equal(entries(ledger(a, b)).length, 0);
});
test('normal batching keeps remaining entries for next execution', async () => {
  const h = harness(ledger(...Array.from({length: 24}, (_, n) => item(n))), blank());
  assert.equal((await deliver(h.deps)).sent, 10);
  assert.equal((await deliver(h.deps)).sent, 10);
  assert.equal((await deliver(h.deps)).sent, 4);
  assert.equal((await deliver(h.deps)).sent, 0);
  assert.equal(new Set(h.sent.map(e => e.key)).size, 24);
});
test('new article after initialization publishes once', async () => {
  const h = harness(); await deliver(h.deps); h.source(ledger(item(1), item(2)));
  assert.equal((await deliver(h.deps)).sent, 1); await deliver(h.deps); assert.equal(h.sent.length, 2);
});
test('empty or undated initial source cannot baseline away articles', async () => {
  const h = harness(ledger()); const r = await deliver(h.deps);
  assert.equal(r.code, 'SOURCE_EMPTY'); assert.equal(r.notify, true); assert.equal(h.state().initialized, false);
  h.advance(); const a = item(1); delete a.payload.createdAt; h.source(ledger(a));
  await deliver(h.deps); assert.equal(h.sent.length, 0);
});
test('lost publication response is never automatically resent', async () => {
  const h = harness(); h.deps.send = async e => { h.sent.push(e); throw new Fault('FACEBOOK_RESPONSE_UNCERTAIN'); };
  assert.equal((await deliver(h.deps)).notify, true);
  assert.equal((await deliver(h.deps)).notify, false);
  h.advance(); assert.equal((await deliver(h.deps)).code, 'DELIVERY_NEEDS_RECONCILIATION');
  h.advance(); await deliver(h.deps); assert.equal(h.sent.length, 1);
});
test('structured rejection is pending and retried only after cooldown', async () => {
  const h = harness(); const send = h.deps.send; let attempts = 0;
  h.deps.send = async e => { if (!attempts++) throw new Fault('FACEBOOK_AUTH_FAILED', {rejected: true}); return send(e); };
  assert.equal((await deliver(h.deps)).notify, true);
  await deliver(h.deps); assert.equal(attempts, 1);
  h.advance(); assert.equal((await deliver(h.deps)).status, 'healthy'); assert.equal(h.sent.length, 1);
});
test('repeated authentication errors notify once and do not POST', async () => {
  const h = harness(); let checks = 0;
  h.deps.health = async () => { checks++; throw new Fault('FACEBOOK_AUTH_FAILED'); };
  assert.equal((await deliver(h.deps)).notify, true);
  await deliver(h.deps); assert.equal(checks, 1);
  h.advance(); assert.equal((await deliver(h.deps)).notify, false); assert.equal(h.sent.length, 0);
  h.advance(); h.deps.health = async () => {}; assert.equal((await deliver(h.deps)).status, 'healthy');
  assert.equal(h.state().incident, null);
});
test('verification read failure reconciles known ID without resending', async () => {
  const h = harness(); let reads = 0;
  h.deps.verify = async () => { if (!reads++) throw new Fault('FACEBOOK_VERIFY_FAILED'); };
  await deliver(h.deps); assert.equal(h.sent.length, 1);
  h.advance(); assert.equal((await deliver(h.deps)).status, 'healthy'); assert.equal(h.sent.length, 1);
});
for (const position of [1, 2, 3, 4]) {
  for (const committed of [false, true]) {
    test(`checkpoint ${position} interruption committed=${committed} never duplicates`, async () => {
      const h = harness(); const save = h.deps.save; let calls = 0;
      h.deps.save = async (...args) => {
        if (++calls === position) { if (committed) await save(...args); throw new Fault('STATE_CHECKPOINT_FAILED'); }
        return save(...args);
      };
      try { await deliver(h.deps); } catch {}
      h.deps.save = save; h.advance(); await deliver(h.deps); h.advance(); await deliver(h.deps);
      assert.ok(h.sent.length <= 1);
    });
  }
}
test('concurrent reservation conflict cannot cause a second POST', async () => {
  const h = harness(ledger(item(1)), blank());
  await Promise.allSettled([deliver(h.deps), deliver(h.deps)]);
  assert.equal(h.sent.length, 1);
});
for (const state of [{...blank(), page: 'other'}, {...blank(), initialized: undefined}, {...blank(), posts: []}, {...blank(), incident: {code: 'token secret', retryAt: 0}}]) {
  test('invalid state fails closed: ' + JSON.stringify(state), () => assert.throws(() => validate(state)));
}
const response = (body, status = 200) => ({ok: status < 300, status, json: async () => body});
test('Graph adapter exact request contract, fixed destination, no token URL', async () => {
  const calls = [], token = 'FAKE_TEST_TOKEN';
  const api = graphClient(token, async (url, options) => {
    calls.push({url, options});
    return response(url.includes('/me?') ? {id: PAGE} : options.method === 'POST' ? {id: PAGE + '_55'} : {id: PAGE + '_55', message: 'Title'});
  });
  await api.health(); const id = await api.send({message: 'Title', link: 'https://hero-news.com/archives/1.html'}); await api.verify(id, {message: 'Title'});
  assert.equal(calls.length, 3);
  assert.equal(calls[1].url, 'https://graph.facebook.com/v25.0/' + PAGE + '/feed');
  assert.deepEqual(Object.fromEntries(new URLSearchParams(calls[1].options.body)), {message: 'Title', link: 'https://hero-news.com/archives/1.html'});
  for (const c of calls) { assert.ok(!c.url.includes(token)); assert.equal(c.options.redirect, 'error'); assert.equal(c.options.headers.Authorization, 'Bearer ' + token); }
});
test('wrong Page prevents publishing', async () => {
  const api = graphClient('FAKE', async () => response({id: 'another'}));
  await assert.rejects(api.health, /PAGE_MISMATCH/);
});
test('POST transport errors are sanitized and never retried', async () => {
  let calls = 0; const api = graphClient('FAKE_SECRET', async () => { calls++; throw Error('FAKE_SECRET remote body'); });
  await assert.rejects(() => api.send({message: 'x', link: 'https://hero-news.com/1'}), /RESPONSE_UNCERTAIN/); assert.equal(calls, 1);
});
test('GET transient failure retries boundedly', async () => {
  let calls = 0; const api = graphClient('FAKE', async () => { if (++calls < 3) throw Error(); return response({id: PAGE}); }, async () => {});
  await api.health(); assert.equal(calls, 3);
});
for (const [status, body, rejected] of [[400, {error: {code: 190}}, true], [429, {error: {code: 4}}, true], [400, {error: {code: 2, is_transient: true}}, false], [500, {error: {code: 2}}, false], [400, {}, false], [200, {id: 'wrong'}, false]]) {
  test('Graph status ' + status + ' classification ' + JSON.stringify(body), async () => {
    const api = graphClient('FAKE', async () => response(body, status));
    await assert.rejects(() => api.send({message: 'x', link: 'https://hero-news.com/1'}), e => e instanceof Fault && e.rejected === rejected);
  });
}
test('readback mismatched body never confirms', async () => {
  const api = graphClient('FAKE', async () => response({id: PAGE + '_1', message: 'other'}));
  await assert.rejects(() => api.verify(PAGE + '_1', {message: 'expected'}), /VERIFY_FAILED/);
});
test('GitHub checkpoint reconciles a lost accepted PUT without repeating it', async () => {
  let stored = null, writes = 0;
  const store = githubStore('FAKE', async (url, options) => {
    if (options.method === 'PUT') { writes++; stored = JSON.parse(options.body); throw Error('lost response'); }
    if (url.includes('/git/ref/')) return response({object: {sha: 'a'.repeat(40)}});
    return response({encoding: 'base64', sha: 'b'.repeat(40), content: stored.content});
  }, async () => {});
  assert.equal(await store.save(null, blank()), 'b'.repeat(40)); assert.equal(writes, 1);
});
test('GitHub checkpoint refuses newer unrelated state', async () => {
  const store = githubStore('FAKE', async (url, options) => {
    if (options.method === 'PUT') return response({}, 409);
    if (url.includes('/git/ref/')) return response({object: {sha: 'a'.repeat(40)}});
    return response({encoding: 'base64', sha: 'c'.repeat(40), content: Buffer.from(JSON.stringify({...blank(), initialized: false})).toString('base64')});
  }, async () => {});
  await assert.rejects(() => store.save('b'.repeat(40), blank()), /STATE_CONFLICT/);
});
test('workflow has dedicated concurrency, tests before secrets, no cron or shared writes', () => {
  const yml = readFileSync(new URL('../.github/workflows/facebook-relay.yml', import.meta.url), 'utf8');
  assert.match(yml, /group: facebook-heronews/); assert.match(yml, /cancel-in-progress: false/);
  assert.ok(yml.indexOf('node --test') < yml.indexOf('FACEBOOK_PAGE_ACCESS_TOKEN'));
  assert.ok(!yml.includes('schedule:')); assert.match(yml, /workflow_run:/);
});
test('operational recovery does not clear incident after health alone', async () => {
  const h = harness(); h.deps.health = async () => { throw new Fault('FACEBOOK_AUTH_FAILED'); };
  await deliver(h.deps); h.advance(); h.deps.health = async () => {};
  h.deps.source = async () => { throw new Fault('SOURCE_MISSING'); };
  const result = await deliver(h.deps);
  assert.equal(result.status, 'paused'); assert.equal(result.notify, false); assert.equal(h.state().incident.code, 'SOURCE_MISSING');
});
test('one incident is alerted again only after a complete recovery', async () => {
  const h = harness(); const health = h.deps.health;
  h.deps.health = async () => { throw new Fault('FACEBOOK_AUTH_FAILED'); };
  assert.equal((await deliver(h.deps)).notify, true);
  h.advance(); h.deps.health = health; await deliver(h.deps);
  h.deps.health = async () => { throw new Fault('FACEBOOK_AUTH_FAILED'); };
  assert.equal((await deliver(h.deps)).notify, true);
});
test('known ambiguous record never blocks independent new article', async () => {
  const h = harness(); const send = h.deps.send;
  h.deps.send = async e => { h.sent.push(e); throw new Fault('FACEBOOK_RESPONSE_UNCERTAIN'); };
  await deliver(h.deps); h.advance(); h.deps.send = send; h.source(ledger(item(1), item(2)));
  const result = await deliver(h.deps);
  assert.equal(result.sent, 1); assert.equal(result.status, 'paused'); assert.equal(h.sent.length, 2);
  h.advance(); await deliver(h.deps); assert.equal(h.sent.length, 2);
});
test('all five article families remain Japanese without generated English tags', () => {
  for (const name of ['\u4eee\u9762\u30e9\u30a4\u30c0\u30fc','\u30a6\u30eb\u30c8\u30e9\u30de\u30f3','\u6226\u968a','\u30b4\u30b8\u30e9','\u30ac\u30e1\u30e9']) {
    const row = item(1, name); row.payload.text = `\u65b0\u7740\u8a18\u4e8b: ${name}\n\n\u8a18\u4e8b\u3092\u8aad\u3080`;
    assert.equal(entries(ledger(row))[0].message, name);
  }
});
test('empty secret and secret with whitespace fail without disclosing it', () => {
  for (const value of ['', undefined, 'FAKE SECRET']) assert.throws(() => graphClient(value), e => e.message === 'FACEBOOK_SECRET_MISSING');
});
test('state writes are restricted to Facebook file, not shared ledger or Telegram', async () => {
  const urls = [];
  const store = githubStore('FAKE', async (url, options) => {
    urls.push({url, method: options.method});
    return response({content: {sha: 'a'.repeat(40)}});
  });
  await store.save(null, blank());
  assert.deepEqual(urls, [{url: 'https://api.github.com/repos/nanasi999/hero-news-bluesky-auto/contents/facebook-state.json', method: 'PUT'}]);
});
