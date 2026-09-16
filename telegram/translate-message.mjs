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

export function appsScriptTranslator(endpoint, fetcher = fetch) {
  if (!/^https:\/\/script\.google\.com\/macros\/s\/[A-Za-z0-9_-]+\/exec$/.test(endpoint)) throw Error('Invalid translation endpoint');
  return async title => {
    try {
      const response = await fetcher(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title}), signal:AbortSignal.timeout(30000)});
      if (!response.ok) throw Error();
      return await response.json();
    } catch { throw Error('Translation service unavailable; publication withheld'); }
  };
}
