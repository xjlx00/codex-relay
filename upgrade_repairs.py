"""Switch a verified repair release at idle; rollback code without reverting usage."""
import hashlib,json,os,sqlite3,subprocess,time,urllib.request
from pathlib import Path

base=Path('/opt/codex-relay-v2'); release=Path(__file__).resolve().parent
database=Path('/var/lib/codex-relay/relay.sqlite3')

def idle(check_pending=True):
    with sqlite3.connect(database) as db:
        active=db.execute("SELECT COUNT(*) FROM requests WHERE status IN ('queued','running')").fetchone()[0]
        pending=db.execute('SELECT MAX(created) FROM response_history WHERE pending=1').fetchone()[0]
    if active or (check_pending and pending and time.time()-pending<900):
        raise RuntimeError('Active requests or recent pending tools; retry at idle')

def ledger(columns):
    with sqlite3.connect(database) as db:
        state={table:db.execute('SELECT '+','.join(names)+' FROM '+table+' ORDER BY rowid').fetchall()
               for table,names in columns.items()}
    return hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()

def switch(target):
    link=base/'current.repair-next';link.symlink_to(target,target_is_directory=True)
    os.replace(link,base/'current')

def ready(version):
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18021/healthz',timeout=2) as response:
                data=json.load(response)
                if data.get('ok') and data.get('version')==version:return True
        except (OSError,ValueError):pass
        time.sleep(1)
    return False

def main():
    old=(base/'current').resolve()
    if release.parent!=base/'releases' or release==old:raise RuntimeError('Invalid release directory')
    verified=json.loads((release/'REPAIR_VERIFIED.json').read_text())
    if not all(verified[k] for k in ('tests_passed','capacity_passed','live_passed')):
        raise RuntimeError('Verification incomplete')
    for relative,expected in verified['hashes'].items():
        if hashlib.sha256((release/relative).read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Verified release changed: '+relative)
    baseline=json.loads((release/'baseline-hashes.json').read_text())
    if baseline['release']!=str(old):raise RuntimeError('Live release changed')
    for relative,expected in baseline['hashes'].items():
        if hashlib.sha256((old/relative).read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Live code changed: '+relative)
    idle()
    backup=Path('/opt/codex-relay-backups')/('repair-'+time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(mode=0o700)
    (backup/'previous-release.txt').write_text(str(old))
    subprocess.run(['systemctl','stop','codex-relay'],check=True)
    switched=False
    try:
        idle(False)
        with sqlite3.connect(database) as db:
            columns={table:[r[1] for r in db.execute('PRAGMA table_info('+table+')')]
                     for table in ('users','requests','admin_actions','meter')}
            with sqlite3.connect(backup/'relay.sqlite3') as target:db.backup(target)
        os.chmod(backup/'relay.sqlite3',0o600)
        before=ledger(columns)
        switch(release);switched=True
        subprocess.run(['systemctl','start','codex-relay'],check=True)
        if not ready('0.3.0'):raise RuntimeError('New gateway failed health check')
        if ledger(columns)!=before:raise RuntimeError('Existing ledger changed during migration')
        with sqlite3.connect(database) as db:
            limits=db.execute("SELECT concurrent_limit FROM users WHERE role='user'").fetchall()
        if limits!=[(1,)]*4:raise RuntimeError('Unexpected initial user limits')
        print(json.dumps({'deployed':str(release),'previous':str(old),'backup':str(backup),
            'ledger_preserved':True,'global_limit':6,'user_limits':[1]*4}))
    except BaseException:
        if switched:
            subprocess.run(['systemctl','stop','codex-relay'],check=True);switch(old)
        # reserved retains its original database semantics. Older code can read
        # the added user column, so rollback must retain the current database.
        subprocess.run(['systemctl','start','codex-relay'],check=True)
        raise

if __name__=='__main__':main()
