"""Back up the live ledger online, then install the administrator update."""
import os,shutil,sqlite3,subprocess,time,urllib.request,json
from pathlib import Path
base=Path('/opt/codex-relay-v2'); old=(base/'current').resolve()
release=base/'releases/admin-tools-20260905'
backup=Path('/opt/codex-relay-backups/admin-tools-'+time.strftime('%Y%m%d-%H%M%S'))
backup.mkdir(parents=True,mode=0o700)
db=sqlite3.connect('/var/lib/codex-relay/relay.sqlite3')
active=db.execute("SELECT COUNT(*) FROM requests WHERE status IN ('queued','running')").fetchone()[0]
if active:raise SystemExit('Active requests present; retry after they finish')
target=sqlite3.connect(backup/'relay.sqlite3'); db.backup(target);target.close();db.close()
os.chmod(backup/'relay.sqlite3',0o600)
(backup/'previous-release.txt').write_text(str(old))
if release.exists():raise SystemExit('Release already exists; not overwriting')
release.mkdir(parents=True)
shutil.copytree(Path(__file__).parent/'cr',release/'cr')
shutil.copy2(old/'requirements.txt',release/'requirements.txt')
next_link=base/'current.next'; next_link.symlink_to(release,target_is_directory=True)
os.replace(next_link,base/'current')
subprocess.run(['systemctl','restart','codex-relay'],check=True)
ready=False
for _ in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:18021/healthz',timeout=2) as r:ready=json.load(r)['ok']
    except Exception:pass
    if ready:break
    time.sleep(1)
if not ready:
    rollback=base/'current.rollback';rollback.symlink_to(old,target_is_directory=True);os.replace(rollback,base/'current')
    subprocess.run(['systemctl','restart','codex-relay'],check=True)
    raise SystemExit('New service failed; previous code restored. Database migration is additive.')
print('ADMIN_UPGRADE_DEPLOYED',str(backup))
