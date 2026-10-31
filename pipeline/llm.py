"""Claude for the language steps (proofread cues, YouTube texts, thumbnail picks).

Backends, first that works:
  api     ANTHROPIC_API_KEY, or a key in %USERPROFILE%\\.shorts-pipeline\\anthropic_key
  cli     `claude -p` (Claude Code on this PC; for unattended use run `claude setup-token` once)
  manual  writes work/llm/<name>.prompt.md and stops; a Claude Code session writes <name>.answer.json
Answers are cached in work/llm, so a re-run never asks twice.
"""
import base64, json, os, shutil, subprocess, urllib.request
from pathlib import Path

MODEL = os.environ.get('PIPELINE_MODEL', 'claude-opus-5-5')
KEY_FILE = Path.home() / '.shorts-pipeline' / 'anthropic_key'


class Pending(Exception):
    """A manual answer is needed before the pipeline can go on."""


def _key():
    if os.environ.get('ANTHROPIC_API_KEY'):
        return os.environ['ANTHROPIC_API_KEY']
    return KEY_FILE.read_text().strip() if KEY_FILE.exists() else None


def backend():
    forced = os.environ.get('PIPELINE_LLM')
    if forced:
        return forced
    if _key():
        return 'api'
    if shutil.which('claude') and _cli_ok():
        return 'cli'
    return 'manual'


_cli_state = None


def _cli_ok():
    global _cli_state
    if _cli_state is None:
        try:
            r = subprocess.run(['claude', '-p', 'Reply with OK', '--output-format', 'json'], capture_output=True,
                               text=True, encoding='utf-8', timeout=120)
            _cli_state = not json.loads(r.stdout).get('is_error', True)
        except Exception:
            _cli_state = False
    return _cli_state


def _parse(text):
    a, b = text.find('{'), text.rfind('}')
    if a < 0 or b < a:
        raise ValueError('no JSON object in the answer:\n' + text[:500])
    return json.loads(text[a:b + 1])


def ask(work, name, prompt, images=(), max_tokens=16000):
    """Returns the JSON object Claude answers with. `images`: paths of JPG/PNG files to look at."""
    d = Path(work) / 'llm'
    d.mkdir(exist_ok=True)
    ans = d / f'{name}.answer.json'
    if ans.exists():
        return json.loads(ans.read_text(encoding='utf-8'))
    (d / f'{name}.prompt.md').write_text(
        prompt + ''.join(f'\n\n[image: {p}]' for p in images), encoding='utf-8')
    how = backend()
    if how == 'manual':
        raise Pending(str(d / f'{name}.prompt.md'))
    if how == 'api':
        content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png' if str(p).endswith('.png') else 'image/jpeg',
                                                'data': base64.b64encode(Path(p).read_bytes()).decode()}} for p in images]
        content.append({'type': 'text', 'text': prompt})
        req = urllib.request.Request('https://api.anthropic.com/v1/messages', method='POST', headers={
            'x-api-key': _key(), 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
            data=json.dumps({'model': MODEL, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': content}]}).encode())
        with urllib.request.urlopen(req, timeout=600) as r:
            text = ''.join(b.get('text', '') for b in json.loads(r.read())['content'])
    else:
        full = prompt + ''.join(f'\n\nLook at this image with the Read tool: {p}' for p in images)
        r = subprocess.run(['claude', '-p', full, '--output-format', 'json', '--model', MODEL, '--allowedTools', 'Read'],
                           capture_output=True, text=True, encoding='utf-8', timeout=1800, cwd=str(d))
        out = json.loads(r.stdout)
        if out.get('is_error'):
            raise RuntimeError('claude -p: ' + str(out.get('result'))[:500])
        text = out['result']
    data = _parse(text)
    ans.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    return data
