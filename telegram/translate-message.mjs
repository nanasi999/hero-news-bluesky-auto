// Only the already acquired public title is sent to Apps Script.
export async function translateMessage(message, request) {
  const lines = message.split('\n');
  const original = lines.shift().replace(/^新着記事:\s*/, '').trim();
  if (!original || original.length > 500) throw Error('Article title is outside translation limits');
  const result = await request(original);
  if (result?.ok !== true || typeof result.title !== 'string') throw Error('Translation unavailable; article kept for the next run');
  const english = result.title.trim();
  if (!english || english.length > 1500 || /[\r\n\u3040-\u30ff\u3400-\u9fff]/.test(english)) throw Error('Invalid translated title; publication withheld');
  return [english, ...lines].join('\n');
}

export function appsScriptTranslator(endpoint, fetcher = fetch, pause = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  if (!/^https:\/\/script\.google\.com\/macros\/s\/[A-Za-z0-9_-]+\/exec$/.test(endpoint)) throw Error('Invalid translation endpoint');
  return async title => {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
      const url = new URL(endpoint); url.searchParams.set('title', title);
      const response = await fetcher(url.href, {method:'GET', signal:AbortSignal.timeout(60000)});
      if (!response.ok) throw Error();
      const result = await response.json();
      if (attempt === 0 && ['busy','translation_unavailable'].includes(result?.error)) {
        await pause(2000); continue;
      }
      return result;
      } catch {
        if (attempt === 0) { await pause(2000); continue; }
        throw Error('Translation service unavailable; publication withheld');
      }
    }
  };
}
