"""Release checks: $250 budget, expanded contracts and explicit ORB behavior."""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import main as e
import practice_budget as b
from test_engine import account
from test_budget import product
from test_futures import inputs, fund, move


def test_new_250_run_is_durable_and_old_run_is_preserved(monkeypatch,tmp_path):
    old=tmp_path/'80.sqlite';new=tmp_path/'250.sqlite'
    monkeypatch.setattr(e,'DB_PATH',str(old));monkeypatch.setattr(e,'START_CASH',80.)
    e.init()
    with e.db() as c:e.put(c,'cash',77.)
    monkeypatch.setattr(e,'DB_PATH',str(new));monkeypatch.setattr(e,'START_CASH',250.)
    e.init()
    with e.db() as c:
        assert float(e.get(c,'cash'))==250
        e.put(c,'cash',247.)
    e.init()
    with e.db() as c:assert float(e.get(c,'cash'))==247
    monkeypatch.setattr(e,'DB_PATH',str(old))
    with e.db() as c:assert float(e.get(c,'cash'))==77


@pytest.mark.parametrize('pair,root',[('XRP-USD','XRP'),('AVAX-USD','AVA'),('DOGE-USD','DOG'),('HBAR-USD','HED')])
def test_expanded_crypto_uses_verified_root_and_whole_contract(pair,root):
    now=1794050000;raw=product(now);raw['product_id']=root+'-27NOV26-CDE'
    assert b.contract_snapshot(raw,now)['pair']==pair
    raw['product_id']='GOL-27NOV26-CDE'
    with pytest.raises(ValueError):b.contract_snapshot(raw,now)


def test_perpetual_compatibility_expiry_cannot_bypass_no_funding_adapter():
    now=1794050000;raw=product(now)
    raw['future_product_details'].update(contract_expiry='2089-12-30T16:00:00Z',funding_interval='3600s',funding_rate='0')
    with pytest.raises(ValueError,match='funding adapter'):b.contract_snapshot(raw,now)


def test_xrp_side_specific_250_280_affordability():
    now=1794050000;raw=product(now);raw.update(product_id='XRP-27NOV26-CDE',price='1.1667')
    raw['future_product_details'].update(contract_size='500',overnight_margin_rate={'long_margin_rate':'.3605','short_margin_rate':'.54275'})
    snap=b.contract_snapshot(raw,now)
    for cash in (250,280):
        long=b.allocation(snap,cash,'LONG',now);short=b.allocation(snap,cash,'SHORT',now)
        assert long['affordable'] and long['max_contracts']==1
        assert not short['affordable'] and short['max_contracts']==0
        assert short['minimum_cash_floor']>317
    assert b.allocation(snap,250,'LONG',now+601)['available'] is False


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_orb_250_full_margin_round_trip_and_deadline(monkeypatch,direction):
    monkeypatch.setattr(e,'PRACTICE_VENUE','COINBASE_US_FUTURES');monkeypatch.setattr(e,'SIGNAL_SOURCE','CONTRACT');monkeypatch.setattr(e,'SIZING_MODE','FULL_MARGIN')
    fund(250);now,q,ev=inputs(side=direction);ev['strategy']='ORB'
    with e.db() as c:
        assert e.open_position(c,'ETH-USD',direction,q,ev,now) is None
        p=dict(c.execute('SELECT * FROM positions').fetchone())
        assert p['expires']==now+1800 and p['strategy']=='ORB'
        assert p['contracts']>=1 and json.loads(p['evidence'])['plan']['net_rr']>=2
    q2=move(q,p['target']+(1 if direction=='LONG' else -1),now+5)
    e.mark_positions({'ETH-USD':q2},now+5)
    data=e.dashboard()
    assert not data['positions'] and data['trades'][0]['action']=='CLOSE_'+direction
    assert data['cash']==pytest.approx(250+data['trades'][0]['pnl'],abs=1e-5)
    assert any(s['strategy']=='ORB' and s['completed']==1 for s in data['strategy_results'])


def seed_orb(direction):
    now=int(datetime(2026,11,6,10,0,tzinfo=ZoneInfo('America/New_York')).timestamp());start=now-1800
    rows=[]
    for interval,count in ((3600,60),(300,65),(60,140)):
        for i in range(count):
            ts=now-(count-i)*interval
            close=2000+(i-count+1)*2 if interval==3600 else 2000 if ts<start+900 else 2003
            low,high=close-1,close+1
            if interval==300:low,high=(1972,2002) if start<=ts<start+900 else (close-20,close+20)
            if interval==60 and ts==now-60:low,high=1999,2004
            row={'ts':ts,'low':low,'high':high,'open':close-.2,'close':close,'volume':25 if interval==60 and ts==now-60 else 10}
            if direction=='SHORT':row={**row,**{k:4000-row[k] for k in ('open','close')},'low':4000-high,'high':4000-low}
            rows.append((interval,row))
    with e.db() as c:
        for interval in (60,300,3600):e.store_bars(c,'ETH-USD',interval,[r for n,r in rows if n==interval])
    return now


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_orb_requires_real_range_then_ordered_break_retest_and_htf(monkeypatch,direction):
    monkeypatch.setattr(e,'SIGNAL_SOURCE','CONTRACT');now=seed_orb(direction)
    with e.db() as c:
        ev=e.orb_evidence(c,'ETH-USD',now)
        assert ev['technical_pass'],ev['reasons']
        assert ev['direction']==direction and ev['break_at']<ev['retest_at']
        c.execute('DELETE FROM bars WHERE interval=300 AND ts=?',(now-1800,))
        assert not e.orb_evidence(c,'ETH-USD',now)['technical_pass']


def test_orb_does_not_trade_outside_named_window():
    now=int(datetime(2026,11,7,10,0,tzinfo=ZoneInfo('America/New_York')).timestamp())
    with e.db() as c:
        ev=e.orb_evidence(c,'ETH-USD',now)
        assert not ev['technical_pass'] and 'weekday' in ev['reasons'][0]


def test_catalog_pagination_fetches_front_contract_after_first_100(monkeypatch):
    now=1794050000;raw=product(now);raw['future_product_details']['contract_expiry']='2027-11-27T16:00:00Z'
    raw['future_product_details'].update(funding_interval=None,funding_rate='')
    # Public dated products use an empty funding string, not a payable zero rate.
    seen=[]
    def handle(req):
        if req.url.path.endswith('/ET-27NOV26-CDE'):return httpx.Response(200,json=raw)
        offset=int(req.url.params.get('offset','0'));seen.append(offset)
        thin={k:v for k,v in raw.items() if k!='price'}
        page=[{'product_id':f'US5-{i}'} for i in range(100)] if offset==0 else [thin]
        return httpx.Response(200,json={'products':page})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:await e.refresh_futures_budget(c)
    asyncio.run(run())
    with e.db() as c:
        snaps=json.loads(e.get(c,'futures_snapshots'))
        assert snaps['ETH-USD']['product_id']=='ET-27NOV26-CDE'
        assert set(snaps)=={'ETH-USD'}
    assert seen==[0,100]


def test_contract_underlying_units_must_match_reference():
    now=1794050000;raw=product(now)
    raw['future_product_details']['contract_root_unit']='1000ETH'
    with pytest.raises(ValueError,match='underlying unit'):b.contract_snapshot(raw,now)
    raw['future_product_details'].update(contract_root_unit='ETH',non_crypto=True)
    with pytest.raises(ValueError,match='underlying unit'):b.contract_snapshot(raw,now)
