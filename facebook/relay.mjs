import {createHash} from 'node:crypto';
import {appendFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {articles} from '../telegram/relay.mjs';

export const PAGE = '1366592206538225';
const REPO = 'nanasi999/hero-news-bluesky-auto';
const BRANCH = 'actions-state/social-posting';
const STATE = 'facebook-state.json';
const EXPIRES = 1795733588;
const hash = value => createHash('sha256').update(value).digest('hex');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

export class Fault extends Error {
  constructor(code, {rejected = false} = {}) { super(code); this.code = code; this.rejected = rejected; }
}
const faultCode = e => e instanceof Fault ? e.code : 'INTERNAL_FAILURE';

export function entries(source) {
  const result = articles(source).map(entry => {
    const lines = entry.text.split('\n');
    const link = lines.pop();
    if (/^#Tokusatsu(?: #(?:Ultraman|KamenRider|SuperSentai|Godzilla|Gamera)){1,3}$/.test(lines.at(-1) || '')) lines.pop();
    const message = lines.join('\n').replace(/^新着記事:\s*/, '').trim();
    if (!message || message.length > 5000) throw new Fault('INVALID_ARTICLE');
    return {key: entry.key, message, link};
  });
  // Ordering from a JSON object is not a publication date. Use verified timestamps.
  const times = new Map();
  for (const row of Object.values(source.posts)) {
    if (row.platform !== 'bluesky' || row.stage !== 'confirmed') continue;
    const time = Date.parse(row.payload?.createdAt);
    if (!Number.isFinite(time)) continue;
    for (const id of row.identifiers || []) {
      try { const url = new URL(id); url.protocol = 'https:'; url.hash = ''; times.set(hash(url.href), time); } catch {}
    }
  }
  return result.map(e => ({...e, time: times.get(e.key) || 0})).sort((a, b) => a.time - b.time || a.key.localeCompare(b.key));
}

export function validate(state) {
  if (!state || state.version !== 1 || state.page !== PAGE || !state.posts ||
      typeof state.posts !== 'object' || Array.isArray(state.posts) || typeof state.initialized !== 'boolean') throw new Fault('INVALID_STATE');
  for (const [key, row] of Object.entries(state.posts)) {
    if (!/^[a-f0-9]{64}$/.test(key) || !row || !['baseline', 'pending', 'sending', 'confirmed'].includes(row.status)) throw new Fault('INVALID_STATE');
    if (row.status === 'confirmed' && !new RegExp('^' + PAGE + '_\\d+$').test(row.id || '')) throw new Fault('INVALID_STATE');
  }
  if (state.incident !== null && (!state.incident || !/^[A-Z0-9_]+$/.test(state.incident.code) ||
      !Number.isFinite(state.incident.retryAt))) throw new Fault('INVALID_STATE');
}

export async function deliver({source, read, save, health, send, verify, now = () => Date.now()}) {
  let {state, sha} = await read();
  const first = state === null;
  if (first) state = {version: 1, page: PAGE, posts: {}, incident: null, initialized: false};
  validate(state);
  if (state.incident && state.incident.retryAt > now()) {
    return {status: 'paused', sent: 0, code: state.incident.code, notify: false};
  }
  const checkpoint = async () => { sha = await save(sha, state); };
  let sent = 0;
  try {
    await health();
    const candidates = entries(await source());
    if (!state.initialized) {
      if (!candidates.length || !candidates.some(e => e.time > 0)) throw new Fault('SOURCE_EMPTY');
      for (const e of candidates) state.posts[e.key] = {status: 'baseline'};
      const latest = candidates.at(-1);
      state.posts[latest.key] = {status: 'pending'};
      state.initialized = true;
      await checkpoint();
    }
    // A known post ID can be reconciled by reading; this never repeats a POST.
    for (const row of Object.values(state.posts)) {
      if (row.status !== 'sending' || !row.id) continue;
      await verify(row.id, row);
      row.status = 'confirmed';
      await checkpoint();
    }
    for (const entry of candidates) {
      if (sent >= 10) break;
      const existing = state.posts[entry.key];
      if (existing && existing.status !== 'pending') continue;
      state.posts[entry.key] = {status: 'sending', link: entry.link, message: entry.message};
      await checkpoint();
      let id;
      try { id = await send(entry); }
      catch (e) {
        // Only an explicit rejection proves that this attempt did not publish.
        if (e instanceof Fault && e.rejected) state.posts[entry.key].status = 'pending';
        throw e;
      }
      state.posts[entry.key].id = id;
      await checkpoint();
      await verify(id, entry);
      state.posts[entry.key] = {status: 'confirmed', id};
      await checkpoint();
      sent++;
    }
    if (Object.values(state.posts).some(row => row.status === 'sending')) throw new Fault('DELIVERY_NEEDS_RECONCILIATION');
    if (state.incident) { state.incident = null; await checkpoint(); }
    return {status: 'healthy', sent, notify: false};
  } catch (e) {
    const code = faultCode(e);
    const notify = !state.incident;
    state.incident = {code, retryAt: now() + (code.includes('AUTH') ? 6 * 3600000 : 30 * 60000)};
    // A checkpoint failure must not be replaced by a success result.
    await checkpoint();
    return {status: 'paused', code, sent, notify};
  }
}

export function graphClient(token, fetcher = fetch, sleep = pause) {
  if (typeof token !== 'string' || !token.trim() || /\s/.test(token)) throw new Fault('FACEBOOK_SECRET_MISSING');
  async function request(method, path, data) {
    const attempts = method === 'GET' ? 3 : 1;
    for (let n = 0; n < attempts; n++) {
      try {
        const res = await fetcher('https://graph.facebook.com/v25.0/' + path, {
          method, headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/x-www-form-urlencoded'},
          body: data ? new URLSearchParams(data).toString() : undefined,
          signal: AbortSignal.timeout(30000), redirect: 'error'
        });
        let body;
        try { body = await res.json(); } catch { throw new Fault('FACEBOOK_RESPONSE_UNCERTAIN'); }
        if (!res.ok || body?.error) {
          const code = body?.error?.code;
          const auth = code === 190 || res.status === 401;
          const rejected = [400, 401, 403, 429].includes(res.status) && Number.isInteger(code) && body.error.is_transient !== true;
          throw new Fault(auth ? 'FACEBOOK_AUTH_FAILED' : 'FACEBOOK_API_REJECTED', {rejected});
        }
        if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Fault('FACEBOOK_RESPONSE_UNCERTAIN');
        return body;
      } catch (e) {
        if (n + 1 === attempts || (e instanceof Fault && e.rejected)) throw e instanceof Fault ? e : new Fault('FACEBOOK_RESPONSE_UNCERTAIN');
        await sleep(1000 * 2 ** n);
      }
    }
  }
  return {
    health: async () => {
      const result = await request('GET', 'me?fields=id,name');
      if (result.id !== PAGE) throw new Fault('FACEBOOK_PAGE_MISMATCH');
    },
    send: async entry => {
      const result = await request('POST', PAGE + '/feed', {message: entry.message, link: entry.link});
      if (!new RegExp('^' + PAGE + '_\\d+$').test(result.id || '')) throw new Fault('FACEBOOK_RESPONSE_UNCERTAIN');
      return result.id;
    },
    verify: async (id, entry) => {
      if (!new RegExp('^' + PAGE + '_\\d+$').test(id || '')) throw new Fault('INVALID_POST_ID');
      const result = await request('GET', id + '?fields=id,message');
      if (result.id !== id || result.message !== entry.message) throw new Fault('FACEBOOK_VERIFY_FAILED');
    }
  };
}

export function githubStore(token, fetcher = fetch, sleep = pause) {
  if (!token) throw new Fault('GITHUB_SECRET_MISSING');
  async function request(path, method = 'GET', data) {
    for (let n = 0; n < (method === 'GET' ? 3 : 1); n++) {
      try {
        const res = await fetcher('https://api.github.com/repos/' + REPO + path, {
          method, headers: {Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json', 'Cache-Control': 'no-cache'},
          body: data ? JSON.stringify(data) : undefined, signal: AbortSignal.timeout(30000), redirect: 'error'
        });
        if (res.status === 404 && method === 'GET') return null;
        if (!res.ok) throw new Fault('GITHUB_HTTP_' + res.status);
        return await res.json();
      } catch (e) {
        if (method !== 'GET' || n === 2) throw e instanceof Fault ? e : new Fault('GITHUB_TRANSPORT_FAILED');
        await sleep(1000 * 2 ** n);
      }
    }
  }
  async function readFile(path) {
    if (![STATE, 'test-ledger.json'].includes(path)) throw new Fault('INVALID_STATE_PATH');
    const ref = await request('/git/ref/heads/' + BRANCH);
    if (!/^[a-f0-9]{40}$/.test(ref?.object?.sha || '')) throw new Fault('STATE_BRANCH_UNAVAILABLE');
    const file = await request('/contents/' + path + '?ref=' + ref.object.sha);
    if (!file) return {state: null, sha: null};
    if (file.encoding !== 'base64' || !/^[a-f0-9]{40}$/.test(file.sha || '')) throw new Fault('INVALID_STATE_FILE');
    try { return {state: JSON.parse(Buffer.from(file.content, 'base64').toString('utf8')), sha: file.sha}; }
    catch { throw new Fault('INVALID_STATE_FILE'); }
  }
  return {
    read: () => readFile(STATE),
    source: async () => {
      const {state} = await readFile('test-ledger.json');
      if (!state) throw new Fault('SOURCE_MISSING');
      return state;
    },
    save: async (sha, state) => {
      validate(state);
      const serialized = JSON.stringify(state);
      for (let n = 0; n < 3; n++) {
        try {
          const saved = await request('/contents/' + STATE, 'PUT', {
            branch: BRANCH, ...(sha ? {sha} : {}), message: 'Checkpoint Facebook delivery',
            content: Buffer.from(serialized).toString('base64')
          });
          if (/^[a-f0-9]{40}$/.test(saved?.content?.sha || '')) return saved.content.sha;
        } catch {}
        // A lost PUT response may follow a committed write. Compare, never overwrite.
        const actual = await readFile(STATE);
        if (JSON.stringify(actual.state) === serialized) return actual.sha;
        if (actual.sha !== sha) throw new Fault('STATE_CONFLICT');
        if (n < 2) await sleep(1000 * 2 ** n);
      }
      throw new Fault('STATE_CHECKPOINT_FAILED');
    }
  };
}

async function main() {
  if (process.env.GITHUB_ACTIONS !== 'true' || process.env.GITHUB_REPOSITORY !== REPO || process.env.GITHUB_REF !== 'refs/heads/main') throw new Fault('UNAUTHORIZED_RUNNER');
  const store = githubStore(process.env.GH_STATE_TOKEN);
  // Delay credential validation until after state loading so a missing secret is deduplicated.
  let api;
  const result = await deliver({...store,
    health: async () => { api = graphClient(process.env.FACEBOOK_PAGE_ACCESS_TOKEN); await api.health(); },
    send: entry => api.send(entry), verify: (id, entry) => api.verify(id, entry)
  });
  const days = Math.floor((EXPIRES * 1000 - Date.now()) / 86400000);
  const summary = `Facebook: ${result.status}; confirmed this run: ${result.sent}; ${result.code || 'no incident'}.`;
  console.log(summary);
  if (process.env.GITHUB_STEP_SUMMARY) await appendFile(process.env.GITHUB_STEP_SUMMARY,
    `## Facebook delivery\n${summary}\n\n${result.status === 'paused' ? 'Publication is paused or requires reconciliation. This is NOT a successful delivery. Repeated incident emails are suppressed.\n' : ''}Token expiry recorded at setup: 2026-11-27. ${days <= 14 ? '**Renew the Page token now.**' : ''}\n`);
  if (result.notify) { console.error('::error::Facebook delivery paused: ' + result.code); process.exitCode = 1; }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(e => { console.error('Facebook stopped safely: ' + faultCode(e)); process.exitCode = 1; });
}
