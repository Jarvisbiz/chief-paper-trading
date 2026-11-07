"""Verified fee snapshot and size-dependent depth execution regressions."""
import time

import pytest

import main as e
from test_engine import account, quote, open_one


def book(now=None):
    now = int(now or time.time())
    return {"pricebook": {"product_id": "BTC-USD", "time": e.iso(now), "bids": [{"price": "99", "size": "1"}, {"price": "98", "size": "2"}], "asks": [{"price": "101", "size": "1"}, {"price": "102", "size": "2"}]}}


def test_book_validates_identity_freshness_crossing_and_size():
    now=int(time.time()); raw=book(now)
    q=e.normalize_book(raw,"BTC-USD",now)
    assert q["price"]==100 and q["bid"]==99 and q["ask"]==101
    with pytest.raises(ValueError): e.normalize_book(raw,"ETH-USD",now)
    with pytest.raises(ValueError): e.normalize_book(raw,"BTC-USD",now+11)
    raw["pricebook"]["asks"][0]["size"]="nan"
    with pytest.raises(ValueError):e.normalize_book(raw,"BTC-USD",now)
    raw=book(now);raw["pricebook"]["bids"][0]["price"]="200"
    with pytest.raises(ValueError):e.normalize_book(raw,"BTC-USD",now)


def test_larger_orders_walk_the_book_and_measure_adverse_impact():
    q=e.normalize_book(book(),"BTC-USD",time.time())
    assert e.book_vwap(q,"BUY",qty=.5)==101
    assert e.book_vwap(q,"BUY",qty=2)==101.5
    assert e.book_vwap(q,"SELL",qty=2)==98.5
    assert e.book_vwap(q,"BUY",notional=203)==101.5
    assert e.impact_rate(q,"BUY",qty=.5)==0
    assert e.impact_rate(q,"BUY",qty=2)>0
    assert e.impact_rate(q,"SELL",qty=2)>0


def test_insufficient_depth_never_becomes_a_fabricated_full_fill():
    q=e.normalize_book(book(),"BTC-USD",time.time())
    with pytest.raises(ValueError):e.book_vwap(q,"BUY",qty=4)
    with pytest.raises(ValueError):e.book_vwap(q,"SELL",notional=1000)
    with pytest.raises(ValueError):e.book_vwap(q,"BUY",qty=-1)
    with pytest.raises(ValueError):e.book_vwap({"bid":99,"ask":101},"BUY",qty=1)


def test_no_blanket_slippage_or_double_charged_spread(monkeypatch):
    monkeypatch.setattr(e,"SLIPPAGE",0)
    q=e.normalize_book(book(),"BTC-USD",time.time())
    assert e.execution_price(q,"BUY",qty=1)==101
    assert e.execution_price(q,"SELL",qty=1)==99
    monkeypatch.setattr(e,"SLIPPAGE",.001)
    assert e.execution_price(q,"BUY",qty=1)==pytest.approx(101*1.001)
    assert e.execution_price(q,"SELL",qty=1)==pytest.approx(99*.999)


def test_verified_taker_snapshot_is_used_and_records_remain_reconcilable(monkeypatch):
    monkeypatch.setattr(e,"FEE",.009);monkeypatch.setattr(e,"SLIPPAGE",0)
    now=int(time.time());p=open_one(now=now)
    assert p["entry_fee"]==pytest.approx(p["qty"]*p["entry"]*.009)
    e.mark_positions({"BTC-USD":quote(107,now+5)},now+5)
    d=e.dashboard();t=d["trades"][0]
    assert t["evidence"]["execution"]["fee_rate"]==.009
    assert t["evidence"]["exit_execution"]["fee_rate"]==.009
    assert d["cash"]==pytest.approx(500+t["pnl"],abs=1e-5)
    assert d["slippage_model"]=="COINBASE_BOOK_VWAP"
    assert d["fee_profile"]["futures_reference"]["applied"] is False


def test_exit_stays_open_when_loaded_depth_cannot_cover_position(monkeypatch):
    monkeypatch.setattr(e,"SLIPPAGE",0)
    now=int(time.time());p=open_one(now=now)
    q=quote(90,now+5);q["bids"]=[(90,.000001)]
    e.mark_positions({"BTC-USD":q},now+5)
    d=e.dashboard()
    assert len(d["positions"])==1 and len(d["trades"])==1
    assert d["positions"][0]["funding_at"]==now+5

@pytest.mark.parametrize('direction,price', [('LONG',104),('SHORT',96)])
def test_breakeven_preserves_recorded_entry_fee_when_current_tier_changes(monkeypatch,direction,price):
    monkeypatch.setattr(e,'FEE',.001);monkeypatch.setattr(e,'SLIPPAGE',0)
    now=int(time.time());p=open_one(direction,now=now)
    with e.db() as c:
        c.execute("UPDATE positions SET strategy='SNIPER_SCALP',target=NULL,risk_amount=.001*qty*entry WHERE pair='BTC-USD'")
    monkeypatch.setattr(e,'FEE',.009)
    e.mark_positions({'BTC-USD':quote(price,now+5)},now+5)
    with e.db() as c:p=dict(c.execute('SELECT * FROM positions').fetchone())
    ratio=p['stop']/p['entry'];sign=e.direction_sign(direction)
    net=sign*(ratio-1)-p['entry_fee']/(p['qty']*p['entry'])-e.FEE*ratio-p['funding']/(p['qty']*p['entry'])
    assert net==pytest.approx(0,abs=1e-12)
