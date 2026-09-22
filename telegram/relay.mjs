import {translateMessage, appsScriptTranslator} from './translate-message.mjs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const repo = 'nanasi999/hero-news-bluesky-auto';
const branch = 'actions-state/social-posting';
const channel = '@heronewscom';
const statePath = 'telegram-state.json';
const hash = text => createHash('sha256').update(text).digest('hex');

export function articleTags(title) {
  const tags = [];
  if (/ウルトラマン|ウルトラセブン|\bUltraman\b/i.test(title)) tags.push('#Ultraman');
  if (/仮面ライダー|\bKamen\s*Rider\b/i.test(title)) tags.push('#KamenRider');
  if (/スーパー戦隊|戦隊|\bSuper\s*Sentai\b/i.test(title)) tags.push('#SuperSentai');
  if (/ゴジラ|\bGodzilla\b/i.test(title)) tags.push('#Godzilla');
  if (/ガメラ|\bGamera\b/i.test(title)) tags.push('#Gamera');
  return tags.length ? ['#Tokusatsu', ...tags].slice(0, 4) : [];
}

export function articles(ledger) {
  if (!ledger || !ledger.posts || Array.isArray(ledger.posts)) throw Error('Invalid source ledger');
  const found = new Map();
  for (const row of Object.values(ledger.posts)) {
    if (row.stage !== 'confirmed' || !['threads', 'bluesky'].includes(row.platform)) continue;
    const links = (row.identifiers || []).filter(x => {
      try { const u = new URL(x); return u.hostname === 'hero-news.com' && !u.username && !u.password && !u.port && ['https:', 'http:'].includes(u.protocol) && (u.pathname !== '/' || /^\d+$/.test(u.searchParams.get('p') || '')); } catch { return false; }
    });
    if (!links.length) continue;
    const u = new URL(links.find(x => new URL(x).pathname !== '/') || links[0]); u.protocol = 'https:'; u.hash = '';
    const link = u.href;
    let text = row.payload?.text;
    if (typeof text !== 'string' || !text.trim()) throw Error('Missing article text');
    // Remove only the generated Bluesky footer, not article content or Threads text.
    if (row.platform === 'bluesky') text = text.replace(/(?:\n#特撮(?: #(?:ウルトラマン|仮面ライダー|スーパー戦隊|ゴジラ|ガメラ)){1,3})?\n\n記事を読む\s*$/, '');
    text = text.replace(/\n読む\s*$/, '').replace(/\nhttps?:\/\/\S+\s*$/, '').trim();
    const key = hash(link);
    if (!found.has(key) || row.platform === 'threads') found.set(key, {key, text: [text.slice(0, 3500), articleTags(text).join(' '), link].filter(Boolean).join('\n')});
  }
  return [...found.values()];
}

export async function relay({source, read, save, send, pause = async () => {}, translate = async text => text}) {
  const entries = articles(source);
  let {state, sha} = await read();
  if (state === null) {
    state = {version: 1, channel, posts: Object.fromEntries(entries.map(e => [e.key, {status: 'baseline'}]))};
    sha = await save(sha, state);
  }
  if (state.version !== 1 || state.channel !== channel || !state.posts || Array.isArray(state.posts)
      || Object.values(state.posts).some(p => !p || !['baseline', 'sending', 'confirmed'].includes(p.status))) throw Error('Invalid Telegram state');
  const welcome = {key: 'activation', text: entries.at(-1)?.text || 'ヒーローNEWSの新着記事の自動投稿を開始しました。'};
  let sent = 0;
  let uncertain = Object.values(state.posts).some(p => p.status === 'sending');
  for (const entry of [welcome, ...entries]) {
    if (state.posts[entry.key]) continue;
    if (sent >= 20) break;
    const translatedText = await translate(entry.text);
    state.posts[entry.key] = {status: 'sending'};
    sha = await save(sha, state); // Durable reservation BEFORE any publication.
    const id = await send(translatedText); // Never retry an ambiguous publication.
    state.posts[entry.key] = {status: 'confirmed', message_id: id};
    sha = await save(sha, state);
    sent++;
    await pause();
  }
  if (uncertain) throw Error('A previous Telegram delivery needs reconciliation; it was not resent');
  return sent;
}

async function main() {
  const gh = process.env.GH_STATE_TOKEN, token = process.env.TELEGRAM_BOT_TOKEN;
  if (!gh || !token) throw Error('Required GitHub or Telegram secret missing');
  if (process.env.GITHUB_ACTIONS !== 'true' || process.env.GITHUB_REPOSITORY !== repo || process.env.GITHUB_REF !== 'refs/heads/main') throw Error('Only the authorized main Actions runner may publish');
  async function github(path, method = 'GET', body) {
    let res;
    try { res = await fetch('https://api.github.com/repos/' + repo + path, {method, headers: {Authorization: 'Bearer ' + gh, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(30000), redirect: 'error'}); } catch { throw Error('GitHub transport failed'); }
    if (res.status === 404) return null;
    if (!res.ok) throw Error('GitHub HTTP ' + res.status);
    return res.json();
  }
  async function readFile(path) {
    const ref = await github('/git/ref/heads/' + branch);
    const sha = ref?.object?.sha;
    if (!/^[a-f0-9]{40}$/.test(sha || '')) throw Error('Cannot verify state branch');
    const file = await github('/contents/' + path + '?ref=' + sha);
    if (!file) return null;
    if (file.encoding !== 'base64') throw Error('Invalid stored encoding');
    return {sha: file.sha, state: JSON.parse(Buffer.from(file.content, 'base64').toString('utf8'))};
  }
  async function telegram(method, data) {
    let res, body;
    try {
      res = await fetch('https://api.telegram.org/bot' + token + '/' + method, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data), signal: AbortSignal.timeout(30000), redirect: 'error'});
      body = await res.json();
    } catch { throw Error('Telegram response uncertain; no automatic resend'); }
    if (!res.ok || body.ok !== true) throw Error('Telegram request rejected: HTTP ' + res.status);
    return body.result;
  }
  const me = await telegram('getMe', {});
  if (me.username !== 'heronewscom_notify_bot') throw Error('Unexpected bot identity');
  const chat = await telegram('getChat', {chat_id: channel});
  if (chat.type !== 'channel' || chat.username?.toLowerCase() !== channel.slice(1)) throw Error('Unexpected channel');
  const member = await telegram('getChatMember', {chat_id: chat.id, user_id: me.id});
  if (member.status !== 'administrator' || member.can_post_messages !== true) throw Error('Bot needs posting permission');
  const ledger = await readFile('test-ledger.json');
  if (!ledger) throw Error('Source ledger missing');
  const translator = appsScriptTranslator(process.env.TITLE_TRANSLATION_URL || '');
  const count = await relay({source: ledger.state,
    translate: text => translateMessage(text, translator),
    read: async () => await readFile(statePath) || {sha: null, state: null},
    save: async (sha, state) => {
      const saved = await github('/contents/' + statePath, 'PUT', {branch, ...(sha ? {sha} : {}), message: 'Persist Telegram delivery state', content: Buffer.from(JSON.stringify(state)).toString('base64')});
      if (!saved?.content?.sha) throw Error('Telegram checkpoint not confirmed');
      return saved.content.sha;
    },
    send: async text => {
      const result = await telegram('sendMessage', {chat_id: chat.id, text, link_preview_options: {is_disabled: true}});
      if (!Number.isSafeInteger(result?.message_id) || result?.chat?.id !== chat.id) throw Error('Telegram delivery result uncertain');
      return result.message_id;
    },
    pause: () => new Promise(resolve => setTimeout(resolve, 1100))
  });
  console.log('Telegram delivery verified: ' + count + ' new messages. No article-site requests.');
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => { console.error(error.message.replace(/\d{6,}:[A-Za-z0-9_-]+/g, '[REDACTED]')); process.exitCode = 1; });
}
