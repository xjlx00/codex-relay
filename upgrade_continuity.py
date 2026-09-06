"""Install this verified release during an idle period; preserve the live ledger."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import urllib.request

base=Path('/opt/codex-relay-v2')
release=Path(__file__).resolve().parent
old=(base/'current').resolve()
database=Path('/var/lib/codex-relay/relay.sqlite3')

def ledger_digest():
    with sqlite3.connect(database) as db:
        state={table:db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
               for table in ('users','requests','admin_actions','meter')}
    return hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()

def check_idle(require_age=True):
    with sqlite3.connect(database) as db:
        active=db.execute("SELECT COUNT(*) FROM requests WHERE status IN ('queued','running')").fetchone()[0]
        latest=db.execute('SELECT MAX(created) FROM requests').fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='response_history'").fetchone():
            latest=db.execute('SELECT MAX(created) FROM response_history WHERE pending=1').fetchone()[0]
    if active or (require_age and latest and time.time()-latest<900):
        raise RuntimeError('Relay is not idle beyond its old tool-wait timeout; retry later')

def switch(target):
    link=base/'current.continuity-next'
    link.symlink_to(target,target_is_directory=True)
    os.replace(link,base/'current')

def ready():
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18021/healthz',timeout=2) as response:
                if json.load(response).get('ok'):return True
        except Exception:pass
        time.sleep(1)
    return False

if release.parent!=base/'releases':raise SystemExit('Run from a staged release directory')
if old==release:raise SystemExit('Release already installed')
verification=json.loads((release/'PROTOCOL_VERIFIED.json').read_text())
for relative,digest in verification['hashes'].items():
    if hashlib.sha256((release/relative).read_bytes()).hexdigest()!=digest:
        raise SystemExit('Verified release was modified: '+relative)
expected_models={m['slug'] for m in json.loads((release/'cr/model_catalog.json').read_text())['models']}
if set(verification['models'])!=expected_models:raise SystemExit('Not every exposed model was verified')
for relative,digest in json.loads((release/'baseline-hashes.json').read_text()).items():
    if hashlib.sha256((old/relative).read_bytes()).hexdigest().upper()!=digest:
        raise SystemExit('Live source changed; merge it before deployment: '+relative)
check_idle()
backup=Path('/opt/codex-relay-backups')/('context-continuity-'+time.strftime('%Y%m%d-%H%M%S'))
backup.mkdir(mode=0o700)
(backup/'previous-release.txt').write_text(str(old))
subprocess.run(['systemctl','stop','codex-relay'],check=True)
switched=False
try:
    check_idle(False)
    before=ledger_digest()
    source=sqlite3.connect(database); target=sqlite3.connect(backup/'relay.sqlite3')
    try:source.backup(target)
    finally:target.close(); source.close()
    os.chmod(backup/'relay.sqlite3',0o600)
    switch(release); switched=True
    subprocess.run(['systemctl','start','codex-relay'],check=True)
    if not ready():raise RuntimeError('New service failed its health check')
    if ledger_digest()!=before:raise RuntimeError('Ledger changed during the idle upgrade; investigate before accepting it')
    print(json.dumps({'deployed':str(release),'backup':str(backup),'ledger_preserved':True}))
except BaseException:
    if switched:
        subprocess.run(['systemctl','stop','codex-relay'],check=True)
        switch(old)
    subprocess.run(['systemctl','start','codex-relay'],check=True)
    raise
