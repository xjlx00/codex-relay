"""Install a separate, unprivileged release; preserve the old relay and other services."""
import json,os,pwd,shutil,subprocess,time,urllib.request
from pathlib import Path
source=Path(__file__).resolve().parent
base=Path('/opt/codex-relay-v2'); release=base/'releases/four-person-20260905'
state=Path('/var/lib/codex-relay'); backups=Path('/opt/codex-relay-backups/configs-20260905')
def run(*args,**kw):return subprocess.run(args,check=True,**kw)
backups.mkdir(parents=True,exist_ok=True,mode=0o700)
try:account=pwd.getpwnam('codex-relay')
except KeyError:
    run('useradd','--system','--home-dir',str(state),'--shell','/usr/sbin/nologin','codex-relay')
    account=pwd.getpwnam('codex-relay')
for p in (base,base/'bin',release):p.mkdir(parents=True,exist_ok=True,mode=0o755)
shutil.copy2('/opt/codex-relay/bin/codex-app-server',base/'bin/codex-app-server')
os.chmod(base/'bin/codex-app-server',0o755)
for p in (state,state/'codex',state/'work'):
    p.mkdir(parents=True,exist_ok=True,mode=0o700); os.chmod(p,0o700); os.chown(p,account.pw_uid,account.pw_gid)
shutil.copytree(source/'cr',release/'cr',dirs_exist_ok=True)
shutil.copy2(source/'requirements.txt',release/'requirements.txt')
if not (base/'venv/bin/python').exists():run('python3','-m','venv',str(base/'venv'))
run(str(base/'venv/bin/python'),'-m','pip','install','--disable-pip-version-check','-q','-r',str(release/'requirements.txt'))
keys=Path('/root/codex-relay-initial-keys.json')
if not (state/'relay.sqlite3').exists():
    run(str(base/'venv/bin/python'),'-B','-m','cr.manage','--db',str(state/'relay.sqlite3'),
        '--keys-file',str(keys),'--budget','500000000',cwd=release)
for p in state.glob('relay.sqlite3*'):os.chown(p,account.pw_uid,account.pw_gid); os.chmod(p,0o600)
current=base/'current'
if current.exists() and not current.is_symlink():raise SystemExit('Unexpected current release path')
if current.is_symlink():current.unlink()
current.symlink_to(release,target_is_directory=True)
unit=Path('/etc/systemd/system/codex-relay.service')
if unit.exists() and not (backups/'codex-relay.service').exists():shutil.copy2(unit,backups/'codex-relay.service')
shutil.copy2(source/'codex-relay.service',unit)
run('systemctl','daemon-reload'); run('systemctl','enable','codex-relay'); run('systemctl','restart','codex-relay')
ready=False
for _ in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:18021/healthz',timeout=2) as r:ready=json.load(r)['ok']
    except Exception:pass
    if ready:break
    time.sleep(1)
if not ready:raise SystemExit('Service not ready; nginx TLS route has not been enabled')
conf=Path('/etc/nginx/conf.d/codex-relay.conf'); stream=Path('/etc/nginx/stream.d/yanzi-stream.conf')
for p in (conf,stream):
    target=backups/p.name
    if not target.exists():shutil.copy2(p,target)
old_conf=conf.read_text(); old_stream=stream.read_text()
if 'relay.yanero.top' not in old_stream:
    marker='map $ssl_preread_server_name $backend {'
    if old_stream.count(marker)!=1:raise SystemExit('Unexpected SNI map; not changing it')
    stream.write_text(old_stream.replace(marker,marker+'\n    relay.yanero.top 127.0.0.1:9444;',1))
shutil.copy2(source/'relay-nginx.conf',conf)
check=subprocess.run(['nginx','-t'])
if check.returncode:
    conf.write_text(old_conf); stream.write_text(old_stream); raise SystemExit('Nginx validation failed; restored configuration')
run('systemctl','reload','nginx')
print('DEPLOYED_UNPRIVILEGED_RELAY_HTTPS')
