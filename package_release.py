from pathlib import Path
import tarfile
root=Path(__file__).parent
with tarfile.open(root/'release.tar.gz','w:gz') as tar:
    for name in ['cr','requirements.txt','codex-relay.service','relay-nginx.conf','deploy_service.py']:
        path=root/name
        files=path.rglob('*') if path.is_dir() else [path]
        for file in files:
            if file.is_file() and '__pycache__' not in file.parts:
                tar.add(file,arcname=file.relative_to(root))
print('Release archive ready')
