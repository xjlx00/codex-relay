"""Persistent, window-aware observations; percentages are not a fixed token price."""
import asyncio,hashlib,json,math,time,uuid

class UpstreamMonitor:
    interval=60
    def __init__(self,rpc,store):
        self.rpc=rpc; self.store=store; self.lock=asyncio.Lock()
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS upstream_samples(id INTEGER PRIMARY KEY,created REAL NOT NULL,
              account TEXT NOT NULL,limit_id TEXT NOT NULL,window TEXT NOT NULL,name TEXT,
              minutes INTEGER NOT NULL,resets_at INTEGER NOT NULL,used REAL NOT NULL,
              tokens INTEGER NOT NULL,segment TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS upstream_series ON upstream_samples(account,limit_id,window,id);
            CREATE INDEX IF NOT EXISTS upstream_segment ON upstream_samples(segment,id);
            CREATE TABLE IF NOT EXISTS upstream_monitor_state(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);
        ''')
    def state(self):
        row=self.store.db.execute('SELECT value FROM upstream_monitor_state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {'last_attempt':None,'last_success':None,'error':None,'account':None,'windows':[]}
    def save_state(self,value):
        with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO upstream_monitor_state VALUES(1,?)',(json.dumps(value),))
    def ingest(self,account,limits,now=None):
        now=time.time() if now is None else now
        identity=limits.get('accountId') or account.get('email')
        if not identity:raise ValueError('account_identity_unavailable')
        owner=hashlib.sha256((str(identity)+'|'+str(account.get('planType'))).encode()).hexdigest()
        last_account=self.state().get('account')
        buckets=limits.get('rateLimitsByLimitId')
        if not isinstance(buckets,dict) or not buckets:
            bucket=limits.get('rateLimits') or {}; buckets={bucket.get('limitId') or 'codex':bucket}
        windows=[]; tokens=self.store.token_counter(); db=self.store.db
        with db:
            for lid,bucket in buckets.items():
                if not isinstance(bucket,dict):continue
                for window in ('primary','secondary'):
                    value=bucket.get(window)
                    if not isinstance(value,dict):continue
                    used=value.get('usedPercent'); minutes=value.get('windowDurationMins'); reset=value.get('resetsAt')
                    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (used,minutes,reset)):continue
                    if not 0<=used<=100 or minutes<=0 or reset<=now:continue
                    previous=db.execute('SELECT * FROM upstream_samples WHERE account=? AND limit_id=? AND window=? ORDER BY id DESC LIMIT 1',(owner,lid,window)).fetchone()
                    same=last_account==owner and previous and previous['minutes']==minutes and previous['resets_at']==reset and used>=previous['used'] and now-previous['created']<=180 and tokens>=previous['tokens']
                    segment=previous['segment'] if same else uuid.uuid4().hex
                    db.execute('INSERT INTO upstream_samples(created,account,limit_id,window,name,minutes,resets_at,used,tokens,segment) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (now,owner,lid,window,bucket.get('limitName') or lid,int(minutes),int(reset),used,tokens,segment))
                    anchor=db.execute('SELECT * FROM upstream_samples WHERE segment=? ORDER BY id LIMIT 1',(segment,)).fetchone()
                    delta_pct=used-anchor['used']; delta_tokens=tokens-anchor['tokens']
                    status='collecting'; ratio=None
                    if lid!='codex':status='unmapped_bucket'
                    elif delta_pct>=1 and delta_tokens>0:status='estimate'; ratio=delta_tokens/delta_pct
                    elif delta_pct>=1:status='no_relay_usage'
                    windows.append({'limit_id':lid,'name':bucket.get('limitName') or lid,'window':window,
                        'minutes':int(minutes),'resets_at':int(reset),'used_percent':used,'remaining_percent':100-used,
                        'sample_start':anchor['created'],'sample_end':now,'delta_percent':delta_pct,'delta_tokens':delta_tokens,
                        'tokens_per_percent':ratio,'status':status})
            if not windows:raise ValueError('rate_limits_unavailable')
            db.execute('DELETE FROM upstream_samples WHERE created<?',(now-35*86400,))
        state={'last_attempt':now,'last_success':now,'error':None,'account':owner,'windows':windows}
        self.save_state(state); return state
    async def sample(self):
        async with self.lock:
            now=time.time()
            if now-(self.state().get('last_attempt') or 0)<20:return self.report()
            try:
                account=(await self.rpc.call('account/read',{},timeout=15)).get('account')
                if not account:raise ValueError('authentication_required')
                limits=await self.rpc.call('account/rateLimits/read',{},timeout=20)
                self.ingest(account,limits)
            except asyncio.CancelledError:raise
            except Exception as error:
                value=self.state(); value.update(last_attempt=now,error=str(error) if isinstance(error,ValueError) else 'upstream_unavailable')
                self.save_state(value)
        return self.report()
    def report(self):
        value=self.state(); value.pop('account',None)
        value['interval_seconds']=self.interval
        value['stale']=not value['last_success'] or time.time()-value['last_success']>self.interval*3
        return value
    async def run(self):
        while True:
            await self.sample()
            await asyncio.sleep(self.interval)
