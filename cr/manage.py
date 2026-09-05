"""One-time initialization. Writes keys to an owner-only file, never stdout."""
import argparse
import json
import os
from pathlib import Path
from .store import Store

def main():
    p=argparse.ArgumentParser(); p.add_argument('--db',required=True); p.add_argument('--keys-file',required=True)
    p.add_argument('--budget',type=int,default=500000000); args=p.parse_args()
    Path(args.db).parent.mkdir(parents=True,exist_ok=True)
    s=Store(args.db)
    try:
        # Reserve the output destination before changing the database.
        fd=os.open(args.keys_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        keys=s.bootstrap(args.budget)
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(keys or {},f,ensure_ascii=False,indent=2)
        print('Initialized four users and administrator; credentials saved to protected output file.' if keys else 'Database already initialized; no keys changed.')
    finally:s.close()
if __name__=='__main__':main()
