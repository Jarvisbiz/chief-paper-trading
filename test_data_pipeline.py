"""Regression cases for sparse contracts, fresh references, retries and full-margin paper."""
import asyncio
import json
import time

import httpx
import pytest

import main as e
import futures_runtime as f
from test_engine import account
from test_futures import inputs, fund
from test_sniper import setup_market


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(e,'PRACTICE_VENUE','COINBASE_US_FUTURES')
    monkeypatch.setattr(e,'SIGNAL_SOURCE','SPOT_REFERENCE')
    monkeypatch.setattr(e,'SIZING_MODE','FULL_MARGIN')


def test_sparse_contract_history_does_not_poison_continuous_spot_setup():
    now,_,_=setup_market()
    with e.db() as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM bars')]
        for interval in (60,300,3600):e.store_signal_bars(c,'BTC-USD',interval,[r for r in rows if r['interval']==interval])
        c.execute('DELETE FROM bars WHERE interval=60 AND ts%120=0')
        ev=e.scalp_evidence(c,'BTC-USD',now)
        assert ev['technical_pass'],ev['reasons']
        assert all(x['state']=='READY' for x in e.input_readiness(c,'BTC-USD',now).values())
        assert c.execute('SELECT COUNT(*) FROM bars WHERE interval=60').fetchone()[0]<40


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_fresh_reference_mapping_preserves_relative_price_levels(direction):
    now,q,ev=inputs(side=direction)
    q['signal_quote']={'product_id':'ETH-USD','price':1990.,'ts':now}
    mapped=e.map_signal_evidence('ETH-USD',q,ev,now)
    ratio=2000/1990
    assert mapped['structural_stop']==pytest.approx(ev['structural_stop']*ratio)
    assert mapped['reference_prices']['structural_stop']==ev['structural_stop']
    assert mapped['signal_source']=='COINBASE_SPOT_REFERENCE'
    assert mapped['basis']['futures_price']==q['price']


def test_stale_mismatched_or_outlier_basis_cannot_make_an_entry():
    now,q,ev=inputs();q['signal_quote']={'product_id':'ETH-USD','price':1990.,'ts':now-11}
    with pytest.raises(e.MarketDataUnavailable):e.map_signal_evidence('ETH-USD',q,ev,now)
    q['signal_quote'].update(ts=now,price=1500)
    with pytest.raises(e.MarketDataUnavailable,match='2%'):e.map_signal_evidence('ETH-USD',q,ev,now)
    q['signal_quote'].update(price=1990,product_id='BTC-USD')
    with pytest.raises(e.MarketDataUnavailable):e.map_signal_evidence('ETH-USD',q,ev,now)
    with e.db() as c:
        assert 'mapping' in e.open_position(c,'ETH-USD','LONG',q,ev,now)
        assert c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==0


@pytest.mark.parametrize('balance',[80,150])
@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_full_margin_uses_max_affordable_whole_lots_with_stops_and_reserves(monkeypatch,balance,direction):
    monkeypatch.setattr(e,'SIGNAL_SOURCE','CONTRACT');fund(balance)
    now,q,ev=inputs(side=direction)
    with e.db() as c:
        assert e.open_position(c,'ETH-USD',direction,q,ev,now) is None
        p=dict(c.execute('SELECT * FROM positions').fetchone())
        count=p['contracts'];qty=p['qty'];entry=p['entry']
        cost=p['margin']+p['entry_fee']+f.fee(count,.1,entry)
        assert count>=1 and count==int(count) and qty==pytest.approx(count*.1)
        assert cost<=balance
        assert (cost/count)*(count+1)>balance
        assert p['stop']>0 and p['target']>0
        assert p['risk_amount']>balance*e.SCALP_RISK  # selected full allocation, not old tiny-risk cap
        assert 'one position' in e.open_position(c,'BTC-USD',direction,q,ev,now)
        plan=json.loads(p['evidence'])['plan']
        assert plan['sizing_mode']=='FULL_MARGIN' and plan['risk_rate'] is None
        assert plan['net_rr']>=2 and plan['risk_pct_of_equity']>0


def book_payload(ts):
    return {'pricebook':{'product_id':'ETH-USD','time':e.iso(ts),'bids':[{'price':'1999','size':'5'}],'asks':[{'price':'2001','size':'5'}]}}


@pytest.mark.parametrize('recovers',[True,False])
def test_stale_book_retry_never_forges_exchange_time(recovers):
    now=int(time.time());seen=[]
    def handle(req):
        seen.append(req)
        assert '_cb' in req.url.params and 'no-store' in req.headers['cache-control']
        return httpx.Response(200,json=book_payload(now if recovers and len(seen)==2 else now-30))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
            if recovers:assert (await e.read_book(c,'ETH-USD'))['ts']==now
            else:
                with pytest.raises(e.MarketDataUnavailable,match='Stale'):await e.read_book(c,'ETH-USD')
    asyncio.run(run());assert len(seen)==2


def seed_flat_signals(pair,now):
    with e.db() as c:
        for interval,count in ((60,40),(300,65),(3600,60),(86400,20)):
            end=now//interval*interval
            e.store_signal_bars(c,pair,interval,[{'ts':end-(count-i)*interval,'low':1999,'high':2001,'open':2000,'close':2000,'volume':10} for i in range(count)])


def test_scan_recovers_and_reports_real_readiness_with_empty_contract_history(monkeypatch):
    monkeypatch.setattr(e,'PAIRS',('ETH-USD',));now,q,_=inputs();seed_flat_signals('ETH-USD',now)
    q['signal_quote']={'product_id':'ETH-USD','price':2000,'ts':now}
    state={'fail':True}
    async def noop(*args):pass
    async def bars(*args):return []
    async def quote(*args):
        if state['fail']:raise e.MarketDataUnavailable('STALE_BOOK','Stale Coinbase order book')
        return q
    async def ref(*args):return q['signal_quote']
    monkeypatch.setattr(e,'refresh_news',noop);monkeypatch.setattr(e,'refresh_futures_budget',noop)
    monkeypatch.setattr(e,'fetch_signal_bars',bars);monkeypatch.setattr(e,'fetch_bars',bars)
    monkeypatch.setattr(e,'fetch_quote',quote);monkeypatch.setattr(e,'read_book',ref)
    monkeypatch.setattr(e,'news_context',lambda *args:{'available':True,'cooldown':False,'bias':'NEUTRAL','sources':[],'headlines':[]})
    async def run():
        await e.scan_once(None,True)
        assert e.dashboard()['market_status']['ETH-USD']['state']=='DATA_BLOCKED'
        state['fail']=False
        await e.scan_once(None,True)
    asyncio.run(run());data=e.dashboard()
    assert data['market_status']['ETH-USD']['state']=='READY'
    assert data['market_status']['ETH-USD']['errors']==[]
    assert all('history has gaps' not in reason for d in data['decisions'] for reason in d['reasons'])
    with e.db() as c:e.put(c,'protection_heartbeat',e.iso())
    health=json.loads(e.health().body)
    assert health['status']=='ok' and health['markets_ready']==1
    assert not data['trades']  # flat reference is healthy data, not a qualifying setup
    with e.db() as c:e.put(c,'scan_version','old-build')
    assert e.health().status_code==503


def test_scan_input_error_cannot_overwrite_saved_open_decision(monkeypatch):
    monkeypatch.setattr(e,'PAIRS',('ETH-USD',));now=int(time.time());bar=now//60*60-60
    with e.db() as c:c.execute('INSERT INTO strategy_decisions VALUES (?,?,?,?,?,?,?,?)',('ETH-USD','SNIPER_SCALP',bar,now,'OPEN_LONG','LONG','["Qualified entry"]','{}'))
    async def noop(*args):pass
    async def fail(*args):raise ValueError('fixture outage')
    monkeypatch.setattr(e,'refresh_futures_budget',noop);monkeypatch.setattr(e,'fetch_signal_bars',fail)
    asyncio.run(e.scan_once(None,False))
    with e.db() as c:assert c.execute('SELECT status FROM strategy_decisions WHERE strategy="SNIPER_SCALP"').fetchone()[0]=='OPEN_LONG'


def test_scheduled_closed_sessions_are_healthy_processes_with_no_ready_entries(monkeypatch):
    monkeypatch.setattr(e,'PAIRS',('ETH-USD',));now=int(time.time())
    with e.db() as c:
        e.put(c,'last_scan',e.iso(now));e.put(c,'scan_version',e.VERSION);e.put(c,'protection_heartbeat',e.iso(now))
        e.put(c,'market_status',json.dumps({'ETH-USD':{'state':'SESSION_CLOSED','checked_at':now,'errors':[]}}))
    d=json.loads(e.health().body)
    assert d['status']=='ok' and d['markets_ready']==0 and d['markets_closed']==1
