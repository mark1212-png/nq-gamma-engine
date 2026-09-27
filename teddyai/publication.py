"""Snapshot identity and fail-closed commentary publication. Standard library only."""
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def identity(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def atomic(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


@contextlib.contextmanager
def lock(root):
    p = Path(root)/'notes'/'commentary.lock'
    p.parent.mkdir(exist_ok=True)
    try:
        fd = os.open(p, os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError('Another commentary publication is running; inspect notes/commentary.lock before retrying')
    try:
        os.write(fd, str(os.getpid()).encode()); os.close(fd)
        yield
    finally:
        p.unlink(missing_ok=True)


def publish(root, prose, snapshot):
    root = Path(root)
    sid = identity(snapshot)
    with lock(root):
        if identity(json.loads((root/'trinity.json').read_text(encoding='utf-8'))) != sid:
            raise RuntimeError('Inputs changed while commentary was generated; replacement was not published')
        stamp = dict(settle=snapshot.get('settle'), spot=snapshot.get('spot'), snapshot_id=sid,
                     flips={k:(v.get('hvl') or [None])[0] for k,v in snapshot.get('panels',{}).items() if v.get('hvl')})
        doc = dict(version=1, snapshot_id=sid, stamp=stamp, prose=prose,
                   generated=datetime.datetime.now(datetime.timezone.utc).isoformat())
        with tempfile.TemporaryDirectory(prefix='publication-', dir=root/'notes') as td:
            stage = Path(td)
            for name in ('build.py','publication.py','ladder_frag.html','trinity.json','state.json','notes/sizing.json','notes/sheet_extra.json','archive/history.csv'):
                src = root/name
                if src.exists():
                    dst = stage/name; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)
            (stage/'trinity.json').write_text(json.dumps(snapshot), encoding='utf-8')
            payload = json.dumps(doc,ensure_ascii=False,indent=2).encode('utf-8')
            (stage/'narrative.json').write_bytes(payload)
            subprocess.run([sys.executable, str(stage/'build.py')], cwd=stage, check=True, capture_output=True)
            rendered = (stage/'trinity.html').read_bytes()
            if identity(json.loads((root/'trinity.json').read_text(encoding='utf-8'))) != sid:
                raise RuntimeError('Inputs changed during rendering; replacement was not published')
            revision = root/'notes'/'commentary-revisions'/sid/doc['generated'].replace(':','-')
            revision.mkdir(parents=True)
            (revision/'narrative.json').write_bytes(payload)
            (revision/'trinity.html').write_bytes(rendered)
            manifest = dict(status='passed', snapshot_id=sid, html_sha256=hashlib.sha256(rendered).hexdigest(),
                            narrative_sha256=hashlib.sha256(payload).hexdigest(), generated=doc['generated'])
            # Manifest is the commit marker. Interrupted writes fail validation.
            atomic(root/'narrative.json',payload)
            atomic(root/'trinity.html',rendered)
            atomic(root/'publication.json',json.dumps(manifest).encode())


def validated(root, snapshot_id=None):
    root = Path(root)
    try:
        m = json.loads((root/'publication.json').read_text(encoding='utf-8'))
        return (m['status']=='passed' and (snapshot_id is None or m['snapshot_id']==snapshot_id)
                and m['snapshot_id']==identity(json.loads((root/'trinity.json').read_text(encoding='utf-8')))
                and m['html_sha256']==hashlib.sha256((root/'trinity.html').read_bytes()).hexdigest()
                and m['narrative_sha256']==hashlib.sha256((root/'narrative.json').read_bytes()).hexdigest())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def publish_replay(root, prose, snapshot):
    """Existing replay swaps the HTML temporarily; keep live prose/manifest intact."""
    root = Path(root)
    with lock(root):
        with tempfile.TemporaryDirectory(prefix='replay-publication-',dir=root/'notes') as td:
            stage=Path(td)
            for name in ('build.py','publication.py','ladder_frag.html','trinity.json','state.json','notes/sizing.json','notes/sheet_extra.json','archive/history.csv'):
                if (root/name).exists():
                    (stage/name).parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(root/name,stage/name)
            publish(stage,prose,snapshot)
            atomic(root/'trinity.html',(stage/'trinity.html').read_bytes())
