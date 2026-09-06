"""Per-person ledger and bounded response history. Never store plaintext keys."""
import hashlib
import json
import secrets
import sqlite3
import time

class BudgetError(Exception):pass

class HistoryError(Exception):
    def __init__(self,code,status):
        self.code=code; self.status=status; super().__init__(code)

class Store:
    def __init__(self,path):
        self.db=sqlite3.connect(path,check_same_thread=False); self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT NOT NULL,key_hash TEXT UNIQUE NOT NULL,
          role TEXT NOT NULL DEFAULT 'user',budget INTEGER NOT NULL CHECK(budget>=0),active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
          model TEXT NOT NULL,created REAL NOT NULL,period INTEGER NOT NULL,status TEXT NOT NULL,
          reserved INTEGER NOT NULL DEFAULT 0,charged INTEGER NOT NULL DEFAULT 0,
          input INTEGER NOT NULL DEFAULT 0,cached INTEGER NOT NULL DEFAULT 0,output INTEGER NOT NULL DEFAULT 0,
          authoritative INTEGER NOT NULL DEFAULT 0,error TEXT);
        CREATE INDEX IF NOT EXISTS requests_owner_period ON requests(user_id,period);
        CREATE INDEX IF NOT EXISTS requests_created ON requests(created);
        CREATE TABLE IF NOT EXISTS admin_actions(id INTEGER PRIMARY KEY,created REAL NOT NULL,action TEXT NOT NULL,value INTEGER);
        CREATE TABLE IF NOT EXISTS meter(id INTEGER PRIMARY KEY CHECK(id=1),tokens INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS response_history(
          response_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
          history TEXT NOT NULL,bytes INTEGER NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
          pending INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS response_history_owner_created ON response_history(user_id,created);
        CREATE INDEX IF NOT EXISTS response_history_expires ON response_history(expires);
        ''')
        columns={r['name'] for r in self.db.execute('PRAGMA table_info(requests)')}
        user_columns={r['name'] for r in self.db.execute('PRAGMA table_info(users)')}
        with self.db:
            if 'concurrent_limit' not in user_columns:
                self.db.execute('ALTER TABLE users ADD COLUMN concurrent_limit INTEGER NOT NULL DEFAULT 1 CHECK(concurrent_limit BETWEEN 1 AND 6)')
            if 'forgiven' not in columns:self.db.execute('ALTER TABLE requests ADD COLUMN forgiven INTEGER NOT NULL DEFAULT 0')
            if 'metered' not in columns:
                self.db.execute('ALTER TABLE requests ADD COLUMN metered INTEGER NOT NULL DEFAULT 0')
                self.db.execute('UPDATE requests SET metered=charged')
            self.db.execute('INSERT OR IGNORE INTO meter SELECT 1,COALESCE(SUM(metered),0) FROM requests')
    def close(self):self.db.close()
    def cleanup_history(self):
        with self.db:self.db.execute('DELETE FROM response_history WHERE expires<=?',(time.time(),))

    def save_history(self,uid,rid,items,ttl,max_bytes,pending=False):
        history=json.dumps(items,ensure_ascii=False,separators=(',',':'))
        size=len(history.encode('utf-8'))
        if size>max_bytes:raise HistoryError('response_history_capacity',413)
        now=time.time()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM response_history WHERE expires<=?',(now,))
            used=self.db.execute('SELECT COALESCE(SUM(bytes),0) FROM response_history WHERE user_id=?',(uid,)).fetchone()[0]
            if used+size>max_bytes:
                rows=self.db.execute('SELECT response_id,bytes FROM response_history WHERE user_id=? ORDER BY created,rowid',(uid,)).fetchall()
                for old in rows:
                    self.db.execute('DELETE FROM response_history WHERE response_id=? AND user_id=?',(old['response_id'],uid))
                    used-=old['bytes']
                    if used+size<=max_bytes:break
            self.db.execute('INSERT INTO response_history VALUES(?,?,?,?,?,?,?)',
                (rid,uid,history,size,now,now+ttl,int(pending)))

    def load_history(self,uid,rid):
        row=self.db.execute('SELECT history,pending,expires FROM response_history WHERE response_id=? AND user_id=?',(rid,uid)).fetchone()
        if row and row['expires']>time.time():
            return {'history':json.loads(row['history']),'pending':bool(row['pending'])}
        # The durable ledger retains ownership after history has expired/been evicted.
        known=row or self.db.execute('SELECT 1 FROM requests WHERE id=? AND user_id=?',(rid,uid)).fetchone()
        if known:raise HistoryError('response_history_required',409)
        raise HistoryError('previous_response_not_found',404)
    def bootstrap(self,budget=500000000):
        if self.db.execute('SELECT 1 FROM users LIMIT 1').fetchone():return None
        keys={}
        with self.db:
            for uid,name,role in [('admin','管理员','admin'),*[(f'user{i}',f'用户{i}','user') for i in range(1,5)]]:
                key='cr_'+secrets.token_urlsafe(32)
                self.db.execute('INSERT INTO users(id,name,key_hash,role,budget) VALUES(?,?,?,?,?)',
                  (uid,name,hashlib.sha256(key.encode()).hexdigest(),role,budget if role=='user' else 0)); keys[uid]=key
        return keys
    def authenticate(self,key):
        row=self.db.execute('SELECT id,name,role,budget,active,concurrent_limit FROM users WHERE key_hash=? AND active=1',
             (hashlib.sha256(key.encode()).hexdigest(),)).fetchone()
        return dict(row) if row else None
    def users(self):
        return [dict(r) for r in self.db.execute("SELECT id,name,role,budget,active,concurrent_limit FROM users WHERE role='user' ORDER BY id")]
    def usage(self,uid):
        user=self.db.execute('SELECT id,name,budget,active,concurrent_limit FROM users WHERE id=?',(uid,)).fetchone()
        row=self.db.execute('''SELECT COUNT(*) request_count,COALESCE(SUM(MAX(0,charged-forgiven)),0) used,
          COALESCE(SUM(MAX(0,reserved-charged)),0) held,COALESCE(SUM(input),0) input_tokens,
          COALESCE(SUM(cached),0) cached_tokens,COALESCE(SUM(output),0) output_tokens,
          COALESCE(SUM(status='unknown'),0) unresolved FROM requests WHERE user_id=?''',(uid,)).fetchone()
        result={**dict(user),**dict(row),'period_start':None,'resets_at':None,'reset_policy':'manual'}
        result['remaining']=max(0,user['budget']-result['used']-result['held'])
        result['warning']=result['used']+result['held']>=user['budget']*.8
        return result
    def reset_all_usage(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('UPDATE requests SET forgiven=charged')
            self.db.execute("INSERT INTO admin_actions(created,action) VALUES(?,'reset_all_usage')",(time.time(),))
        return [self.usage(u['id']) for u in self.users()]
    def set_all_budgets(self,budget):
        if isinstance(budget,bool) or not isinstance(budget,int) or not 0<=budget<=10**12:raise ValueError('Invalid budget')
        with self.db:
            self.db.execute("UPDATE users SET budget=? WHERE role='user'",(budget,))
            self.db.execute("INSERT INTO admin_actions(created,action,value) VALUES(?,'set_all_budgets',?)",(time.time(),budget))
        return self.users()
    def statistics(self,start,end):
        rows=self.db.execute('''SELECT u.id,u.name,COUNT(r.id) request_count,
            COALESCE(SUM(r.charged),0) total_tokens,COALESCE(SUM(r.input),0) input_tokens,
            COALESCE(SUM(r.output),0) output_tokens,COALESCE(SUM(r.cached),0) cached_tokens,
            COALESCE(SUM(r.status='unknown'),0) unresolved,
            COALESCE(SUM(r.status='reconciled'),0) reconciled
            FROM users u LEFT JOIN requests r ON r.user_id=u.id AND r.created>=? AND r.created<?
            WHERE u.role='user' GROUP BY u.id ORDER BY u.id''',(start,end))
        return {'start':start,'end':end,'time_basis':'request_started','users':[dict(r) for r in rows]}
    def token_counter(self):return self.db.execute('SELECT tokens FROM meter WHERE id=1').fetchone()[0]
    def _meter(self,rid,total):
        previous=self.db.execute('SELECT metered FROM requests WHERE id=?',(rid,)).fetchone()[0]
        delta=max(0,total-previous)
        self.db.execute('UPDATE meter SET tokens=tokens+? WHERE id=1',(delta,))
        self.db.execute('UPDATE requests SET metered=MAX(metered,?) WHERE id=?',(total,rid))
    def reserve(self,rid,uid,model,amount):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE'); u=self.usage(uid)
            if not u['active']:raise BudgetError('user_disabled')
            if u['remaining']<amount:raise BudgetError('budget_exhausted')
            self.db.execute('INSERT INTO requests(id,user_id,model,created,period,status,reserved) VALUES(?,?,?,?,?,?,?)',
                (rid,uid,model,time.time(),0,'queued',amount))
    def running(self,rid):
        with self.db:self.db.execute("UPDATE requests SET status='running' WHERE id=? AND status='queued'",(rid,))
    def checkpoint(self,rid,usage,carried_ids=()):
        """Persist completed model usage even if the client later disconnects."""
        inp=max(0,int(usage['input_tokens'])); out=max(0,int(usage['output_tokens']))
        cached=max(0,min(inp,int(usage.get('input_tokens_details',{}).get('cached_tokens',0))))
        with self.db:
            row=self.db.execute('SELECT user_id,status FROM requests WHERE id=?',(rid,)).fetchone()
            if not row or row['status']!='running':return False
            self._meter(rid,inp+out)
            # Some app-server versions report the model's usage only after a dynamic
            # tool returns. The new cumulative report includes these earlier segments.
            for prior in carried_ids:
                self.db.execute("UPDATE requests SET status='settled_in_continuation',reserved=0,error=NULL WHERE id=? AND status='unknown' AND user_id=(SELECT user_id FROM requests WHERE id=?)",
                    (prior,rid))
            self.db.execute("UPDATE requests SET charged=?,input=?,cached=?,output=? WHERE id=? AND status='running'",
                (inp+out,inp,cached,out,rid))
        # reserved remains the original reservation in SQLite, so existing ledgers
        # need no destructive migration. Only the unspent portion is still held.
        user=self.usage(row['user_id'])
        own=self.db.execute('SELECT MAX(0,reserved-charged) FROM requests WHERE id=?',(rid,)).fetchone()[0]
        # Already admitted work may consume its own reservation. Other requests'
        # holds remain protected; consuming exactly the budget is still valid.
        return bool(user['active']) and user['used']+user['held']-own<=user['budget']
    def finish(self,rid,status,usage=None,error=None,dispatched=True):
        with self.db:
            row=self.db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
            if not row or row['status'] not in ('queued','running'):return
            if usage is not None:
                inp=max(0,int(usage.get('input_tokens',0))); out=max(0,int(usage.get('output_tokens',0)))
                cached=max(0,min(inp,int(usage.get('input_tokens_details',{}).get('cached_tokens',0))))
                self._meter(rid,inp+out)
                self.db.execute('''UPDATE requests SET status=?,charged=?,reserved=0,input=?,cached=?,output=?,
                  authoritative=1,error=? WHERE id=?''',(status,inp+out,inp,cached,out,error,rid))
            elif not dispatched:self.db.execute('UPDATE requests SET status=?,reserved=0,error=? WHERE id=?',(status,error,rid))
            else:self.db.execute("UPDATE requests SET status='unknown',error=? WHERE id=?",(error or 'usage_unavailable',rid))
    def recover(self):
        with self.db:
            self.db.execute("UPDATE requests SET status='cancelled',reserved=0,error='service_restart' WHERE status='queued'")
            self.db.execute("UPDATE requests SET status='unknown',error='service_restart' WHERE status='running'")
    def recent(self,uid=None):
        fields='id,user_id,model,created,status,charged,MAX(0,reserved-charged) AS reserved,input,cached,output,authoritative,error'
        if uid:rows=self.db.execute(f'SELECT {fields} FROM requests WHERE user_id=? ORDER BY created DESC LIMIT 60',(uid,))
        else:rows=self.db.execute(f'SELECT {fields} FROM requests ORDER BY created DESC LIMIT 120')
        return [dict(r) for r in rows]
    def update_user(self,uid,values):
        allowed={k:v for k,v in values.items() if k in ('name','budget','active','concurrent_limit')}
        if 'concurrent_limit' in allowed:
            limit=allowed['concurrent_limit']
            if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=6:raise ValueError('Invalid concurrency limit')
        if not allowed:return
        with self.db:
            cur=self.db.execute('UPDATE users SET '+','.join(k+'=?' for k in allowed)+" WHERE id=? AND role='user'",(*allowed.values(),uid))
        return self.usage(uid) if cur.rowcount else None
    def rotate(self,uid):
        key='cr_'+secrets.token_urlsafe(32)
        with self.db:
            cur=self.db.execute("UPDATE users SET key_hash=? WHERE id=? AND role='user'",(hashlib.sha256(key.encode()).hexdigest(),uid))
            if not cur.rowcount:raise KeyError(uid)
        return key
    def reconcile(self,rid,charge):
        with self.db:
            cur=self.db.execute("UPDATE requests SET status='reconciled',charged=?,reserved=0,error='admin_reconciled' WHERE id=? AND status='unknown'",(charge,rid))
            return bool(cur.rowcount)
