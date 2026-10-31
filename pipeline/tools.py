"""Shared helpers: ffmpeg, probing, CUDA DLLs, logging and the status file the phone sees in Drive."""
import json, os, re, site, subprocess, time
from pathlib import Path

FF_DIR = Path(os.environ.get('FFMPEG_DIR', r'C:\Users\baris\AppData\Local\Microsoft\WinGet\Packages'
                             r'\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin'))
FFMPEG, FFPROBE = str(FF_DIR / 'ffmpeg.exe'), str(FF_DIR / 'ffprobe.exe')
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.mkv', '.webm', '.3gp'}
HERE = Path(__file__).resolve().parent


def cuda_dlls():
    """faster-whisper on Windows needs cuBLAS/cuDNN from the pip wheels on the DLL path."""
    for sp in site.getsitepackages():
        for sub in ('nvidia/cublas/bin', 'nvidia/cudnn/bin'):
            p = os.path.join(sp, sub)
            if os.path.isdir(p):
                os.add_dll_directory(p)
                os.environ['PATH'] = p + ';' + os.environ['PATH']


def run(args, **kw):
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding='utf-8', errors='replace', **kw)
    if r.returncode:
        raise RuntimeError(f'{Path(str(args[0])).name} failed ({r.returncode}):\n{r.stderr[-3000:]}')
    return r


def ffmpeg(*args):
    return run([FFMPEG, '-hide_banner', '-v', 'error', '-y', *args])


def probe(path):
    r = run([FFPROBE, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path])
    d = json.loads(r.stdout)
    v = next((s for s in d['streams'] if s['codec_type'] == 'video'), None)
    a = next((s for s in d['streams'] if s['codec_type'] == 'audio'), None)
    rot = 0
    for sd in (v or {}).get('side_data_list', []):
        rot = int(sd.get('rotation', rot) or 0)
    rot = int((v or {}).get('tags', {}).get('rotate', rot))
    w, h = int(v['width']), int(v['height'])
    if abs(rot) % 180 == 90:
        w, h = h, w
    tags = d['format'].get('tags', {})
    return {
        'duration': float(d['format']['duration']), 'width': w, 'height': h,
        'fps': eval(v.get('avg_frame_rate', '30/1').replace('/0', '/1')) or 30,
        'hdr': v.get('color_transfer') in ('arib-std-b67', 'smpte2084'), 'transfer': v.get('color_transfer'),
        'audio': a is not None,
        'created': tags.get('com.apple.quicktime.creationdate') or tags.get('creation_time') or '',
    }


def tonemap(transfer):
    """iPhone HLG/PQ → SDR BT.709 (what V1 used)."""
    tin = 'arib-std-b67' if transfer == 'arib-std-b67' else 'smpte2084'
    return (f'zscale=tin={tin}:min=bt2020nc:pin=bt2020:rin=tv:t=linear:npl=203,format=gbrpf32le,'
            'zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv')


def ts(t):
    return f'{int(t // 60)}:{int(t % 60):02d}'


def slug(name):
    return re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-').lower() or 'lesson'


class Status:
    """Progress for the phone: Output/STATUS.txt (human) + status.json (machine), plus a local log."""

    def __init__(self, out_dir, work_dir):
        self.out, self.work = Path(out_dir), Path(work_dir)
        self.data = {}
        p = self.out / 'status.json'
        if p.exists():
            self.data = json.loads(p.read_text(encoding='utf-8'))

    def log(self, msg):
        line = time.strftime('%H:%M:%S ') + msg
        print(line, flush=True)
        with open(self.work / 'pipeline.log', 'a', encoding='utf-8') as f:
            f.write(line + '\n')

    def set(self, state, step='', **extra):
        self.data.update(state=state, step=step, updated=time.strftime('%Y-%m-%d %H:%M'), **extra)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / 'status.json').write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding='utf-8')
        human = {'working': 'Working', 'done': 'Ready', 'failed': 'Failed', 'waiting': 'Waiting'}.get(state, state)
        lines = [f'{human}: {step}', f'Updated {self.data["updated"]}']
        if self.data.get('error'):
            lines.append('Error: ' + self.data['error'])
        (self.out / 'STATUS.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.log(f'[{state}] {step}')
