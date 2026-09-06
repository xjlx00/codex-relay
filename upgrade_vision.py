"""Install a verified vision release during an idle window, with rollback."""
import hashlib,json,os,re,shutil,sqlite3,subprocess,time,urllib.request
from pathlib import Path

base=Path('/opt/codex-relay-v2');release=Path(__file__).resolve().parent;old=(base/'current').resolve()
database=Path('/var/lib/codex-relay/relay.sqlite3');nginx=Path('/etc/nginx/conf.d/codex-relay.conf')
def digest():
    with sqlite3.connect(database) as db:
        state={t:db.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in ('users','requests','admin_actions','meter')}
    return hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()
def idle(age=True):
    with sqlite3.connect(database) as db:
        active=db.execute("SELECT COUNT(*) FROM requests WHERE status IN ('queued','running')").fetchone()[0]
        pending=db.execute('SELECT MAX(created) FROM response_history WHERE pending=1').fetchone()[0]
    if active or (age and pending and time.time()-pending<900):raise RuntimeError('Users still have active requests or pending tools')
def switch(target):
    link=base/'current.vision-next';link.symlink_to(target,target_is_directory=True);os.replace(link,base/'current')
def ready():
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18021/healthz',timeout=2) as r:
                if json.load(r).get('ok'):return True
        except Exception:pass
        time.sleep(1)
    return False

if release.parent!=base/'releases' or old==release:raise SystemExit('Invalid staging directory')
verified=json.loads((release/'VISION_VERIFIED.json').read_text())
if not verified['passed']:raise SystemExit('Live vision verification did not pass')
for p,h in verified['hashes'].items():
    if hashlib.sha256((release/p).read_bytes()).hexdigest()!=h:raise SystemExit('Staged code changed: '+p)
for p,h in json.loads((release/'baseline-hashes.json').read_text(encoding='utf-8-sig')).items():
    if hashlib.sha256((old/p).read_bytes()).hexdigest().upper()!=h:raise SystemExit('Live code changed: '+p)
before_conf=nginx.read_text()
after_conf,count=re.subn(r'client_max_body_size\s+2m\s*;', 'client_max_body_size 20m;',before_conf)
if count!=1:raise SystemExit('Unexpected nginx request body configuration')
idle()
backup=Path('/opt/codex-relay-backups')/('vision-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(mode=0o700)
shutil.copy2(nginx,backup/'nginx.conf');(backup/'previous-release.txt').write_text(str(old))
subprocess.run(['systemctl','stop','codex-relay'],check=True)
switched=False
try:
    idle(False);before=digest()
    source=sqlite3.connect(database);target=sqlite3.connect(backup/'relay.sqlite3')
    try:source.backup(target)
    finally:source.close();target.close()
    os.chmod(backup/'relay.sqlite3',0o600)
    nginx.write_text(after_conf);subprocess.run(['nginx','-t'],check=True)
    switch(release);switched=True
    subprocess.run(['systemctl','start','codex-relay'],check=True)
    if not ready():raise RuntimeError('New gateway did not become healthy')
    if digest()!=before:raise RuntimeError('Unexpected ledger change during upgrade')
    subprocess.run(['systemctl','reload','nginx'],check=True)
    print(json.dumps({'deployed':str(release),'previous':str(old),'backup':str(backup),'ledger_preserved':True}))
except BaseException:
    if switched:
        subprocess.run(['systemctl','stop','codex-relay'],check=True);switch(old)
    shutil.copy2(backup/'nginx.conf',nginx)
    subprocess.run(['systemctl','start','codex-relay'],check=True)
    subprocess.run(['nginx','-t'],check=True);subprocess.run(['systemctl','reload','nginx'],check=True)
    raise
