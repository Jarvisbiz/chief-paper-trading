"""Sniper regression fixtures; lower-fee scenarios NEVER change production fees."""
import json
import time

import pytest

import main as e
from test_engine import account, news_item, quote


def setup_market(direction="LONG", now=None):
    now = int(now or time.time())
    hour, five, minute = now // 3600 * 3600, now // 300 * 300, now // 60 * 60
    h = [{"ts":hour-(60-i)*3600,"low":99+i*.03-.05,"high":99+i*.03+.05,"open":99+i*.03-.01,"close":99+i*.03,"volume":100.} for i in range(60)]
    m = [{"ts":five-(65-i)*300,"low":99.9+i*.002-.3,"high":99.9+i*.002+.3,"open":99.9+i*.002-.001,"close":99.9+i*.002,"volume":100.} for i in range(65)]
    one = [{"ts":minute-(40-i)*60,"low":99.73,"high":99.79,"open":99.75,"close":99.76,"volume":100.} for i in range(40)]
    one[-3].update(low=99.67,high=99.77,open=99.72,close=99.73)
    one[-2].update(low=99.73,high=99.83,open=99.75,close=99.81)
    one[-1].update(low=99.76,high=99.84,open=99.78,close=99.79,volume=150.)
    if direction == "SHORT":
        for rows in (h,m,one):
            for b in rows:
                b["low"],b["high"],b["open"],b["close"] = 200-b["high"],200-b["low"],200-b["open"],200-b["close"]
    with e.db() as c:
        for interval, rows in ((3600,h),(300,m),(60,one)): e.store_bars(c,"BTC-USD",interval,rows)
    return now, quote(99.79 if direction=="LONG" else 100.21,now), one


def lower_fee_fixture(monkeypatch):
    monkeypatch.setattr(e,"FEE",.00005)
    monkeypatch.setattr(e,"SLIPPAGE",.00002)


def neutral_news(now):
    with e.db() as c: news_item(c,"CoinDesk","Bitcoin market update",now)


@pytest.mark.parametrize("direction",["LONG","SHORT"])
def test_completed_sweep_break_and_retest_in_both_directions(direction):
    now,q,one=setup_market(direction)
    with e.db() as c: ev=e.scalp_evidence(c,"BTC-USD",now)
    assert ev["technical_pass"],ev["reasons"]
    assert ev["direction"]==direction
    assert ev["sweep_at"]<ev["break_at"]<ev["retest_at"]
    assert ev["retest_at"]+60<=now


@pytest.mark.parametrize("direction",["LONG","SHORT"])
def test_high_fee_small_target_is_rejected_without_changing_assumptions(direction):
    now,q,_=setup_market(direction)
    with e.db() as c: ev=e.scalp_evidence(c,"BTC-USD",now)
    plan,error=e.scalp_trade_plan(q,ev)
    assert error and plan["net_rr"]<2
    assert plan["round_trip_fee_pct"]==pytest.approx(1.8)
    neutral_news(now)
    assert e.decide("BTC-USD",q,now,"SNIPER_SCALP")["status"]=="WAIT"
    assert not e.dashboard()["trades"]


@pytest.mark.parametrize("direction",["LONG","SHORT"])
def test_low_fee_fixture_sized_to_quarter_percent_and_fills_are_tagged(monkeypatch,direction):
    lower_fee_fixture(monkeypatch);now,q,_=setup_market(direction);neutral_news(now)
    assert e.decide("BTC-USD",q,now,"SNIPER_SCALP")["status"]=="OPEN_"+direction
    with e.db() as c: p=dict(c.execute("SELECT * FROM positions").fetchone())
    assert p["strategy"]=="SNIPER_SCALP" and p["expires"]==now+900
    assert 0<p["risk_amount"]<=500*.0025+1e-9
    ev=json.loads(p["evidence"]);assert ev["plan"]["net_rr"]>=2
    # Identical restart/candle cannot generate a duplicate paper entry.
    e.init();e.decide("BTC-USD",q,now,"SNIPER_SCALP")
    with e.db() as c: assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]==1
    e.mark_positions({"BTC-USD":quote(p["target"],now+5)},now+5)
    d=e.dashboard();t=d["trades"][0]
    assert t["strategy"]=="SNIPER_SCALP" and t["reason"]=="target"
    assert t["r_multiple"]>=2 and t["hold_seconds"]==5
    stats={s["strategy"]:s for s in d["strategy_results"]}
    assert stats["SNIPER_SCALP"]["completed"]==1 and stats["BREAKOUT"]["completed"]==0
    assert d["cash"]==pytest.approx(500+t["pnl"],abs=1e-5)


@pytest.mark.parametrize("direction",["LONG","SHORT"])
def test_sniper_time_stop_closes_on_fresh_quote_even_when_entries_paused(monkeypatch,direction):
    lower_fee_fixture(monkeypatch);now,q,_=setup_market(direction);neutral_news(now)
    e.decide("BTC-USD",q,now,"SNIPER_SCALP")
    with e.db() as c: e.put(c,"emergency_paused","true")
    e.mark_positions({"BTC-USD":quote(q["price"],now+901)},now+901)
    d=e.dashboard();assert not d["positions"]
    assert d["trades"][0]["reason"]=="time_stop"


@pytest.mark.parametrize("direction",["LONG","SHORT"])
def test_cost_adjusted_breakeven_after_one_net_r_never_loosens(monkeypatch,direction):
    lower_fee_fixture(monkeypatch);now,q,_=setup_market(direction);neutral_news(now)
    e.decide("BTC-USD",q,now,"SNIPER_SCALP")
    with e.db() as c: p=dict(c.execute("SELECT * FROM positions").fetchone())
    sign=e.direction_sign(direction);price=p["entry"]*(1+sign*.003)
    e.mark_positions({"BTC-USD":quote(price,now+5)},now+5)
    with e.db() as c: stop=c.execute("SELECT stop FROM positions").fetchone()[0]
    assert sign*(stop-p["entry"])>0  # covers fees, not a raw-price breakeven
    e.mark_positions({"BTC-USD":quote(price,now+10)},now+10)
    with e.db() as c: assert sign*(c.execute("SELECT stop FROM positions").fetchone()[0]-stop)>=0


def test_missing_break_or_retest_never_qualifies():
    now,q,one=setup_market()
    with e.db() as c:
        c.execute("UPDATE bars SET close=99.75,high=99.76 WHERE pair='BTC-USD' AND interval=60 AND ts=?",(one[-2]["ts"],))
        ev=e.scalp_evidence(c,"BTC-USD",now)
    assert not ev["technical_pass"] and any("sequence" in r for r in ev["reasons"])


def test_future_confirmation_and_gaps_do_not_create_a_setup():
    now,q,one=setup_market()
    with e.db() as c:
        c.execute("UPDATE bars SET ts=? WHERE pair='BTC-USD' AND interval=60 AND ts=?",(now//60*60,one[-1]["ts"]))
        assert not e.scalp_evidence(c,"BTC-USD",now)["technical_pass"]


def test_two_strategies_keep_separate_decisions_but_share_positions_and_risk(monkeypatch):
    lower_fee_fixture(monkeypatch);now,q,_=setup_market();neutral_news(now)
    e.decide("BTC-USD",q,now,"SNIPER_SCALP")
    other=e.decide("BTC-USD",q,now,"BREAKOUT")
    assert other["status"]=="WAIT" and any("already open" in r for r in other["reasons"])
    d=e.dashboard();assert len(d["positions"])==1
    assert {v["strategy"] for v in d["decisions"]}==set(e.STRATEGIES)
    assert sum(s["entries"] for s in d["strategy_results"])==1


def test_sniper_requires_newer_quote_and_cannot_bypass_daily_halt(monkeypatch):
    lower_fee_fixture(monkeypatch);now,q,_=setup_market();neutral_news(now)
    stale=e.decide("BTC-USD",{**q,"ts":now-20},now,"SNIPER_SCALP")
    assert stale["status"]=="WAIT" and any("bid/ask" in r for r in stale["reasons"])
    with e.db() as c:
        e.roll_day(c,now);e.put(c,"cash",480)
    halted=e.decide("BTC-USD",q,now,"SNIPER_SCALP")
    assert halted["status"]=="WAIT" and any("halt" in r for r in halted["reasons"])


def test_old_decisions_are_migrated_without_rewriting_results():
    now=int(time.time())
    with e.db() as c: c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?)",("BTC-USD",now,now,"OPEN_LONG","LONG","[]","{}"))
    e.init();e.init()
    with e.db() as c:
        row=c.execute("SELECT * FROM strategy_decisions").fetchone()
        assert row["strategy"]=="BREAKOUT" and row["status"]=="OPEN_LONG"
        assert c.execute("SELECT COUNT(*) FROM strategy_decisions").fetchone()[0]==1


def test_five_trade_ceiling_is_combined_not_five_per_strategy():
    now=int(time.time())
    with e.db() as c:
        for strategy in ["BREAKOUT"]*3+["SNIPER_SCALP"]*2:
            c.execute("INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl,action,strategy) VALUES (?,'BTC-USD','BUY',1,100,0,'fixture',NULL,'OPEN_LONG',?)",(now,strategy))
        assert any("Five-trade" in r for r in e.risk_blocks(c,now))
