import time

import pytest
import main as e
import practice_budget as p
from test_engine import account, quote


def product(now):
    return {'product_id':'ET-27NOV26-CDE','product_type':'FUTURE','price':'2250','base_min_size':'1','base_increment':'1','price_increment':'.5','future_product_details':{'contract_size':'.1','contract_expiry':e.iso(now+20*86400),'intraday_margin_rate':{'long_margin_rate':'.1','short_margin_rate':'.1'},'overnight_margin_rate':{'long_margin_rate':'.237','short_margin_rate':'.2995'}},'fcm_trading_session_details':{'is_session_open':True}}


def test_minimum_contract_fee_and_risk_floor():
    now=int(time.time());snap=p.contract_snapshot(product(now),now)
    d=p.assess(snap,80,now=now)
    assert d['minimum_contracts']==1 and d['notional']==225
    assert d['margin']==pytest.approx(225*.237)
    assert d['round_trip_fee_floor']==pytest.approx(.225+.224325+.30)
    assert d['planned_loss_floor']>d['allowed_loss'] and not d['fits']
    assert p.assess(snap,150,now=now)['fits'] is False
    assert p.assess(snap,1000,now=now)['fits'] is True
    assert p.assess(snap,80,'SHORT',now=now)['margin']>d['margin']


def test_stale_closed_and_missing_specs_never_claim_affordability():
    now=int(time.time());snap=p.contract_snapshot(product(now),now)
    assert not p.assess(snap,80,now=now+601)['available']
    assert not p.assess(None,80,now=now)['available']
    snap['session_open']=False
    assert not p.assess(snap,1000,now=now)['fits']
    raw=product(now);raw['base_min_size']='.5'
    with pytest.raises(ValueError):p.contract_snapshot(raw,now)
    raw=product(now);raw['price']='nan'
    with pytest.raises(ValueError):p.contract_snapshot(raw,now)


def test_no_proxy_fill_can_be_recorded_as_us_futures(monkeypatch):
    monkeypatch.setattr(e,'PRACTICE_VENUE','COINBASE_US_FUTURES')
    now=int(time.time())
    with e.db() as c:
        error=e.open_position(c,'ETH-USD','LONG',quote(),{'bar':now},now)
        assert 'No spot-proxy substitute' in error or 'specifications unavailable' in error
        assert c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==0


def test_new_small_account_persists_without_reset_on_restart(monkeypatch,tmp_path):
    monkeypatch.setattr(e,'DB_PATH',str(tmp_path/'new.sqlite'));monkeypatch.setattr(e,'START_CASH',80.)
    e.init()
    with e.db() as c:
        assert float(e.get(c,'cash'))==80
        e.put(c,'cash',79.)
    e.init()
    with e.db() as c:
        assert float(e.get(c,'cash'))==79
        assert float(e.get(c,'starting_cash'))==80
    monkeypatch.setattr(e,'PRACTICE_VENUE','COINBASE_US_FUTURES')
    d=e.dashboard();assert d['account_profile']['starting_cash']==80
    assert d['futures_budget']['execution_ready']
    assert set(d['futures_budget']['rows'][0]['comparisons'])=={'250','280'}

