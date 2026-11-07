"""Offline fixtures verify mechanics; fixture trades are never production results."""
import asyncio
import json
import time
from datetime import datetime,timezone

import httpx
import pytest

import main as e
import futures_runtime as f
import practice_budget as b
from test_engine import account
from test_budget import product


@pytest.fixture(autouse=True)
def venue(monkeypatch):
    monkeypatch.setattr(e,'PRACTICE_VENUE','COINBASE_US_FUTURES')


def inputs(now=None,price=2000,side='LONG'):
    now=int(now or time.time());raw=product(now);raw['price']=str(price)
    raw['base_max_size']='5000'
    s=b.contract_snapshot(raw,now)
    q={'price':price,'bid':price-.5,'ask':price+.5,'ts':now,'bids':[(price-.5,100.)],'asks':[(price+.5,100.)],'product_id':s['product_id'],'contract':s}
    ev={'strategy':'SNIPER_SCALP','bar':now//60*60-60,'structural_stop':price-5 if side=='LONG' else price+5,'opposite_range_level':price+60 if side=='LONG' else price-60,'atr_5m':25}
    return now,q,ev


def fund(amount=1000):
    with e.db() as c:
        e.put(c,'cash',amount);e.put(c,'day','');e.put(c,'starting_cash',amount)


def opened(direction='LONG',strategy='SNIPER_SCALP',amount=1000,now=None):
    fund(amount);now,q,ev=inputs(now,side=direction);ev['strategy']=strategy
    with e.db() as c:
        assert e.open_position(c,'ETH-USD',direction,q,ev,now) is None
        p=dict(c.execute('SELECT * FROM positions').fetchone())
    return now,q,p


def move(q,price,now):
    return {**q,'price':price,'bid':price-.5,'ask':price+.5,'ts':now,'bids':[(price-.5,100.)],'asks':[(price+.5,100.)],'contract':{**q['contract'],'observed_at':now}}


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_whole_contract_two_sides_net_costs_and_exit_cash_identity(direction):
    now,q,p=opened(direction)
    assert p['contracts']>=1 and p['contracts']==int(p['contracts'])
    assert p['qty']==pytest.approx(p['contracts']*.1)
    assert p['entry_fee']==pytest.approx(f.fee(p['contracts'],.1,p['entry']))
    assert p['risk_amount']<=1000*e.SCALP_RISK
    assert json.loads(p['evidence'])['plan']['net_rr']>=2
    q2=move(q,p['target']+(1 if direction=='LONG' else -1),now+5)
    e.mark_positions({'ETH-USD':q2},now+5)
    d=e.dashboard();close=d['trades'][0]
    assert len(d['positions'])==0 and close['action']=='CLOSE_'+direction and close['reason']=='target'
    assert d['cash']==pytest.approx(1000+close['pnl'],abs=1e-5)
    assert close['funding']==0 and close['contracts']==p['contracts']
    assert close['pnl']>0 and d['fee_rate']==.001
    assert d['fee_profile']['futures_reference']['applied'] is True


@pytest.mark.parametrize('balance',[80,100,150])
def test_user_balances_cannot_round_up_or_bypass_risk(balance):
    fund(balance);now,q,ev=inputs()
    with e.db() as c:
        err=e.open_position(c,'ETH-USD','LONG',q,ev,now)
        assert 'risk allowance' in err or 'exposure ceiling' in err
        assert c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==0
        assert float(e.get(c,'cash'))==balance


def test_cost_gate_uses_futures_not_spot_fee_and_does_not_extend_target():
    fund();now,q,ev=inputs();ev['opposite_range_level']=2010;ev['atr_5m']=4
    with e.db() as c:
        p,err=f.plan(e,c,'ETH-USD','LONG',q,ev,now)
        assert 'below 2R' in err
        assert p['target']<=2008.5
        assert p['fee_rate']==.001


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_daily_variation_is_once_only_and_full_pnl_reconciles(direction):
    # Fixed timestamp spans 5pm New York. Keep the same dated contract throughout.
    now=int(datetime(2026,11,9,21,59,tzinfo=timezone.utc).timestamp())
    now,q,p=opened(direction,'BREAKOUT',10000,now)
    mark=2010 if direction=='LONG' else 1990
    q2=move(q,mark,now+120)
    e.mark_positions({'ETH-USD':q2},now+120)
    before=e.dashboard();assert len(before['settlements'])==1
    e.init();e.mark_positions({'ETH-USD':q2},now+120)
    after=e.dashboard();assert len(after['settlements'])==1
    assert after['equity']==pytest.approx(before['equity'])
    p=after['positions'][0]
    assert p['settlement_basis']==mark and p['vm_total']!=0
    with e.db() as c:e.close_position(c,p,move(q,mark+1,now+125),'fixture_exit',now+125)
    d=e.dashboard();assert d['cash']==pytest.approx(10000+d['trades'][0]['pnl'],abs=1e-5)
    assert d['totals']['completed']==1  # variation transfers aren't trades


def test_gap_loss_and_negative_balance_are_not_capped_at_margin():
    now,q,p=opened('SHORT','BREAKOUT',10000)
    q2=move(q,50000,now+5)
    e.mark_positions({'ETH-USD':q2},now+5)
    d=e.dashboard();assert not d['positions'] and d['cash']<0
    assert d['trades'][0]['reason']=='margin_protection'
    assert d['trades'][0]['pnl'] < -p['margin']
    with e.db() as c:assert any('deficit' in r for r in e.risk_blocks(c,now+6))


def test_margin_rate_increase_preserves_equity_and_can_force_exit():
    now,q,p=opened('LONG','BREAKOUT',10000)
    with e.db() as c:
        e.put(c,'cash',10.)
        before=e.equity_value(c)
    q2=move(q,2000,now+5);q2['contract']['margin_rates']['long']=.99
    e.mark_positions({'ETH-USD':q2},now+5)
    d=e.dashboard();assert not d['positions']
    assert d['trades'][0]['reason']=='margin_protection'
    # Re-reserving margin cannot manufacture a profit.
    assert d['equity']<before


def test_stop_time_pause_and_expiry_protect_even_while_entries_paused():
    now,q,p=opened()
    with e.db() as c:e.put(c,'emergency_paused','true')
    q2=move(q,2000,now+e.SCALP_MAX_HOLD+1)
    e.mark_positions({'ETH-USD':q2},now+e.SCALP_MAX_HOLD+1)
    assert e.dashboard()['trades'][0]['reason']=='time_stop'
    with e.db() as c:e.put(c,'emergency_paused','false')
    now,q,p=opened('LONG','BREAKOUT',10000,now+2000)
    q2=move(q,2000,now+5);q2['contract']['expiry']=now+86399
    e.mark_positions({'ETH-USD':q2},now+5)
    assert e.dashboard()['trades'][0]['reason']=='expiry_exit'


def test_stale_wrong_contract_closed_and_shallow_books_never_fill():
    fund();now,q,ev=inputs()
    with e.db() as c:
        wrong={**q,'product_id':'ETH-USD'}
        assert 'identity mismatch' in e.open_position(c,'ETH-USD','LONG',wrong,ev,now)
        stale={**q,'ts':now-11}
        assert 'book unavailable' in e.open_position(c,'ETH-USD','LONG',stale,ev,now)
    now,q,p=opened()
    shallow=move(q,1900,now+5);shallow['bids']=[(1899.5,.00001)]
    e.mark_positions({'ETH-USD':shallow},now+5)
    assert len(e.dashboard()['positions'])==1 and len(e.dashboard()['trades'])==1
    closed=move(q,1900,now+6);closed['contract']['session_open']=False
    e.mark_positions({'ETH-USD':closed},now+6)
    assert len(e.dashboard()['trades'])==1
    with e.db() as c:assert any('exit pending' in x for x in e.risk_blocks(c,now+6))
    recovered=move(q,2000,now+7)
    e.mark_positions({'ETH-USD':recovered},now+7)
    assert e.dashboard()['trades'][0]['reason']=='stop'


def test_futures_candles_and_book_use_contract_identity_and_size_units():
    now,q,ev=inputs();s=q['contract']
    with e.db() as c:
        e.put(c,'futures_snapshots',json.dumps({'ETH-USD':s}));e.put(c,'futures_contracts',json.dumps({s['product_id']:s}))
    seen=[]
    def handler(req):
        seen.append(req)
        assert req.method=='GET'
        if req.url.path.endswith('/candles'):
            assert s['product_id'] in req.url.path
            return httpx.Response(200,json={'candles':[{'start':str(now//60*60-60),'low':'1999','high':'2001','open':'2000','close':'2000','volume':'5'}]})
        assert req.url.params['product_id']==s['product_id']
        return httpx.Response(200,json={'pricebook':{'product_id':s['product_id'],'time':e.iso(now),'bids':[{'price':'1999.5','size':'2'}],'asks':[{'price':'2000.5','size':'3'}]}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            bars=await e.fetch_bars(client,'ETH-USD',60);quote=await e.fetch_quote(client,'ETH-USD')
            assert bars[0]['close']==2000
            assert quote['asks'][0][1]==pytest.approx(.3)
            assert quote['bids'][0][1]==pytest.approx(.2)
    asyncio.run(run());assert len(seen)==2


def test_forward_decision_news_risk_and_restart_idempotence(monkeypatch):
    fund();now,q,ev=inputs();ev.update(direction='LONG',technical_pass=True,reasons=[],one_minute_close=q['price'])
    monkeypatch.setattr(e,'scalp_evidence',lambda *args:dict(ev))
    news={'available':True,'bias':'NEUTRAL','cooldown':False,'coverage':'fixture','headlines':[],'sources':[]}
    monkeypatch.setattr(e,'news_context',lambda *args:news)
    d=e.decide('ETH-USD',q,now,'SNIPER_SCALP');assert d['status']=='OPEN_LONG'
    e.init();again=e.decide('ETH-USD',q,now,'SNIPER_SCALP')
    assert again['status']=='WAIT'
    with e.db() as c:assert c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==1
    p=e.dashboard()['positions'][0]
    e.mark_positions({'ETH-USD':move(q,p['stop']-1,now+5)},now+5)
    assert e.dashboard()['trades'][0]['reason']=='stop'


def test_contract_chart_does_not_substitute_old_spot_bars():
    now,q,_=inputs();s=q['contract']
    with e.db() as c:
        e.put(c,'futures_snapshots',json.dumps({'ETH-USD':s}));e.put(c,'futures_contracts',json.dumps({s['product_id']:s}))
        e.store_bars(c,'ETH-USD',300,[{'ts':now//300*300-300,'low':1990,'high':2010,'open':2000,'close':2001,'volume':3}])
    assert e.contract_candles('ETH-USD',300).status_code==503
    with e.db() as c:e.put(c,'bar_product_ETH-USD',s['product_id'])
    d=e.contract_candles('ETH-USD',300)
    assert d['source']=='FUTURES_CONTRACT_CANDLES' and d['product_id']==s['product_id']
    assert e.contract_candles('ETH-USD',42).status_code==400


def test_quiet_valid_futures_candles_are_not_fabricated_or_transport_failure():
    now,q,_=inputs();s=q['contract']
    with e.db() as c:e.put(c,'futures_snapshots',json.dumps({'ETH-USD':s}))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'candles':[]}))) as client:
            assert await e.fetch_bars(client,'ETH-USD',60)==[]
    asyncio.run(run())
