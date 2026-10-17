import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
LIBRARY_DIR = os.path.join(ROOT, 'audiobooks', 'library')
JOBS_DIR = os.path.join(ROOT, 'audiobooks', 'jobs')
DB_PATH = os.path.join(ROOT, 'audiobooks', 'jobs.sqlite')
TMP_DIR = os.path.join(ROOT, 'tmp')
APP_PATH = os.path.join(ROOT, 'app.py')
AUDIO_SUFFIXES = {'.m4b', '.m4a', '.mp3', '.aac', '.flac', '.ogg', '.wav', '.mp4'}

def _connect()->sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init()->None:
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)
    with _connect() as db:
        db.execute(
            '''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                filename TEXT,
                ebook_path TEXT,
                settings TEXT,
                status TEXT,
                pid INTEGER,
                output_dir TEXT,
                error TEXT,
                created REAL,
                updated REAL
            )'''
        )

def _now()->float:
    return time.time()

def _rows(status:str|None=None)->list[sqlite3.Row]:
    init()
    with _connect() as db:
        if status is None:
            return list(db.execute('SELECT * FROM jobs ORDER BY created DESC'))
        return list(db.execute('SELECT * FROM jobs WHERE status=? ORDER BY created ASC', (status,)))

def _update(job_id:str, **fields:object)->None:
    fields['updated'] = _now()
    keys = list(fields)
    sql = 'UPDATE jobs SET ' + ', '.join(f'{key}=?' for key in keys) + ' WHERE id=?'
    with _connect() as db:
        db.execute(sql, [fields[key] for key in keys] + [job_id])

def alive(pid:int|None)->bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True

def _output_ready(output_dir:str)->bool:
    if not output_dir or not os.path.isdir(output_dir):
        return False
    for name in os.listdir(output_dir):
        if os.path.splitext(name)[1].lower() in AUDIO_SUFFIXES and not name.endswith('.vtt'):
            return True
    return False

def _sentence_audio(job_id:str)->bool:
    proc = os.path.join(TMP_DIR, f'proc-{job_id}')
    if not os.path.isdir(proc):
        return False
    for dirpath, _dirs, files in os.walk(proc):
        for name in files:
            if os.path.splitext(name)[1].lower() in {'.flac', '.wav', '.ogg'}:
                return True
    return False

def _last_log_line(job_id:str)->str:
    path = os.path.join(JOBS_DIR, f'{job_id}.log')
    if not os.path.isfile(path):
        return ''
    with open(path, 'rb') as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 8000))
        data = handle.read().decode('utf-8', 'replace').replace('\r', '\n')
    lines = [line.strip() for line in data.splitlines() if line.strip()]
    return lines[-1] if lines else ''

def progress(job_id:str)->str:
    import re
    path = os.path.join(JOBS_DIR, f'{job_id}.log')
    if not os.path.isfile(path):
        return ''
    with open(path, 'rb') as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - 20000))
        data = handle.read().decode('utf-8', 'replace')
    found = re.findall(r'(\d+)\s*/\s*(\d+)', data)
    if not found:
        return ''
    current, total = found[-1]
    return f'{current}/{total}'

def _settings_args(settings:dict)->list[str]:
    cmd = [
        '--language', str(settings.get('language') or 'eng'),
        '--device', str(settings.get('device') or 'mps'),
        '--tts_engine', str(settings.get('tts_engine') or 'xtts'),
        '--output_format', str(settings.get('output_format') or 'm4b'),
    ]
    voice = settings.get('voice')
    if isinstance(voice, str) and voice.strip():
        cmd.extend(['--voice', voice])
    fine = settings.get('fine_tuned')
    if isinstance(fine, str) and fine.strip():
        cmd.extend(['--fine_tuned', fine])
    custom = settings.get('custom_model')
    if isinstance(custom, str) and custom.strip() and os.path.exists(custom):
        cmd.extend(['--custom_model', custom])
    channel = settings.get('output_channel')
    if isinstance(channel, str) and channel.strip():
        cmd.extend(['--output_channel', channel])
    translate = settings.get('translate')
    if isinstance(translate, str) and translate.strip():
        cmd.extend(['--translate', translate])
    numbers = {
        '--temperature': 'xtts_temperature',
        '--length_penalty': 'xtts_length_penalty',
        '--num_beams': 'xtts_num_beams',
        '--repetition_penalty': 'xtts_repetition_penalty',
        '--top_k': 'xtts_top_k',
        '--top_p': 'xtts_top_p',
        '--speed': 'xtts_speed',
        '--text_temp': 'bark_text_temp',
        '--waveform_temp': 'bark_waveform_temp',
    }
    for flag, key in numbers.items():
        if settings.get(key) is not None:
            cmd.extend([flag, str(settings[key])])
    if settings.get('xtts_enable_text_splitting'):
        cmd.append('--enable_text_splitting')
    if settings.get('interlude_enabled'):
        cmd.append('--enable_interlude')
    return cmd

def _start(job:sqlite3.Row)->None:
    job_id = job['id']
    os.makedirs(os.path.join(TMP_DIR, f'proc-{job_id}'), exist_ok=True)
    os.makedirs(job['output_dir'], exist_ok=True)
    settings = json.loads(job['settings'] or '{}')
    cmd = [
        sys.executable, '-u', APP_PATH,
        '--script_mode', 'native', '--headless',
        '--session', job_id,
        '--ebook', job['ebook_path'],
        '--output_dir', job['output_dir'],
        *_settings_args(settings),
    ]
    log_path = os.path.join(JOBS_DIR, f'{job_id}.log')
    log = open(log_path, 'ab', buffering=0)
    resume = _sentence_audio(job_id)
    stdin = subprocess.PIPE if resume else subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdin=stdin,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    if resume and proc.stdin is not None:
        proc.stdin.write(b'r\n')
        proc.stdin.close()
    _update(job_id, status='running', pid=proc.pid, error='')

def pump()->None:
    init()
    running = [row for row in _rows('running') if alive(row['pid'])]
    if running:
        return
    queued = _rows('queued')
    if not queued:
        return
    _start(queued[0])

def poll()->None:
    init()
    for row in _rows('running'):
        if alive(row['pid']):
            continue
        if _output_ready(row['output_dir']):
            _update(row['id'], status='done', pid=None, error='')
        else:
            _update(row['id'], status='failed', pid=None, error=_last_log_line(row['id']) or 'The converter stopped before writing an audiobook.')
    pump()

def reconcile()->None:
    init()
    for row in _rows('running'):
        if alive(row['pid']):
            continue
        if _output_ready(row['output_dir']):
            _update(row['id'], status='done', pid=None, error='')
        else:
            _update(row['id'], status='queued', pid=None, error='')
    pump()

def _copy_ebook(job_id:str, source:str)->str:
    folder = os.path.join(JOBS_DIR, job_id)
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, Path(source).name)
    if os.path.abspath(source) != os.path.abspath(target):
        with open(source, 'rb') as src, open(target, 'wb') as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
    return target

def enqueue(source:str, settings:dict)->str:
    init()
    job_id = str(uuid.uuid4())
    ebook_path = _copy_ebook(job_id, source)
    output_dir = os.path.join(LIBRARY_DIR, job_id)
    os.makedirs(output_dir, exist_ok=True)
    with _connect() as db:
        db.execute(
            'INSERT INTO jobs (id, filename, ebook_path, settings, status, pid, output_dir, error, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (job_id, Path(source).name, ebook_path, json.dumps(settings), 'queued', None, output_dir, '', _now(), _now()),
        )
    pump()
    return job_id

def enqueue_text(text:str, settings:dict)->str:
    init()
    job_id = str(uuid.uuid4())
    folder = os.path.join(JOBS_DIR, job_id)
    os.makedirs(folder, exist_ok=True)
    ebook_path = os.path.join(folder, 'pasted.txt')
    with open(ebook_path, 'w', encoding='utf-8') as handle:
        handle.write(text)
    output_dir = os.path.join(LIBRARY_DIR, job_id)
    os.makedirs(output_dir, exist_ok=True)
    with _connect() as db:
        db.execute(
            'INSERT INTO jobs (id, filename, ebook_path, settings, status, pid, output_dir, error, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (job_id, 'pasted.txt', ebook_path, json.dumps(settings), 'queued', None, output_dir, '', _now(), _now()),
        )
    pump()
    return job_id

def cancel(job_id:str)->str:
    init()
    with _connect() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        return 'That job is not on the server.'
    _update(job_id, status='canceled', error='')
    pid = int(row['pid'] or 0)
    if alive(pid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except OSError:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            for _ in range(20):
                if not alive(pid):
                    break
                time.sleep(0.1)
            if not alive(pid):
                break
    return f'Canceled {row["filename"]}. Spoken chapters are still on disk.'

def resume(job_id:str)->str:
    init()
    with _connect() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        return 'That job is not on the server.'
    if row['status'] == 'running' and alive(row['pid']):
        return f'{row["filename"]} is already running.'
    _update(job_id, status='queued', pid=None, error='')
    pump()
    return f'Resumed {row["filename"]}.'

def status_text()->str:
    rows = _rows()
    if not rows:
        return 'No server jobs yet. Convert keeps running after you leave this page.'
    lines = []
    for row in rows[:12]:
        prog = progress(row['id']) if row['status'] == 'running' else ''
        extra = f' {prog}' if prog else ''
        err = f' — {row["error"]}' if row['status'] == 'failed' and row['error'] else ''
        lines.append(f'{row["filename"]}: {row["status"]}{extra}{err}')
    return '\n'.join(lines)

def job_choices()->list[tuple[str, str]]:
    choices = []
    for row in _rows():
        prog = progress(row['id']) if row['status'] == 'running' else ''
        label = f'{row["filename"]} — {row["status"]}'
        if prog:
            label = f'{label} {prog}'
        choices.append((label, row['id']))
    return choices

def library_choices()->list[tuple[str, str]]:
    init()
    found = []
    for dirpath, _dirs, files in os.walk(LIBRARY_DIR):
        for name in files:
            if name.lower().endswith('.vtt'):
                continue
            if os.path.splitext(name)[1].lower() not in AUDIO_SUFFIXES:
                continue
            path = os.path.join(dirpath, name)
            found.append((name, path, os.path.getmtime(path)))
    found.sort(key=lambda item: item[2], reverse=True)
    return [(name, path) for name, path, _mtime in found]
