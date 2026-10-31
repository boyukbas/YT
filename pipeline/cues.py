"""Claude turns the raw Whisper passes into proofread, timed, bilingual subtitle cues and an edit list
(where each clip starts/ends, retakes to cut). Then the edit list becomes the output timeline."""
import json

GAP_MAX, GAP_KEEP = 1.5, 0.8        # silences longer than GAP_MAX between cues shrink to GAP_KEEP

PROMPT = """You prepare subtitles for a YouTube lesson of the channel "{channel}": German lessons for Turkish speakers.
The presenter speaks mostly {primary_name} and says German example sentences. Below are {n} raw clip(s) in recording order,
each transcribed by Whisper three times: forced Turkish (TR, the word timings to use), forced German (DE, spells German parts
right), and auto-detected (AUTO). Whisper makes mistakes in every pass; combine them and your own knowledge into what she
really says.

Known terms (spell exactly like this): {terms}

Return ONE JSON object, nothing else:
{{
 "order": ["c1", ...],                  // clip order for the video (normally recording order; change only if clearly wrong)
 "clips": [{{
   "id": "c1",
   "in": 0.85, "out": 31.7,             // keep from just before her first word (~0.3 s) to after her last (~0.5 s);
                                        // drop the start/stop of the recording and anything that is not the lesson
   "cuts": [[12.1, 15.3, "false start, she repeats the sentence"]],   // ranges to remove inside the clip:
                                        // ONLY retakes/false starts/abandoned sentences and long hesitations.
                                        // NEVER cut deliberate repetitions: teachers say an example twice on purpose.
   "short_cuts": [[72.2, 76.4, "side remark"]],   // ONLY if all clips together are longer than {short_max} s: extra
                                        // ranges to leave out of the Short version so it ends up under {short_max} s
                                        // (side remarks, second repetitions, "next video" teasers). [] otherwise.
   "cues": [{{"s": 1.27, "e": 4.10, "lang": "tr", "text": "...", "de": "German translation of this Turkish line"}},
            {{"s": 4.2, "e": 7.0, "lang": "de", "text": "Ich fange an, Deutsch zu lernen.", "tr": "Almanca öğrenmeye başlıyorum."}}]
 }}],
 "vocab": [{{"clip": "c1", "s": 61.1, "word": "die Lust", "tr": "istek, heves"}}],
                                        // word cards shown on screen: about ONE every 30-60 s of video, the harder /
                                        // newer German words she actually says (not the grammar term itself, not
                                        // basic words like ich/ist). "s" = when she says it. Nouns with article.
                                        // "tr": short Turkish meaning, max ~22 characters.
 "cta": {{"clip": "c4", "s": 16.3}},     // where she asks viewers to subscribe/like (null if she doesn't)
 "title_hint": "zu + Infinitiv",        // the lesson topic in a few words
 "terms": ["zu + Infinitiv", "anfangen", ...],   // German terms/structures this lesson teaches (for the term list)
 "notes": ["0:31 'so müde, dass' - audio unclear, kept as heard"]     // anything uncertain a human should check (in English)
}}

Rules for cues:
- Exactly her words, cleaned of fillers (ııı, eee) and false starts; correct spelling, punctuation, capitals
  (German nouns capitalised; Turkish rules for Turkish, so "Infinitiv" never becomes "İnfinitiv").
- "lang": "de" for a German sentence/phrase she says as German; give its Turkish translation in "tr".
  "lang": "tr" for Turkish; German words inside a Turkish line go in [square brackets] (they get the German colour);
  give its German translation in "de" (for the German caption track; keep the bracketed German as it is).
- One cue = one short phrase: at most ~45 characters (German cues may go to ~60), 1.2-4.5 s long, split at commas/clauses.
- Times in seconds, clip-local, taken from the TR word timings (first word start, last word end). Cues never overlap,
  stay inside [in, out] and outside cuts.
- Cover everything she says between in and out.

{clips}
"""


def clip_block(c, tx):
    lines = [f'=== {c["id"]}  ({c["name"]}, {c["duration"]:.1f} s)']
    lines.append('TR words (word@start):')
    for s in tx['tr']['segs']:
        lines.append(f'  [{s["s"]:.2f}-{s["e"]:.2f}] ' + ' '.join(f'{w["w"]}@{w["s"]:.2f}' for w in s['words']))
    for p in ('de', 'auto'):
        lines.append(f'{p.upper()} segments' + (f' (detected: {tx[p]["lang"]})' if p == 'auto' else '') + ':')
        lines += [f'  [{s["s"]:.2f}-{s["e"]:.2f}] {s["text"]}' for s in tx[p]['segs']]
    return '\n'.join(lines)


def build_prompt(clips, work, profile):
    blocks = []
    for c in clips:
        tx = {p: json.loads((work / 'tx' / f'{c["id"]}.{p}.json').read_text(encoding='utf-8')) for p in ('tr', 'de', 'auto')}
        blocks.append(clip_block(c, tx))
    return PROMPT.format(channel=profile['name'], primary_name='Turkish', n=len(clips), short_max=170,
                         terms=', '.join(profile.get('terms', [])) or '-', clips='\n\n'.join(blocks))


def timeline(ans, clips, short=False):
    """Edit list → segments [{clip, a, b, start}], cues in output time [{s, e, lang, text, tr, de}], total length,
    call-to-action time and word cards [{s, e, word, tr}]."""
    by_id = {c['id']: c for c in clips}
    plan = {c['id']: c for c in ans['clips']}
    segs, cues, t = [], [], 0.0
    cta, vocab = None, []
    for cid in ans.get('order') or [c['id'] for c in clips]:
        p = plan[cid]
        a0, b0 = max(0.0, p['in']), min(by_id[cid]['duration'], p['out'])
        inside = lambda c: any(x[0] <= c['s'] and c['e'] <= x[1] for x in p.get('cuts', []) + (p.get('short_cuts', []) if short else []))
        cs = sorted((c for c in p['cues'] if c['e'] > a0 and c['s'] < b0 and not inside(c)), key=lambda c: c['s'])
        extra = p.get('short_cuts', []) if short else []
        cut = [(max(a0, x[0]), min(b0, x[1])) for x in p.get('cuts', []) + extra if x[1] > a0 and x[0] < b0]
        # long silences between cues shrink to GAP_KEEP (half kept on each side)
        for x, y in zip(cs, cs[1:]):
            if y['s'] - x['e'] > GAP_MAX:
                m = GAP_KEEP / 2
                cut.append((x['e'] + m, y['s'] - m))
        keep, pos = [], a0
        for x, y in sorted(cut):
            if x > pos:
                keep.append((pos, x))
            pos = max(pos, y)
        if b0 > pos:
            keep.append((pos, b0))
        keep = [(x, y) for x, y in keep if y - x > 0.15]

        def to_out(v, kp=tuple(keep), base=t):
            acc = base
            for x, y in kp:
                if v < x:
                    return acc
                if v <= y:
                    return acc + v - x
                acc += y - x
            return acc

        for x, y in keep:
            segs.append({'clip': cid, 'a': round(x, 3), 'b': round(y, 3), 'start': round(t, 3)})
            t += y - x
        for c in cs:
            s, e = to_out(max(c['s'], a0)), to_out(min(c['e'], b0))
            if e - s > 0.3:
                cues.append({'s': s, 'e': e, 'lang': c['lang'], 'text': c['text'].strip(),
                             'tr': c.get('tr', '').strip(), 'de': c.get('de', '').strip()})
        if ans.get('cta') and ans['cta'].get('clip') == cid:
            cta = to_out(ans['cta']['s'])
        for v in ans.get('vocab', []):
            if v.get('clip') == cid and a0 <= v['s'] <= b0:
                vocab.append({'s': to_out(v['s']), 'word': v['word'].strip(), 'tr': v['tr'].strip()})
    total = t
    # readable timing: at least 1.2 s on screen, close tiny gaps, never overlap
    for i, c in enumerate(cues):
        nxt = cues[i + 1]['s'] if i + 1 < len(cues) else total
        if nxt - c['e'] < 0.35:
            c['e'] = nxt - 0.04
        if c['e'] - c['s'] < 1.2:
            c['e'] = min(nxt - 0.04, c['s'] + 1.2)
    # word cards: 4 s each, at least 20 s apart
    cards = []
    for v in sorted(vocab, key=lambda v: v['s']):
        if (not cards or v['s'] - cards[-1]['s'] >= 20) and v['s'] + 4 < total - 1:
            cards.append({**v, 'e': v['s'] + 4.0})
    return segs, cues, total, cta, cards
