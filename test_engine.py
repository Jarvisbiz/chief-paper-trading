"""Offline execution/accounting regression tests. Fixtures are NOT performance results."""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import formatdate

import httpx
import pytest

import main as e


@pytest.fixture(autouse=True)
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(e, "DB_PATH", str(tmp_path / "paper.sqlite"))
    monkeypatch.setattr(e, "PAUSED", False)
    monkeypatch.setattr(e, "LEVERAGE", 2.)
    monkeypatch.setenv("DASHBOARD_USER", "fixture")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "fixture-password")
    e.init()


def quote(price=100, now=None):
    return {"price": price, "bid": price, "ask": price, "ts": int(now or time.time())}


def open_one(direction="LONG", pair="BTC-USD", now=None):
    now = int(now or time.time())
    with e.db() as c:
        assert e.open_position(c, pair, direction, quote(now=now), {"bar": now // 300 * 300 - 300}, now) is None
    with e.db() as c:
        return dict(c.execute("SELECT * FROM positions WHERE pair=?", (pair,)).fetchone())


@pytest.mark.parametrize("direction,price", [("LONG", 107), ("SHORT", 93)])
def test_two_directions_profit_and_cash_identity(direction, price):
    now = int(time.time()); p = open_one(direction, now=now)
    with e.db() as c:
        assert float(e.get(c, "cash")) == pytest.approx(500 - p["margin"] - p["entry_fee"])
        assert p["margin"] * 2 == pytest.approx(p["qty"] * p["entry"])
        assert e.equity_value(c) < 500  # entry fee and adverse execution
    e.mark_positions({"BTC-USD": quote(price, now + 15)}, now + 15)
    with e.db() as c:
        assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        t = dict(c.execute("SELECT * FROM trades WHERE action LIKE 'CLOSE_%'").fetchone())
        assert t["action"] == "CLOSE_" + direction
        assert t["side"] == ("SELL" if direction == "LONG" else "BUY")
        assert t["pnl"] > 0 and t["reason"] == "target"
        assert float(e.get(c, "cash")) == pytest.approx(500 + t["pnl"])
        assert t["funding"] > 0


@pytest.mark.parametrize("direction,price", [("LONG", 96), ("SHORT", 104)])
def test_stops_fire_and_preserve_cash_identity(direction, price):
    now = int(time.time()); open_one(direction, now=now)
    e.mark_positions({"BTC-USD": quote(price, now + 15)}, now + 15)
    with e.db() as c:
        t = dict(c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 1").fetchone())
        assert t["reason"] == "stop" and t["pnl"] < 0
        assert float(e.get(c, "cash")) == pytest.approx(500 + t["pnl"])


@pytest.mark.parametrize("direction,price", [("LONG", 1), ("SHORT", 500)])
def test_liquidation_gap_is_isolated_not_unlimited_loss(direction, price):
    now = int(time.time()); p = open_one(direction, now=now)
    e.mark_positions({"BTC-USD": quote(price, now + 15)}, now + 15)
    with e.db() as c:
        t = dict(c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 1").fetchone())
        assert t["reason"] == "liquidation"
        assert t["pnl"] == pytest.approx(-p["margin"] - p["entry_fee"])
        assert float(e.get(c, "cash")) >= 0


@pytest.mark.parametrize("direction,prices", [("LONG", [104, 103.5]), ("SHORT", [96, 96.5])])
def test_trailing_stop_never_loosens(direction, prices):
    now = int(time.time()); p = open_one(direction, now=now)
    e.mark_positions({"BTC-USD": quote(prices[0], now + 15)}, now + 15)
    with e.db() as c: first = c.execute("SELECT stop FROM positions").fetchone()[0]
    assert e.direction_sign(direction) * (first - p["stop"]) > 0
    e.mark_positions({"BTC-USD": quote(prices[1], now + 30)}, now + 30)
    with e.db() as c: assert c.execute("SELECT stop FROM positions").fetchone()[0] == first


def test_pause_blocks_entry_but_never_protective_exit():
    now = int(time.time()); open_one(now=now)
    with e.db() as c:
        e.put(c, "emergency_paused", "true")
        assert any("paused" in x for x in e.risk_blocks(c, now))
    e.mark_positions({"BTC-USD": quote(90, now + 15)}, now + 15)
    with e.db() as c: assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_stale_quote_cannot_close_or_revalue_position():
    now = int(time.time()); p = open_one(now=now)
    e.mark_positions({"BTC-USD": quote(1, now - 100)}, now)
    with e.db() as c:
        assert c.execute("SELECT mark FROM positions").fetchone()[0] == p["mark"]
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_restart_preserves_every_record_and_margin():
    open_one("SHORT")
    with e.db() as c: before = e.equity_value(c)
    e.init()
    with e.db() as c:
        assert e.equity_value(c) == pytest.approx(before)
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        assert c.execute("SELECT direction FROM positions").fetchone()[0] == "SHORT"


def test_legacy_spot_migration_keeps_equity_and_history(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.sqlite"); monkeypatch.setattr(e, "DB_PATH", path)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE state(k TEXT PRIMARY KEY,v TEXT NOT NULL)")
        c.executemany("INSERT INTO state VALUES (?,?)", [("cash", "399.4"), ("day", "")])
        c.execute("CREATE TABLE positions(pair TEXT PRIMARY KEY,qty REAL,entry REAL,entry_fee REAL,stop REAL,peak REAL,opened INTEGER)")
        c.execute("INSERT INTO positions VALUES ('BTC-USD',1,100,.6,97,100,100)")
        c.execute("CREATE TABLE trades(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,pair TEXT,side TEXT,qty REAL,price REAL,fee REAL,reason TEXT,pnl REAL)")
        c.execute("INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl) VALUES (100,'BTC-USD','BUY',1,100,.6,'legacy',NULL)")
    e.init(); e.init()
    with e.db() as c:
        assert e.equity_value(c) == pytest.approx(499.4)
        p = dict(c.execute("SELECT * FROM positions").fetchone())
        assert p["margin"] == 100 and p["leverage"] == 1
        assert c.execute("SELECT action FROM trades").fetchone()[0] == "OPEN_LONG"


def test_daily_halt_is_latched_and_next_day_resets():
    now = int(time.time())
    with e.db() as c:
        e.roll_day(c, now); e.put(c, "cash", 489); e.roll_day(c, now)
        assert e.get(c, "daily_halt") == "true"
        e.put(c, "cash", 500)
        assert any("halt" in x for x in e.risk_blocks(c, now))
        e.roll_day(c, now + 86400)
        assert e.get(c, "daily_halt") == "false"


def test_size_includes_costs_inside_half_percent_planned_risk():
    p = open_one()
    notional = p["qty"] * p["entry"]
    assumed_risk = notional * (e.STOP + e.FEE * (2 + e.STOP) + 2 * e.SLIPPAGE + 3 * e.HOLDING_RATE)
    assert assumed_risk <= 2.5 + 1e-9


def news_item(c, source, title, now, age=3600, first_seen_age=3600):
    assets, bias, urgent = e.classify_headline(title)
    c.execute("INSERT OR REPLACE INTO news_sources VALUES (?,?,?,?,?)", (source, now, now, now - age, ""))
    c.execute("INSERT OR REPLACE INTO news VALUES (?,?,?,?,?,?,?,?,?)", (source + title, source, title, "https://" + e.FEEDS[source][1] + "/fixture", now - age, now - first_seen_age, json.dumps(assets), bias, urgent))


def test_news_requires_fresh_coverage_and_corroboration():
    now = int(time.time())
    with e.db() as c:
        assert not e.news_context(c, "BTC-USD", now)["available"]
        news_item(c, "CoinDesk", "Bitcoin ETF receives approval", now)
        assert e.news_context(c, "BTC-USD", now)["bias"] == "UNCONFIRMED"
        news_item(c, "Decrypt", "New Bitcoin ETF approved by regulator", now)
        assert e.news_context(c, "BTC-USD", now)["bias"] == "BULLISH"
        assert not e.news_context(c, "BTC-USD", now + 1000)["available"]


def test_identical_syndicated_titles_do_not_corroborate():
    now = int(time.time())
    with e.db() as c:
        for s in ("CoinDesk", "Decrypt"): news_item(c, s, "Bitcoin ETF approved", now)
        assert e.news_context(c, "BTC-USD", now)["bias"] == "UNCONFIRMED"


def test_conflicting_news_and_newly_discovered_event_cooldown():
    now = int(time.time())
    with e.db() as c:
        news_item(c, "CoinDesk", "Bitcoin ETF approved", now)
        news_item(c, "Decrypt", "Bitcoin ETF rejected", now)
        assert e.news_context(c, "BTC-USD", now)["bias"] == "CONFLICT"
        news_item(c, "Cointelegraph", "Bitcoin exchange suffers hack", now, age=3600, first_seen_age=10)
        assert e.news_context(c, "BTC-USD", now)["cooldown"]


def test_speculative_headline_never_directional():
    assert e.classify_headline("Could Bitcoin ETF be approved?")[1] == 0
    assert e.classify_headline("Bitcoin ETF approval denied by officials")[1] == 0


def test_rss_rejects_future_dates_entities_and_unapproved_links():
    now = int(time.time())
    def rss(date, link="https://coindesk.com/fixture"):
        return f'<rss><channel><item><title>Bitcoin report</title><link>{link}</link><pubDate>{formatdate(date,usegmt=True)}</pubDate></item></channel></rss>'.encode()
    assert len(e.parse_feed(rss(now - 10), "CoinDesk", now)) == 1
    for data in (rss(now + 60), rss(now - 10, "https://evil.example/"), b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss/>'):
        with pytest.raises(ValueError): e.parse_feed(data, "CoinDesk", now)


def good_technical(c, pair, now, direction="LONG"):
    return {"direction": direction, "bar": int(now // 300) * 300 - 300, "reasons": [], "five_minute_close": 100, "technical_pass": True, "levels": []}


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_forward_entry_exit_journal_and_idempotent_restart(monkeypatch, direction):
    now = int(time.time()); monkeypatch.setattr(e, "technical_evidence", lambda c,p,t: good_technical(c,p,t,direction))
    with e.db() as c: news_item(c, "CoinDesk", "Bitcoin market update", now)
    assert e.decide("BTC-USD", quote(now=now), now)["status"] == "OPEN_" + direction
    assert e.decide("BTC-USD", quote(now=now), now)["status"] == "WAIT"
    e.init()
    assert e.decide("BTC-USD", quote(now=now), now)["status"] == "WAIT"
    e.mark_positions({"BTC-USD": quote(107 if direction == "LONG" else 93, now + 15)}, now + 15)
    d = e.dashboard()
    assert d["totals"]["completed"] == 1 and d["journal"]["completed"] == 1
    assert d["totals"]["net"] > 0
    assert len(d["trades"]) == 2
    assert d["decisions"][0]["status"] == "OPEN_" + direction


def test_missing_news_or_stale_quote_never_creates_a_fill(monkeypatch):
    now = int(time.time()); monkeypatch.setattr(e, "technical_evidence", good_technical)
    d = e.decide("BTC-USD", quote(now=now), now)
    assert d["status"] == "WAIT" and any("news" in s for s in d["reasons"])
    with e.db() as c: news_item(c, "CoinDesk", "Bitcoin market update", now)
    d = e.decide("BTC-USD", quote(now=now - 100), now)
    assert any("bid/ask" in s for s in d["reasons"])
    assert not e.dashboard()["trades"]


def test_opposing_news_blocks_short(monkeypatch):
    now = int(time.time()); monkeypatch.setattr(e, "technical_evidence", lambda c,p,t: good_technical(c,p,t,"SHORT"))
    with e.db() as c:
        news_item(c,"CoinDesk","Bitcoin ETF approval confirmed",now)
        news_item(c,"Decrypt","Regulator approved new Bitcoin ETF",now)
    assert any("opposes" in r for r in e.decide("BTC-USD",quote(now=now),now)["reasons"])
    assert not e.dashboard()["trades"]


def test_daily_five_ceiling_and_two_open_limit():
    now = int(time.time())
    for i in range(5):
        p = open_one(now=now+i)
        with e.db() as c: e.close_position(c,p,quote(now=now+i),"test",now+i)
    with e.db() as c: assert any("Five" in r for r in e.risk_blocks(c, now+5))
    # Separately verify simultaneous position limit.
    open_one(pair="BTC-USD",now=now+6); open_one(pair="ETH-USD",now=now+6)
    with e.db() as c: assert any("Two-position" in r for r in e.risk_blocks(c,now+6))


def test_no_future_or_gapped_candles_and_no_fake_all_time_level():
    now = 1794000000
    bars = e.normalize_bars([[now+300,99,101,100,100,1],[now-300,99,101,100,100,1],[now-300,99,102,100,101,2],[now-600,99,float('nan'),100,100,1]],300,now)
    assert len(bars) == 1 and bars[0]["close"] == 101
    levels = e.mapping_levels([],[],now)
    assert all(l["high"] is None for l in levels)


@pytest.mark.parametrize("direction,sign", [("LONG",1),("SHORT",-1)])
def test_real_multitimeframe_signal_and_level_confirmation(direction,sign):
    now=int(time.time()); hour=now//3600*3600; minute=now//300*300
    hourly=[{"ts":hour-(26-i)*3600,"low":99.,"high":101.,"open":100.,"close":100.,"volume":100.} for i in range(26)]
    hourly[-1].update(close=100+sign*10,high=111 if sign==1 else 101,low=89 if sign==-1 else 99,volume=300)
    minutes=[{"ts":minute-(26-i)*300,"low":min(100,100+sign*(i+1)*.4)-1,"high":max(100,100+sign*(i+1)*.4)+1,"open":100.,"close":100+sign*(i+1)*.4,"volume":100.} for i in range(26)]
    daily=[{"ts":int(e.day_floor(now))-86400,"low":95.,"high":105.,"open":100.,"close":100.,"volume":1000.}]
    with e.db() as c:
        for interval,bars in [(3600,hourly),(300,minutes),(86400,daily)]: e.store_bars(c,'BTC-USD',interval,bars)
        ev=e.technical_evidence(c,'BTC-USD',now)
        assert ev['direction']==direction
        assert ev['technical_pass'],ev['reasons']
        assert 'Previous day' in ev['level_confirmations']
        c.execute('DELETE FROM bars WHERE pair=? AND interval=300 AND ts=?',('BTC-USD',minutes[-5]['ts']))
        assert 'gaps' in ' '.join(e.technical_evidence(c,'BTC-USD',now)['reasons'])


def test_private_api_auth_and_paper_only_routes():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app),base_url="http://fixture",trust_env=False) as c:
            assert (await c.get('/api/dashboard')).status_code == 401
            c.auth = ('fixture','fixture-password')
            d = (await c.get('/api/dashboard')).json()
            assert d['mode'] == 'PAPER_ONLY' and d['settings']['leverage'] == 2
            for path in ['/api/orders','/api/live','/api/reset','/api/liquidate']:
                assert (await c.post(path,json={})).status_code == 404
            assert (await c.post('/api/control',json={'action':'resume'})).status_code == 400
            assert (await c.post('/api/control',json={'action':'pause','confirmation':'STOP PAPER ENTRIES'})).json()['emergency_paused']
            assert (await c.get('/api/journal')).json()['mode'] == 'PAPER_ONLY'
    asyncio.run(run())
