// Optional: fixes the transcript with Claude (your own API key, stored only on this device).
// Claude can't listen to audio, so speech recognition still runs on the device; Claude
// corrects the words using context and labels each line's language.
export const CLAUDE_MODELS = {
  'claude-sonnet-5-5': 'Sonnet 5.5 (best)',
  'claude-haiku-5-5': 'Haiku 5.5 (cheaper, faster)',
};
const KEY = 'claudeApiKey';
export const getKey = () => { try { return localStorage.getItem(KEY) || ''; } catch { return ''; } };
// Pasted keys sometimes carry spaces, line breaks or quotes; keep only the key itself.
export const cleanKey = (k) => (k || '').replace(/[\s"'“”‘’]/g, '');
export const setKey = (k) => { try { k ? localStorage.setItem(KEY, cleanKey(k)) : localStorage.removeItem(KEY); } catch {} };
/** Non-secret facts about a key's shape, for the diagnostics log. */
export function keyShape(raw) {
  const k = cleanKey(raw);
  return {
    length: k.length,
    start: k.slice(0, 10),               // e.g. "sk-ant-api" — not secret
    hadSpacesOrQuotes: (raw || '').length !== k.length,
    looksLikeApiKey: /^sk-ant-api\d\d-/.test(k),
    looksLikeAdminKey: k.startsWith('sk-ant-admin'),
    looksLikeOpenAIKey: /^sk-(proj-|svcacct-|[A-Za-z0-9]{20,}$)/.test(k) && !k.startsWith('sk-ant'),
  };
}

async function call(key, model, system, user, maxTokens) {
  let res;
  try {
    res = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-api-key': key,
        'anthropic-version': '2023-06-01',
        'anthropic-dangerous-direct-browser-access': 'true',
      },
      body: JSON.stringify({ model, max_tokens: maxTokens, system, messages: [{ role: 'user', content: user }] }),
    });
  } catch (e) {
    throw new Error('Could not reach Claude (check the internet connection)');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data?.error?.message || `HTTP ${res.status}`;
    if (res.status === 401) throw new Error(`The API key was rejected (${msg}) — check it in the Claude settings`);
    if (res.status === 429) throw new Error('Too many requests or credit limit reached: ' + msg);
    throw new Error(`Claude error: ${msg}`);
  }
  const text = (data.content || []).filter(b => b.type === 'text').map(b => b.text).join('\n');
  return { text, usage: data.usage || {} };
}

export async function testKey(key, model) {
  const r = await call(key, model, 'Reply with exactly: OK', 'Say OK.', 5);
  return r.text.trim();
}

/**
 * Returns corrected lines, each starting with a language tag like "[de] ".
 * names: { primary: 'Turkish', secondary: 'German' } for the prompt.
 */
export async function fixTranscript({ key, model, primary, secondary, names, glossary, transcript }) {
  const langs = secondary ? `${names.primary} [${primary}] and ${names.secondary} [${secondary}]` : `${names.primary} [${primary}]`;
  const system = 'You correct automatic speech-recognition transcripts of short language-teaching videos. ' +
    'You only fix misrecognized words; you never translate, summarize, or add content.';
  const user =
    `The video is spoken in ${langs}. Below is the automatic transcript, one line per line. ` +
    `Speech recognition often misheard words, especially when the language switches mid-sentence ` +
    `(it may have written ${secondary ? names.secondary : 'some'} words as gibberish in the other language). ` +
    `Use the context to restore what was actually said.\n\n` +
    `Rules:\n` +
    `- Keep the speaker's words and their order. Don't translate, don't add or remove sentences.\n` +
    `- One line per sentence; start a new line wherever the language changes.\n` +
    `- Start every line with its language tag: ${secondary ? `[${primary}] or [${secondary}]` : `[${primary}]`}.\n` +
    `- If Whisper repeated a word many times, keep it once.\n` +
    `- Reply with only the corrected lines, nothing else.` +
    (glossary?.trim() ? `\n\nWords taught in this video:\n${glossary.trim()}` : '') +
    `\n\nTranscript:\n${transcript}`;
  const maxTokens = Math.min(16000, Math.round(transcript.length / 2) + 1500);
  const r = await call(key, model, system, user, maxTokens);
  const text = r.text.replace(/^```[a-z]*\n?|```$/gim, '').trim();
  return { text, usage: r.usage };
}
