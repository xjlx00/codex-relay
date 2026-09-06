import sqlite3,time
import httpx,pytest
from cr.store import Store
from cr.monitor import UpstreamMonitor
from cr.server import create_app
from cr.config import Settings
from test_bridge_gateway import FakeRPC

@pytest.fixture
def db(tmp_path):
    store=Store(tmp_path/'admin.sqlite'); store.bootstrap(10000)
    yield store
    store.close()

def charge(db,rid='r',user='user1',amount=100):
    db.reserve(rid,user,'gpt-6-astra',100); db.running(rid)
    db.finish(rid,'completed',{'input_tokens':amount-10,'output_tokens':10,'input_tokens_details':{'cached_tokens':20}})

def test_reset_preserves_history_meter_and_inflight_new_usage(db):
    charge(db)
    db.reserve('live','user1','m',200); db.running('live')
    db.checkpoint('live',{'input_tokens':50,'output_tokens':10})
    db.reset_all_usage()
    assert db.usage('user1')['used']==0 and db.usage('user1')['held']==140
    assert db.token_counter()==160
    assert db.statistics(0,int(time.time())+2)['users'][0]['total_tokens']==160
    db.checkpoint('live',{'input_tokens':80,'output_tokens':20})
    db.finish('live','completed',{'input_tokens':80,'output_tokens':20})
    assert db.usage('user1')['used']==40
    db.reset_all_usage(); assert db.usage('user1')['used']==0
    assert db.statistics(0,int(time.time())+2)['users'][0]['total_tokens']==200
    assert db.token_counter()==200

def test_bulk_budget_covers_disabled_users_but_not_admin(db):
    db.update_user('user4',{'active':False}); db.set_all_budgets(555)
    assert [u['budget'] for u in db.users()]==[555]*4
    assert db.db.execute("SELECT budget FROM users WHERE id='admin'").fetchone()[0]==0
    assert not db.users()[-1]['active']
    for value in (-1,True,1.1,10**12+1):
        with pytest.raises(ValueError):db.set_all_budgets(value)

def test_time_range_includes_start_excludes_end_and_zero_users(db):
    charge(db,'a');charge(db,'b','user2',200)
    with db.db:
        db.db.execute("UPDATE requests SET created=100 WHERE id='a'")
        db.db.execute("UPDATE requests SET created=200 WHERE id='b'")
    data=db.statistics(100,200)['users']
    assert len(data)==4 and data[0]['total_tokens']==100 and data[1]['total_tokens']==0
    assert data[0]['input_tokens']==90 and data[0]['cached_tokens']==20
    assert db.statistics(200,201)['users'][1]['total_tokens']==200

def test_migration_preserves_existing_ledger(tmp_path):
    file=tmp_path/'legacy.sqlite'
    old=sqlite3.connect(file)
    old.executescript('''CREATE TABLE users(id TEXT PRIMARY KEY,name TEXT NOT NULL,key_hash TEXT UNIQUE NOT NULL,role TEXT NOT NULL DEFAULT 'user',budget INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1);
      CREATE TABLE requests(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,model TEXT NOT NULL,created REAL NOT NULL,period INTEGER NOT NULL,status TEXT NOT NULL,reserved INTEGER NOT NULL DEFAULT 0,charged INTEGER NOT NULL DEFAULT 0,input INTEGER NOT NULL DEFAULT 0,cached INTEGER NOT NULL DEFAULT 0,output INTEGER NOT NULL DEFAULT 0,authoritative INTEGER NOT NULL DEFAULT 0,error TEXT);
      INSERT INTO users VALUES('user1','One','hash','user',1000,1);
      INSERT INTO requests(id,user_id,model,created,period,status,charged) VALUES('r','user1','m',1,0,'completed',123);''');old.close()
    new=Store(file); assert new.token_counter()==123 and new.usage('user1')['used']==123;new.close()
    new=Store(file); assert new.token_counter()==123;new.close()

def limits(percent,reset=10000,identity='account',spark=0):
    return {'accountId':identity,'rateLimitsByLimitId':{
        'codex':{'primary':{'usedPercent':percent,'windowDurationMins':10080,'resetsAt':reset}},
        'codex_bengalfox':{'primary':{'usedPercent':spark,'windowDurationMins':300,'resetsAt':reset}}}}
ACCOUNT={'email':'example@example.test','planType':'pro'}

def test_monitor_ratio_survives_local_reset_and_separates_buckets(db):
    m=UpstreamMonitor(None,db); m.ingest(ACCOUNT,limits(20),100)
    charge(db,amount=300); db.reset_all_usage()
    result=m.ingest(ACCOUNT,limits(22,spark=1),160)['windows']
    assert result[0]['tokens_per_percent']==150 and result[0]['remaining_percent']==78
    assert result[1]['tokens_per_percent'] is None and result[1]['status']=='unmapped_bucket'
    again=UpstreamMonitor(None,db)
    assert again.state()['windows'][0]['tokens_per_percent']==150

@pytest.mark.parametrize('variant',['decrease','rollover','gap','account','plan'])
def test_monitor_rebaselines_discontinuous_samples(db,variant):
    m=UpstreamMonitor(None,db); m.ingest(ACCOUNT,limits(20),100); charge(db)
    account=dict(ACCOUNT); value=limits(22); now=160
    if variant=='decrease':value=limits(2)
    if variant=='rollover':value=limits(22,reset=20000)
    if variant=='gap':now=500
    if variant=='account':value=limits(22,identity='other')
    if variant=='plan':account['planType']='plus'
    w=m.ingest(account,value,now)['windows'][0]
    assert w['tokens_per_percent'] is None and w['delta_tokens']==0

def test_manual_reconciliation_does_not_fabricate_observed_tokens(db):
    db.reserve('missing','user1','m',100);db.running('missing');db.finish('missing','failed')
    assert db.reconcile('missing',300)
    assert db.token_counter()==0

async def test_admin_routes_and_validation(tmp_path):
    store=Store(tmp_path/'http.sqlite');keys=store.bootstrap(10000);rpc=FakeRPC()
    app=create_app(Settings(work_dir=str(tmp_path)),rpc,store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            h=lambda who:{'Authorization':'Bearer '+keys[who]}
            charge(store)
            for path,method,body in [('/api/admin/usage/reset','POST',None),('/api/admin/budget/all','PATCH',{'budget':200}),('/api/admin/statistics?start=0&end=9999999999','GET',None),('/api/admin/upstream/monitor','GET',None),('/api/admin/upstream/sample','POST',None)]:
                assert (await c.request(method,path,headers=h('user1'),json=body)).status_code==403
            assert (await c.patch('/api/admin/budget/all',headers=h('admin'),json={'budget':-1})).status_code==422
            r=await c.patch('/api/admin/budget/all',headers=h('admin'),json={'budget':500000000})
            assert r.status_code==200 and [u['budget'] for u in r.json()['users']]==[500000000]*4
            r=await c.post('/api/admin/usage/reset',headers=h('admin'))
            assert r.json()['users'][0]['used']==0
            r=await c.get('/api/admin/statistics?start=0&end=9999999999',headers=h('admin'))
            assert r.json()['users'][0]['total_tokens']==100
            assert (await c.get('/api/admin/statistics?start=5&end=5',headers=h('admin'))).status_code==400
            assert (await c.get('/api/admin/upstream/monitor',headers=h('admin'))).status_code==200
