"""Chief v2: isolated-margin PAPER simulator. No private exchange API or real orders."""
import asyncio
import base64
import hashlib
import html
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
import practice_budget as practice
import futures_runtime as futures

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chief")
VERSION = "2.6.0-paper"
PAIRS = tuple(practice.ROOTS)
DB_PATH = os.getenv("DB_PATH", "/data/chief.sqlite")
START_CASH = float(os.getenv("PAPER_STARTING_CASH", "250"))
PRACTICE_VENUE = os.getenv("PAPER_PRACTICE_VENUE", "SPOT_PRICE_PROXY")
SIGNAL_SOURCE = os.getenv("PAPER_SIGNAL_SOURCE", "SPOT_REFERENCE")
SIZING_MODE = os.getenv("PAPER_SIZING_MODE", "RISK_LIMITED")
if SIGNAL_SOURCE not in ("SPOT_REFERENCE", "CONTRACT") or SIZING_MODE not in ("RISK_LIMITED", "FULL_MARGIN"): raise ValueError("Unsupported paper configuration")
if not math.isfinite(START_CASH) or START_CASH <= 0: raise ValueError("Invalid practice starting cash")
FEE = float(os.getenv("PAPER_FEE_RATE", "0.009"))
# Optional additional stress buffer on top of measured book impact, not Coinbase's slippage fee.
SLIPPAGE = float(os.getenv("PAPER_SLIPPAGE", "0"))
FEE_SOURCE = "Connected Coinbase SPOT Intro snapshot verified 2026-11-06 America/Chicago"
LEVERAGE = min(3., max(1., float(os.getenv("PAPER_LEVERAGE", "2"))))
POLL = max(60, int(os.getenv("POLL_SECONDS", "300")))
MARK_POLL = 5
PAUSED = os.getenv("PAPER_PAUSED", "false").lower() == "true"
RISK = .005
DAILY_LOSS = .02
STOP = .03
MAINTENANCE = .05
HOLDING_RATE = .0001  # assumed cost per 8h, both sides; NOT actual exchange funding
STRATEGIES = ("SNIPER_SCALP", "ORB", "BREAKOUT")
ORB_MAX_HOLD = 30 * 60
SCALP_RISK = .0025
SCALP_MAX_HOLD = 15 * 60
FEEDS = {
    "CoinDesk": ("https://www.coindesk.com/arc/outboundfeeds/rss", "coindesk.com", "crypto"),
    "Cointelegraph": ("https://cointelegraph.com/rss", "cointelegraph.com", "crypto"),
    "Decrypt": ("https://decrypt.co/feed", "decrypt.co", "crypto"),
    "Federal Reserve": ("https://www.federalreserve.gov/feeds/press_all.xml", "federalreserve.gov", "macro"),
    "BBC World": ("https://feeds.bbci.co.uk/news/world/rss.xml", "bbc.co.uk", "world"),
}


def db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c


def get(c, k):
    row = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return row["v"] if row else ""


def put(c, k, v):
    c.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, str(v)))


def add_columns(c, table, columns):
    existing = {r["name"] for r in c.execute("PRAGMA table_info(" + table + ")")}
    for name, definition in columns.items():
        if name not in existing:
            c.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)


def init():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY,v TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS candles(pair TEXT,ts INTEGER,close REAL,volume REAL,PRIMARY KEY(pair,ts))")
        c.execute("CREATE TABLE IF NOT EXISTS positions(pair TEXT PRIMARY KEY,qty REAL,entry REAL,entry_fee REAL,stop REAL,peak REAL,opened INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,pair TEXT,side TEXT,qty REAL,price REAL,fee REAL,reason TEXT,pnl REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS equity(ts INTEGER PRIMARY KEY,value REAL,cash REAL)")
        add_columns(c, "positions", {"direction": "TEXT NOT NULL DEFAULT 'LONG'", "leverage": "REAL NOT NULL DEFAULT 1", "margin": "REAL NOT NULL DEFAULT 0", "target": "REAL", "initial_stop": "REAL", "funding": "REAL NOT NULL DEFAULT 0", "funding_at": "INTEGER", "evidence": "TEXT NOT NULL DEFAULT '{}'", "mark": "REAL", "mark_ts": "INTEGER"})
        add_columns(c, "trades", {"direction": "TEXT NOT NULL DEFAULT 'LONG'", "action": "TEXT", "leverage": "REAL NOT NULL DEFAULT 1", "margin": "REAL NOT NULL DEFAULT 0", "funding": "REAL NOT NULL DEFAULT 0", "evidence": "TEXT NOT NULL DEFAULT '{}'"})
        add_columns(c, "positions", {"strategy": "TEXT NOT NULL DEFAULT 'BREAKOUT'", "expires": "INTEGER", "risk_amount": "REAL NOT NULL DEFAULT 0"})
        add_columns(c, "trades", {"strategy": "TEXT NOT NULL DEFAULT 'BREAKOUT'", "hold_seconds": "INTEGER", "risk_amount": "REAL NOT NULL DEFAULT 0", "r_multiple": "REAL"})
        for table in ("positions", "trades"):
            add_columns(c, table, {"model": "TEXT NOT NULL DEFAULT 'SPOT_PRICE_PROXY'", "product_id": "TEXT", "contracts": "INTEGER", "contract_size": "REAL"})
        add_columns(c, "positions", {"settlement_basis": "REAL", "vm_total": "REAL NOT NULL DEFAULT 0", "settlement_cycle": "TEXT", "margin_rate": "REAL", "exit_pending": "TEXT"})
        c.execute("CREATE TABLE IF NOT EXISTS futures_settlements(id INTEGER PRIMARY KEY AUTOINCREMENT,position_opened INTEGER,pair TEXT,product_id TEXT,cycle TEXT,ts INTEGER,price REAL,amount REAL,kind TEXT,UNIQUE(position_opened,pair,product_id,cycle))")
        # Preserve the old spot account and every fill. Its full purchase cost becomes 1x margin.
        c.execute("UPDATE positions SET margin=qty*entry,initial_stop=stop,target=entry*1.06,funding_at=?,mark=entry,mark_ts=opened WHERE margin=0", (int(time.time()),))
        c.execute("UPDATE trades SET action=CASE WHEN side='BUY' THEN 'OPEN_LONG' ELSE 'CLOSE_LONG' END WHERE action IS NULL")
        c.execute("CREATE TABLE IF NOT EXISTS bars(pair TEXT,interval INTEGER,ts INTEGER,low REAL,high REAL,open REAL,close REAL,volume REAL,PRIMARY KEY(pair,interval,ts))")
        c.execute("CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY,source TEXT,title TEXT,url TEXT,published INTEGER,first_seen INTEGER,assets TEXT,bias INTEGER,urgent INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS news_sources(source TEXT PRIMARY KEY,checked INTEGER,success INTEGER,last_item INTEGER,error TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS decisions(pair TEXT,bar INTEGER,ts INTEGER,status TEXT,direction TEXT,reasons TEXT,evidence TEXT,PRIMARY KEY(pair,bar))")
        c.execute("CREATE TABLE IF NOT EXISTS strategy_decisions(pair TEXT,strategy TEXT,bar INTEGER,ts INTEGER,status TEXT,direction TEXT,reasons TEXT,evidence TEXT,PRIMARY KEY(pair,strategy,bar))")
        c.execute("CREATE TABLE IF NOT EXISTS signal_bars AS SELECT * FROM bars WHERE 0")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS signal_bars_key ON signal_bars(pair,interval,ts)")
        c.execute("INSERT OR IGNORE INTO strategy_decisions SELECT pair,'BREAKOUT',bar,ts,status,direction,reasons,evidence FROM decisions")
        for k, v in (("cash", START_CASH), ("starting_cash", START_CASH), ("day", ""), ("day_start", START_CASH), ("last_scan", "never"), ("last_success", "never"), ("last_error", ""), ("last_mark", "never")):
            c.execute("INSERT OR IGNORE INTO state VALUES (?,?)", (k, str(v)))
        if PRACTICE_VENUE == "COINBASE_US_FUTURES" and get(c, "bar_source") != "FUTURES_CONTRACT":
            if c.execute("SELECT COUNT(*) FROM positions WHERE model!=?", (futures.MODEL,)).fetchone()[0]:
                raise ValueError("Cannot change bar source with existing proxy positions")
            c.execute("DELETE FROM bars")
            put(c, "bar_source", "FUTURES_CONTRACT")
        put(c, "version", VERSION)
    log.info("PAPER RUN %s: starting capital $%.2f; %d configured markets; real orders disabled",VERSION,START_CASH,len(PAIRS))


def iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat()


def direction_sign(direction):
    return 1 if direction == "LONG" else -1


def equity_value(c):
    value = float(get(c, "cash"))
    for row in c.execute("SELECT * FROM positions"):
        p = dict(row)
        if p["model"] == futures.MODEL:
            value += p["margin"] + direction_sign(p["direction"]) * (p["mark"] - p["settlement_basis"]) * p["qty"]
        else:
            value += max(0, p["margin"] + direction_sign(p["direction"]) * (p["mark"] - p["entry"]) * p["qty"] - p["funding"])
    return value


def roll_day(c, now):
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    if get(c, "day") != day:
        put(c, "day", day)
        put(c, "day_start", equity_value(c))
        put(c, "daily_halt", "false")
    if equity_value(c) < float(get(c, "day_start")) * (1 - DAILY_LOSS):
        put(c, "daily_halt", "true")


def day_floor(now):
    return int(now // 86400 * 86400)


def risk_blocks(c, now):
    roll_day(c, now)
    positions = c.execute("SELECT * FROM positions").fetchall()
    opened = c.execute("SELECT COUNT(*) FROM trades WHERE action LIKE 'OPEN_%' AND ts>=?", (day_floor(now),)).fetchone()[0]
    completed = c.execute("SELECT COUNT(*) FROM trades WHERE action LIKE 'CLOSE_%' AND ts>=?", (day_floor(now),)).fetchone()[0]
    reasons = []
    if PAUSED or get(c, "emergency_paused") == "true": reasons.append("New paper entries paused")
    if get(c, "daily_halt") == "true": reasons.append("2% daily drawdown halt latched until next UTC day")
    if opened >= 5 or completed + len(positions) >= 5: reasons.append("Five-trade daily ceiling reached")
    if len(positions) >= (1 if SIZING_MODE == "FULL_MARGIN" else 2): reasons.append("Full-margin paper mode permits one position" if SIZING_MODE == "FULL_MARGIN" else "Two-position limit reached")
    if PRACTICE_VENUE == "COINBASE_US_FUTURES":
        if equity_value(c) <= 0 or float(get(c, "cash")) < 0: reasons.append("Futures margin deficit; no new entries")
        if any(p["model"] == futures.MODEL and p["exit_pending"] for p in positions): reasons.append("Futures protective exit pending; no new entries")
        if any(p["model"] == futures.MODEL and now - (p["mark_ts"] or 0) > 15 for p in positions): reasons.append("Existing futures position marks are stale")
    return reasons


def normalize_bars(data, interval, now):
    unique = {}
    if not isinstance(data, list): raise ValueError("Unexpected Coinbase candle response")
    for row in data:
        if not isinstance(row, list) or len(row) < 6: continue
        ts, low, high, op, close, volume = map(float, row[:6])
        if not all(math.isfinite(x) for x in (ts, low, high, op, close, volume)): continue
        if ts <= 0 or int(ts) % interval or min(low, op, close) <= 0 or high < max(low, op, close) or low > min(op, close) or volume < 0 or ts > now: continue
        unique[int(ts)] = {"ts": int(ts), "low": low, "high": high, "open": op, "close": close, "volume": volume}
    return [unique[k] for k in sorted(unique)]


def active_contract(c, pair, now):
    held = c.execute("SELECT product_id FROM positions WHERE pair=? AND model=?", (pair, futures.MODEL)).fetchone()
    snapshots = json.loads(get(c, "futures_snapshots") or "{}")
    contracts = json.loads(get(c, "futures_contracts") or "{}")
    return futures.fresh_spec(contracts.get(held[0]) if held else snapshots.get(pair), now)


async def coinbase_get(client,url,**kwargs):
    """Pace shared scan traffic; protection uses its own independent client."""
    if isinstance(getattr(client,'_transport',None),httpx.MockTransport):
        return await client.get(url,**kwargs)
    if not hasattr(client,'_chief_request_lock'):
        client._chief_request_lock=asyncio.Lock();client._chief_next_request=0.
    for attempt in range(2):
        async with client._chief_request_lock:
            await asyncio.sleep(max(0,client._chief_next_request-time.monotonic()))
            client._chief_next_request=time.monotonic()+.5
        response=await client.get(url,**kwargs)
        if response.status_code!=429 or attempt:return response
        # Bounded retry, with the actual exchange timestamps checked downstream.
        await asyncio.sleep(2)


async def fetch_bars(client, pair, interval):
    if PRACTICE_VENUE == "COINBASE_US_FUTURES":
        with db() as c: s = active_contract(c, pair, time.time())
        end = int(time.time() // interval) * interval
        count = 120 if interval == 60 else 350
        granularity = {60:"ONE_MINUTE",300:"FIVE_MINUTE",3600:"ONE_HOUR",86400:"ONE_DAY"}[interval]
        r = await coinbase_get(client,"https://api.coinbase.com/api/v3/brokerage/market/products/" + s["product_id"] + "/candles", params={"start":end-count*interval,"end":end,"granularity":granularity,"limit":count}, headers={"Cache-Control":"no-cache"})
        r.raise_for_status()
        raw = [[b["start"],b["low"],b["high"],b["open"],b["close"],b["volume"]] for b in r.json().get("candles",[])]
    else:
        params = {"granularity": interval}
        if interval == 60:
            end = int(time.time() // 60) * 60
            params.update(start=iso(end-120*60),end=iso(end))
        r = await coinbase_get(client,"https://api.exchange.coinbase.com/products/" + pair + "/candles", params=params)
        r.raise_for_status(); raw = r.json()
    bars = normalize_bars(raw, interval, time.time())
    if not bars and PRACTICE_VENUE != "COINBASE_US_FUTURES": raise ValueError("Empty valid candle history")
    return bars


def store_bars(c, pair, interval, bars):
    c.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", [(pair, interval, b["ts"], b["low"], b["high"], b["open"], b["close"], b["volume"]) for b in bars])


class MarketDataUnavailable(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


async def read_book(client, product):
    # A bounded retry avoids reusing an intermediary's cached response. Never
    # replace the exchange timestamp with fetch time or relax the ten-second cap.
    error = None
    for attempt in range(2):
        try:
            r = await coinbase_get(client,"https://api.coinbase.com/api/v3/brokerage/market/product_book", params={"product_id":product,"limit":50,"_cb":str(time.time_ns())}, headers={"Cache-Control":"no-cache, no-store","Pragma":"no-cache"})
            r.raise_for_status()
            return normalize_book(r.json(),product,time.time())
        except ValueError as exc:
            error = MarketDataUnavailable("STALE_BOOK" if "Stale" in str(exc) else "INVALID_BOOK",str(exc))
            if error.code != "STALE_BOOK": raise error
        except httpx.HTTPError as exc:
            error = MarketDataUnavailable("BOOK_TRANSPORT",type(exc).__name__)
        if attempt == 0: await asyncio.sleep(.2)
    raise error


async def fetch_quote(client, pair):
    spec = None
    if PRACTICE_VENUE == "COINBASE_US_FUTURES":
        with db() as c: spec = active_contract(c,pair,time.time())
    product = spec["product_id"] if spec else pair
    q = await read_book(client,product)
    if spec:
        q.update(product_id=product,contract=spec)
        for side in ("bids","asks"):q[side]=[(price,contracts*spec["contract_size"]) for price,contracts in q[side]]
    return q


def signal_table():
    return "signal_bars" if PRACTICE_VENUE == "COINBASE_US_FUTURES" and SIGNAL_SOURCE == "SPOT_REFERENCE" else "bars"


async def fetch_signal_bars(client,pair,interval):
    end=int(time.time()//interval)*interval
    count=160 if interval==60 else 350
    r=await coinbase_get(client,"https://api.coinbase.com/api/v3/brokerage/market/products/"+pair+"/candles",params={"start":end-count*interval,"end":end,"granularity":{60:"ONE_MINUTE",300:"FIVE_MINUTE",3600:"ONE_HOUR",86400:"ONE_DAY"}[interval],"limit":count,"_cb":str(time.time_ns())},headers={"Cache-Control":"no-cache, no-store"})
    r.raise_for_status()
    raw=[[b["start"],b["low"],b["high"],b["open"],b["close"],b["volume"]] for b in r.json().get("candles",[])]
    bars=normalize_bars(raw,interval,time.time())
    if not bars:raise MarketDataUnavailable("SIGNAL_EMPTY","Liquid spot reference history unavailable")
    return bars


def store_signal_bars(c,pair,interval,bars):
    c.executemany("INSERT OR REPLACE INTO signal_bars VALUES (?,?,?,?,?,?,?,?)",[(pair,interval,b["ts"],b["low"],b["high"],b["open"],b["close"],b["volume"]) for b in bars])


def map_signal_evidence(pair,quote,evidence,now):
    ev={**evidence,"product_id":quote.get("product_id") if quote else None,"price_source":"FUTURES_CONTRACT_CANDLES"}
    if SIGNAL_SOURCE != "SPOT_REFERENCE": return ev
    ref=quote.get("signal_quote") if quote else None
    if not ref or ref.get("product_id") != pair or not -5<=now-ref.get("ts",0)<=10 or not quote or not -5<=now-quote.get("ts",0)<=10:
        raise MarketDataUnavailable("BASIS_UNAVAILABLE","Fresh paired spot and futures quotes required for signal mapping")
    ratio=quote["price"]/ref["price"]
    if not math.isfinite(ratio) or abs(ratio-1)>.02:
        raise MarketDataUnavailable("BASIS_OUTLIER","Spot/futures price basis exceeds the 2% sanity bound")
    ev.update(price_source="SPOT_REFERENCE_MAPPED_TO_FUTURES",signal_source="COINBASE_SPOT_REFERENCE",reference_pair=pair,basis={"ratio":ratio,"spot_price":ref["price"],"futures_price":quote["price"],"spot_ts":ref["ts"],"futures_ts":quote["ts"]})
    price_keys=("structural_stop","opposite_range_level","atr_1m","atr_5m","one_minute_close","five_minute_close","retest_level","sweep_reference","orb_high","orb_low")
    ev["reference_prices"]={k:evidence[k] for k in price_keys if isinstance(evidence.get(k),(int,float))}
    for key,value in ev["reference_prices"].items():ev[key]=value*ratio
    if "levels" in evidence:
        ev["reference_levels"]=evidence["levels"]
        ev["levels"]=[{**level,**{k:v*ratio for k,v in level.items() if k in ("open","high","low","midpoint") and isinstance(v,(int,float))}} for level in evidence["levels"]]
    return ev


def input_readiness(c,pair,now):
    parts={}
    for interval,minimum in ((60,40),(300,65),(3600,60)):
        rows=c.execute("SELECT ts FROM "+signal_table()+" WHERE pair=? AND interval=? AND ts+?<=? ORDER BY ts DESC LIMIT ?",(pair,interval,interval,now,minimum)).fetchall()
        times=sorted(r["ts"] for r in rows)
        state="WARMING" if len(times)<minimum else "STALE" if now-(times[-1]+interval)>interval+15 else "GAPPED" if any(b-a!=interval for a,b in zip(times,times[1:])) else "READY"
        parts[str(interval)]={"state":state,"bars":len(times),"last_close":times[-1]+interval if times else None}
    return parts


def save_market_status(pair,status):
    with db() as c:
        data=json.loads(get(c,"market_status") or "{}")
        data[pair]=status
        put(c,"market_status",json.dumps(data))


def normalize_book(data, pair, now):
    book = data.get("pricebook", {})
    if book.get("product_id") != pair: raise ValueError("Order book product mismatch")
    ts = datetime.fromisoformat(book["time"].replace("Z", "+00:00")).timestamp()
    if not -5 <= now - ts <= 10: raise ValueError("Stale Coinbase order book")
    sides = {}
    for side in ("bids", "asks"):
        rows = book.get(side)
        if not isinstance(rows, list) or not rows: raise ValueError("Empty order book side")
        values = [(float(r["price"]), float(r["size"])) for r in rows[:50]]
        if any(not math.isfinite(x) or x <= 0 for row in values for x in row): raise ValueError("Invalid order book level")
        sides[side] = sorted(values, reverse=side == "bids")
    bid, ask = sides["bids"][0][0], sides["asks"][0][0]
    if bid > ask: raise ValueError("Crossed order book")
    return {"price": (bid + ask) / 2, "bid": bid, "ask": ask, "ts": int(ts), **sides, "source": "Coinbase public product book"}


def book_vwap(quote, side, qty=None, notional=None):
    if side not in ("BUY", "SELL") or (qty is None) == (notional is None): raise ValueError("Invalid book execution request")
    remaining = float(qty if qty is not None else notional)
    if not math.isfinite(remaining) or remaining <= 0: raise ValueError("Invalid paper order size")
    total_qty, value = 0., 0.
    levels = quote.get("asks" if side == "BUY" else "bids", [])
    for price, size in levels:
        take = min(size, remaining if qty is not None else remaining / price)
        total_qty += take; value += take * price
        remaining -= take if qty is not None else take * price
        if remaining <= 1e-9: return value / total_qty
    raise ValueError("Insufficient visible Coinbase book depth for the complete paper fill")


def execution_price(quote, side, qty=None, notional=None):
    return book_vwap(quote, side, qty, notional) * (1 + SLIPPAGE if side == "BUY" else 1 - SLIPPAGE)


def impact_rate(quote, side, qty=None, notional=None):
    vwap = book_vwap(quote, side, qty, notional)
    top = quote["ask"] if side == "BUY" else quote["bid"]
    return max(0., vwap / top - 1 if side == "BUY" else 1 - vwap / top)


def classify_headline(title):
    """Explicit title rules, not an LLM, fact check or win-probability model."""
    t = title.lower()
    names = {'BTC':'bitcoin','ETH':'ethereum|ether','SOL':'solana','XRP':'ripple','ADA':'cardano','AVAX':'avalanche','BCH':'bitcoin cash','DOGE':'dogecoin','DOT':'polkadot','HBAR':'hedera','LTC':'litecoin','LINK':'chainlink','SUI':'sui','XLM':'stellar'}
    assets = [pair for pair in PAIRS if re.search(r'\b(?:'+pair.split('-')[0].lower()+'|'+names.get(pair.split('-')[0],pair.split('-')[0].lower())+r')\b',t)]
    macro = bool(re.search(r"\bfederal reserve\b|\bfomc\b|\bfed\b|\binflation\b|\bpayrolls?\b|\binterest rates?\b|\boil\b|\bwar\b|\binvasion\b|\bceasefire\b|\bstrait of hormuz\b", t))
    broad = bool(re.search(r"\bcrypto\b|\bcryptocurrency\b|\bstablecoin\b", t))
    if not assets and (macro or broad): assets = list(PAIRS)
    # Ambiguous/question/speculative headlines never become directional evidence.
    speculative = bool(re.search(r"\?|\bcould\b|\bmight\b|\bmay\b|\brumou?r\b|\bpredict\w*\b|\bif\b|\balleged\b|\bden(?:y|ies|ied)\b|\bnot\b|\bno longer\b|\blifted\b|\breversed\b|\brules? out\b", t))
    positive = bool(re.search(r"\betf\b.*\bapprov\w*|\bapprov\w*.*\betf\b|\brecord inflows\b|\brate cut\b|\bcuts rates\b|\bceasefire agreed\b", t))
    negative = bool(re.search(r"\betf\b.*\breject\w*|\brecord outflows\b|\brate hike\b|\braises rates\b|\btrading ban\b|\bnetwork outage\b", t))
    urgent = bool(re.search(r"\bhack\w*\b|\bexploit\w*\b|\battack\w*\b|\binvasion\b|\bwar\b|\bemergency\b|\btrading halt\b|\bnetwork outage\b|\bfomc\b|\brate (?:cut|hike)\b|\bpayrolls?\b|\binflation\b", t))
    return assets, (0 if speculative or positive == negative else (1 if positive else -1)), urgent


def parse_feed(data, source, now):
    if len(data) > 1_500_000 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper(): raise ValueError("Unsafe or oversized RSS")
    root = ET.fromstring(data)
    nodes = root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")
    if not nodes: raise ValueError("RSS contains no entries")
    result = []
    for item in nodes[:100]:
        def value(*names):
            for name in names:
                node = item.find(name)
                if node is not None: return (node.text or node.attrib.get("href", "")).strip()
            return ""
        title = html.unescape(re.sub(r"<[^>]*>", "", value("title", "{http://www.w3.org/2005/Atom}title")))[:240]
        url = value("link", "{http://www.w3.org/2005/Atom}link")
        date = value("pubDate", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated", "{http://purl.org/dc/elements/1.1/}date")
        try:
            try: dt = parsedate_to_datetime(date)
            except (ValueError, TypeError): dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if dt.tzinfo is None: continue
            published = int(dt.timestamp())
        except (ValueError, TypeError, OverflowError): continue
        host = (urlparse(url).hostname or "").lower()
        allowed = FEEDS[source][1]
        if not title or urlparse(url).scheme != "https" or not (host == allowed or host.endswith("." + allowed) or source == "BBC World" and host.endswith(".bbc.com")): continue
        if published > now or now - published > 3 * 86400: continue
        assets, bias, urgent = classify_headline(title)
        key = hashlib.sha256((source + "|" + url.split("?")[0]).encode()).hexdigest()
        result.append((key, source, title, url, published, int(now), json.dumps(assets), bias, int(urgent)))
    if not result: raise ValueError("RSS has no valid recent timestamped articles")
    return result


async def refresh_news(client):
    async def one(source):
        now = int(time.time())
        try:
            r = await client.get(FEEDS[source][0])
            r.raise_for_status()
            rows = parse_feed(r.content, source, now)
            with db() as c:
                c.executemany("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?,?,?)", rows)
                c.execute("INSERT OR REPLACE INTO news_sources VALUES (?,?,?,?,?)", (source, now, now, max(x[4] for x in rows), ""))
                c.execute("DELETE FROM news WHERE published<?", (now - 7 * 86400,))
        except Exception as exc:
            with db() as c:
                c.execute("INSERT INTO news_sources VALUES (?,?,0,0,?) ON CONFLICT(source) DO UPDATE SET checked=excluded.checked,error=excluded.error", (source, now, type(exc).__name__))
            log.warning("News source unavailable: %s (%s)", source, type(exc).__name__)
    await asyncio.gather(*(one(s) for s in FEEDS))


def news_context(c, pair, now):
    sources = [dict(r) for r in c.execute("SELECT * FROM news_sources ORDER BY source")]
    fresh_crypto = [s for s in sources if FEEDS.get(s["source"], (None, None, None))[2] == "crypto" and 0 <= now - s["success"] <= 900 and 0 <= now - s["last_item"] <= 18 * 3600]
    fresh_sources = {s["source"] for s in sources if now - s["success"] <= 900}
    articles = [dict(r) for r in c.execute("SELECT * FROM news WHERE published>=? AND published<=? AND first_seen<=? ORDER BY published DESC LIMIT 300", (now - 6 * 3600, now, now)) if pair in json.loads(r["assets"]) and r["source"] in fresh_sources]
    # Syndicated identical titles count once even across outlets.
    seen = set()
    unique = []
    for a in articles:
        title_key = re.sub(r"\W+", "", a["title"].lower())
        if title_key in seen: continue
        seen.add(title_key); unique.append(a)
    pos = {a["source"] for a in unique if a["bias"] == 1}
    neg = {a["source"] for a in unique if a["bias"] == -1}
    bias = "CONFLICT" if pos and neg else "BULLISH" if len(pos) >= 2 else "BEARISH" if len(neg) >= 2 else "UNCONFIRMED" if pos or neg else "NEUTRAL"
    cooldown = any(a["urgent"] and (now - a["published"] < 1800 or now - a["first_seen"] < 900) for a in unique)
    return {"available": bool(fresh_crypto), "coverage": f"{len(fresh_crypto)}/3 crypto feeds fresh", "bias": bias, "cooldown": cooldown, "headlines": [{k: a[k] for k in ("id", "source", "title", "url", "published", "first_seen", "bias", "urgent")} for a in unique[:15]], "sources": sources, "method": "Timestamped RSS; explicit headline rules. Two independent directional reports required; no fact-check or prediction claim."}


def mapping_levels(daily, hourly, now):
    dt = datetime.fromtimestamp(now, timezone.utc)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    quarter = month.replace(month=((month.month - 1) // 3) * 3 + 1)
    levels = []
    def window(name, start, end, interval, rows):
        a, b = int(start.timestamp()), int(end.timestamp())
        selected = [r for r in rows if a <= r["ts"] < b]
        required_end = min(b, int(now // interval) * interval)
        expected = set(range(a, required_end, interval))
        present = {r["ts"] for r in selected}
        complete = bool(selected) and a in present and expected.issubset(present)
        levels.append({"name": name, "open": selected[0]["open"] if complete else None, "high": max(r["high"] for r in selected) if complete else None, "low": min(r["low"] for r in selected) if complete else None, "coverage": "Verified developing range" if complete and b > now else "Verified closed range" if complete else "Unavailable: incomplete history", "start": a, "end": b})
    window("Previous day", day - timedelta(days=1), day, 86400, daily)
    window("Daily", day, day + timedelta(days=1), 3600, hourly)
    window("Previous week", week - timedelta(days=7), week, 86400, daily)
    window("Weekly", week, week + timedelta(days=7), 86400, daily)
    window("Monday", week, week + timedelta(days=1), 3600, hourly)
    window("Monthly", month, (month + timedelta(days=32)).replace(day=1), 86400, daily)
    window("Quarterly", quarter, (quarter + timedelta(days=95)).replace(day=1), 86400, daily)
    window("Yearly", day.replace(month=1, day=1), day.replace(year=day.year + 1, month=1, day=1), 86400, daily)
    for name, tz, start_hour, end_hour in (("Asia", "Asia/Tokyo", 9, 18), ("London", "Europe/London", 8, 17), ("New York", "America/New_York", 8, 17)):
        local = dt.astimezone(ZoneInfo(tz))
        start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        if local < start: start -= timedelta(days=1)
        end = start.replace(hour=end_hour)
        window(name + " session", start.astimezone(timezone.utc), end.astimezone(timezone.utc), 3600, hourly)
    levels.append({"name": "All-time", "open": None, "high": None, "low": None, "coverage": "Unavailable: complete listing history not loaded"})
    return levels


def technical_evidence(c, pair, now):
    def bars(interval, closed=True):
        return [dict(r) for r in c.execute("SELECT * FROM "+signal_table()+" WHERE pair=? AND interval=? ORDER BY ts", (pair, interval)) if not closed or r["ts"] + interval <= now]
    h, m = bars(3600), bars(300)
    daily, all_hours = bars(86400, False), bars(3600, False)
    levels = mapping_levels(daily, all_hours, now)
    ev = {"levels": levels, "observed_at": int(now), "direction": None, "technical_pass": False, "reasons": []}
    if len(h) < 26 or len(m) < 25:
        ev["reasons"].append("Insufficient completed 1H / 5M history"); return ev
    h, m = h[-26:], m[-25:]
    if now - (h[-1]["ts"] + 3600) > 3900 or now - (m[-1]["ts"] + 300) > 360:
        ev["reasons"].append("Completed candle data is stale"); return ev
    if any(b["ts"] - a["ts"] != 3600 for a, b in zip(h, h[1:])) or any(b["ts"] - a["ts"] != 300 for a, b in zip(m, m[1:])):
        ev["reasons"].append("Candle history has gaps"); return ev
    price = h[-1]["close"]
    prior = h[-25:-1]
    momentum = price / h[-5]["close"] - 1
    avg_v = sum(r["volume"] for r in prior) / 24
    ratio = h[-1]["volume"] / avg_v if avg_v > 0 else 0
    direction = "LONG" if price > max(r["close"] for r in prior) else "SHORT" if price < min(r["close"] for r in prior) else None
    ema = m[0]["close"]
    for b in m[1:]: ema += (b["close"] - ema) * 2 / 21
    # Completed 15-minute momentum is assembled from three closed 5-minute bars.
    groups = {}
    for b in m: groups.setdefault(b["ts"] // 900 * 900, []).append(b)
    q = [v[-1]["close"] for k, v in sorted(groups.items()) if len(v) == 3 and v[0]["ts"] == k and v[-1]["ts"] == k + 600 and k + 900 <= now]
    fifteen = q[-1] / q[-2] - 1 if len(q) >= 2 else 0
    structural = [l for l in levels if l["name"] in ("Previous day", "Previous week", "Monday") and l.get("high") is not None and l.get("end", now + 1) <= now]
    confirmed = [l["name"] for l in structural if direction == "LONG" and price >= l["high"] or direction == "SHORT" and price <= l["low"]]
    ev.update({"direction": direction, "bar": m[-1]["ts"], "hour_bar": h[-1]["ts"], "momentum_pct": round(momentum * 100, 4), "volume_multiple": round(ratio, 4), "fifteen_minute_momentum_pct": round(fifteen * 100, 4), "five_minute_close": m[-1]["close"], "level_confirmations": confirmed})
    if direction is None: ev["reasons"].append("No 24-hour close breakout or breakdown")
    if direction and direction_sign(direction) * momentum <= .02: ev["reasons"].append("Four-hour directional momentum must exceed 2%")
    if ratio <= 1.5: ev["reasons"].append("Hourly volume must exceed 1.5x prior 24-bar average")
    if direction and (direction_sign(direction) * (m[-1]["close"] - ema) <= 0 or direction_sign(direction) * fifteen <= 0): ev["reasons"].append("5M trend and completed 15M momentum must confirm")
    if not confirmed: ev["reasons"].append("No confirmed break of a verified prior-day/week or Monday high/low")
    ev["technical_pass"] = not ev["reasons"]
    return ev


def ema_value(bars, length):
    value = bars[0]["close"]
    for b in bars[1:]: value += (b["close"] - value) * 2 / (length + 1)
    return value


def atr_value(bars, length=14):
    ranges = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"])) for a, b in zip(bars, bars[1:])]
    return sum(ranges[-length:]) / min(length, len(ranges)) if ranges else 0


def scalp_evidence(c, pair, now):
    """A price sweep/reclaim + break/retest, not an order-book liquidity claim."""
    def load(interval, count):
        rows = c.execute("SELECT * FROM "+signal_table()+" WHERE pair=? AND interval=? AND ts+?<=? ORDER BY ts DESC LIMIT ?", (pair, interval, interval, now, count)).fetchall()
        return [dict(r) for r in reversed(rows)]
    h, m, one = load(3600, 60), load(300, 65), load(60, 40)
    ev = {"strategy": "SNIPER_SCALP", "direction": None, "bar": int(now // 60) * 60 - 60, "observed_at": int(now), "technical_pass": False, "reasons": [], "level_confirmations": []}
    for rows, interval, minimum in ((h, 3600, 55), (m, 300, 30), (one, 60, 30)):
        if len(rows) < minimum:
            ev["reasons"].append("Sniper needs completed 1H / 5M / 1M history"); return ev
        if now - (rows[-1]["ts"] + interval) > interval + 15:
            ev["reasons"].append("Sniper candle data is stale"); return ev
        if any(b["ts"] - a["ts"] != interval for a, b in zip(rows, rows[1:])):
            ev["reasons"].append("Sniper candle history has gaps"); return ev
    ev["bar"] = one[-1]["ts"]
    fast, slow, slope = ema_value(h, 20), ema_value(h, 50), ema_value(h[:-3], 20)
    direction = "LONG" if h[-1]["close"] > fast > slow and fast > slope else "SHORT" if h[-1]["close"] < fast < slow and fast < slope else None
    ev["direction"] = direction
    ev["one_hour_bias"] = direction or "FLAT"
    ev["one_minute_close"] = one[-1]["close"]
    ev["five_minute_close"] = m[-1]["close"]
    average_volume = sum(b["volume"] for b in one[-21:-1]) / 20
    volume_ratio = one[-1]["volume"] / average_volume if average_volume > 0 else 0
    ev["volume_multiple"] = round(volume_ratio, 4)
    ev["atr_1m"], ev["atr_5m"] = atr_value(one), atr_value(m)
    if not direction: ev["reasons"].append("Sniper: 1H trend is not aligned; wait")
    if volume_ratio < 1.2: ev["reasons"].append("Sniper: entry volume below 1.2x recent 1M average")
    if direction:
        sign = direction_sign(direction)
        five_ema = ema_value(m, 20)
        if sign * (m[-1]["close"] - five_ema) <= 0 or sign * (m[-1]["close"] - m[-4]["close"]) <= 0:
            ev["reasons"].append("Sniper: completed 5M trend / 15M momentum disagrees with 1H bias")
        setup = None
        # The sweep must precede both the structure break and the retest. Use only
        # 5M reference bars that had closed BEFORE that sweep, never its own future range.
        for i in range(max(0, len(one) - 7), len(one) - 2):
            sweep = one[i]
            prior = [b for b in m if b["ts"] + 300 <= sweep["ts"]][-12:]
            if len(prior) < 12: continue
            reference = min(b["low"] for b in prior) if sign == 1 else max(b["high"] for b in prior)
            opposite = max(b["high"] for b in prior) if sign == 1 else min(b["low"] for b in prior)
            pierced = sweep["low"] < reference and sweep["close"] > reference if sign == 1 else sweep["high"] > reference and sweep["close"] < reference
            if not pierced: continue
            level = sweep["high"] if sign == 1 else sweep["low"]
            breaks = [b for b in one[i + 1:-1] if sign * (b["close"] - level) > 0]
            if not breaks: continue
            last = one[-1]
            touched = last["low"] <= level + ev["atr_1m"] * .1 if sign == 1 else last["high"] >= level - ev["atr_1m"] * .1
            if not touched or sign * (last["close"] - level) <= 0 or sign * (last["close"] - last["open"]) <= 0: continue
            extreme = min(b["low"] for b in one[i:]) if sign == 1 else max(b["high"] for b in one[i:])
            stop = extreme - sign * ev["atr_1m"] * .2
            setup = {"sweep_at": sweep["ts"], "break_at": breaks[0]["ts"], "retest_at": last["ts"], "sweep_reference": reference, "retest_level": level, "structural_stop": stop, "opposite_range_level": opposite}
        if setup:
            ev.update(setup)
            ev["level_confirmations"] = ["Prior 12-bar 5M range sweep/reclaim", "1M structure break and confirmed retest"]
        else: ev["reasons"].append("Sniper: no completed sweep, reclaim, structure break AND retest sequence")
    ev["technical_pass"] = not ev["reasons"]
    return ev


def orb_evidence(c,pair,now):
    """Default NY cash-open ORB: weekdays 09:30–09:45, entries until 11:30 NY."""
    local=datetime.fromtimestamp(now,ZoneInfo("America/New_York"))
    start=local.replace(hour=9,minute=30,second=0,microsecond=0).timestamp();end=start+900
    ev={"strategy":"ORB","direction":None,"bar":int(now//60)*60-60,"observed_at":int(now),"technical_pass":False,"reasons":[],"level_confirmations":[],"orb_timezone":"America/New_York","orb_start":int(start),"orb_end":int(end)}
    if local.weekday()>=5 or not end<=now<start+7200:
        ev["reasons"].append("ORB waits for weekday New York 09:45–11:30 entry window");return ev
    def load(interval,count):
        return [dict(r) for r in reversed(c.execute("SELECT * FROM "+signal_table()+" WHERE pair=? AND interval=? AND ts+?<=? ORDER BY ts DESC LIMIT ?",(pair,interval,interval,now,count)).fetchall())]
    h,m,one=load(3600,60),load(300,65),load(60,140)
    for rows,interval,minimum in ((h,3600,55),(m,300,30),(one,60,30)):
        if len(rows)<minimum or now-(rows[-1]['ts']+interval)>interval+15 or any(b['ts']-a['ts']!=interval for a,b in zip(rows,rows[1:])):
            ev['reasons'].append('ORB needs fresh contiguous completed 1H / 5M / 1M history');return ev
    opening=[b for b in m if start<=b['ts']<end]
    if [b['ts'] for b in opening]!=list(range(int(start),int(end),300)):
        ev['reasons'].append('ORB opening range incomplete; no invented range');return ev
    high=max(b['high'] for b in opening);low=min(b['low'] for b in opening)
    fast,slow,slope=ema_value(h,20),ema_value(h,50),ema_value(h[:-3],20)
    direction='LONG' if h[-1]['close']>fast>slow and fast>slope else 'SHORT' if h[-1]['close']<fast<slow and fast<slope else None
    ev.update(direction=direction,one_hour_bias=direction or 'FLAT',orb_high=high,orb_low=low,one_minute_close=one[-1]['close'],five_minute_close=m[-1]['close'],atr_1m=atr_value(one),atr_5m=atr_value(m),bar=one[-1]['ts'])
    if not direction:ev['reasons'].append('ORB: 1H trend is not aligned');return ev
    sign=direction_sign(direction);level=high if sign==1 else low;last=one[-1]
    breaks=[b for b in one[:-1] if b['ts']>=end and sign*(b['close']-level)>0]
    touched=last['low']<=level+ev['atr_1m']*.1 if sign==1 else last['high']>=level-ev['atr_1m']*.1
    if not breaks or last['ts']<=breaks[0]['ts'] or not touched or sign*(last['close']-level)<=0 or sign*(last['close']-last['open'])<=0:
        ev['reasons'].append('ORB: completed breakout followed by confirming retest required')
    avg=sum(b['volume'] for b in one[-21:-1])/20
    ev['volume_multiple']=last['volume']/avg if avg>0 else 0
    if ev['volume_multiple']<1.2:ev['reasons'].append('ORB: retest volume below 1.2x recent average')
    if sign*(m[-1]['close']-ema_value(m,20))<=0:ev['reasons'].append('ORB: 5M trend opposes 1H bias')
    ev.update(structural_stop=min(last['low'],level)-ev['atr_1m']*.2 if sign==1 else max(last['high'],level)+ev['atr_1m']*.2,opposite_range_level=level+sign*(high-low),retest_level=level,break_at=breaks[0]['ts'] if breaks else None,retest_at=last['ts'],level_confirmations=['NY 15-minute opening range'] if breaks else [])
    ev['technical_pass']=not ev['reasons'];return ev


def scalp_trade_plan(quote, evidence):
    """Project net reward/risk with the SAME execution math used for the ledger."""
    direction = evidence.get("direction")
    if direction not in ("LONG", "SHORT") or not evidence.get("structural_stop"):
        return None, "Sniper: no confirmed structural invalidation"
    sign = direction_sign(direction)
    probe = quote.get("plan_notional", 250.)
    try:
        entry = execution_price(quote, "BUY" if sign == 1 else "SELL", notional=probe)
        exit_impact = impact_rate(quote, "SELL" if sign == 1 else "BUY", qty=probe / entry)
    except ValueError as exc: return None, "Sniper: " + str(exc)
    stop = evidence["structural_stop"]
    distance = sign * (entry - stop) / entry
    if not .001 <= distance <= .008:
        return None, "Sniper: structural stop must be between 0.1% and 0.8% from entry"
    # Target sits before the opposite observed range, capped by 2x completed 5M ATR.
    # Never push a target farther away solely to manufacture a favorable R multiple.
    room = sign * (evidence["opposite_range_level"] - entry)
    move = min(room * .95, evidence["atr_5m"] * 2)
    if move <= 0: return None, "Sniper: no clear room to the opposite range level"
    target = entry + sign * move
    holding = HOLDING_RATE * SCALP_MAX_HOLD / 28800
    projected_exit_factor = (1 - sign * exit_impact) * (1 - sign * SLIPPAGE)
    stop_ratio = stop * projected_exit_factor / entry
    target_ratio = target * projected_exit_factor / entry
    loss = -(sign * (stop_ratio - 1) - FEE * (1 + stop_ratio) - holding)
    reward = sign * (target_ratio - 1) - FEE * (1 + target_ratio) - holding
    rr = reward / loss if loss > 0 else 0
    plan = {"strategy": "SNIPER_SCALP", "entry_estimate": entry, "stop": stop, "target": target, "risk_fraction": loss, "risk_rate": SCALP_RISK, "reward_fraction": reward, "net_rr": rr, "max_hold_seconds": SCALP_MAX_HOLD, "round_trip_fee_pct": FEE * 200, "additional_stress_per_side_pct": SLIPPAGE * 100, "projected_exit_book_impact_pct": exit_impact * 100, "cost_probe_notional": probe, "book_time": iso(quote["ts"]), "fee_source": FEE_SOURCE, "spread_pct": (quote["ask"] - quote["bid"]) / quote["price"] * 100}
    if reward <= 0 or rr < 2:
        return plan, f"Sniper: net reward/risk {rr:.2f}R is below 2R after modeled fees/spread/slippage; wait"
    return plan, None


def open_position(c, pair, direction, quote, evidence, now):
    if PRACTICE_VENUE == "COINBASE_US_FUTURES":
        return futures.open_position(sys.modules[__name__],c,pair,direction,quote,evidence,now)
    sign = direction_sign(direction)
    eq, cash = equity_value(c), float(get(c, "cash"))
    exposure = sum(p["qty"] * p["mark"] for p in c.execute("SELECT * FROM positions"))
    strategy = evidence.get("strategy", "BREAKOUT")
    plan = evidence.get("plan") if strategy == "SNIPER_SCALP" else None
    if strategy == "SNIPER_SCALP" and (not plan or plan["net_rr"] < 2): return "Sniper plan must pass costs and minimum 2R net reward/risk"
    # Breakout retains its original sizing. Sniper uses its tighter structural stop and net costs.
    try:
        depth_stress = max(impact_rate(quote, "BUY", notional=max(10., eq * .5)), impact_rate(quote, "SELL", notional=max(10., eq * .5)))
    except ValueError as exc: return str(exc)
    risk_per_dollar = plan["risk_fraction"] if plan else STOP + FEE * (2 + STOP) + 2 * (SLIPPAGE + depth_stress) + 3 * HOLDING_RATE
    risk_rate = SCALP_RISK if plan else RISK
    notional = min(eq * risk_rate / risk_per_dollar, eq * .5, max(0, eq * 2 - exposure), cash / (1 / LEVERAGE + FEE))
    if notional < 10: return "Insufficient free margin or exposure allowance"
    try: execution = execution_price(quote, "BUY" if sign == 1 else "SELL", notional=notional)
    except ValueError as exc: return str(exc)
    margin, qty, fee = notional / LEVERAGE, notional / execution, notional * FEE
    stop, target = (plan["stop"], plan["target"]) if plan else (execution * (1 - sign * STOP), execution * (1 + sign * STOP * 2))
    risk_amount = notional * risk_per_dollar
    evidence["strategy"], evidence["planned_risk_amount"] = strategy, risk_amount
    evidence["execution"] = {"model": "COINBASE_BOOK_VWAP", "book_time": iso(quote["ts"]), "fee_rate": FEE, "fee_source": FEE_SOURCE, "additional_stress_rate": SLIPPAGE, "book_impact_rate": impact_rate(quote, "BUY" if sign == 1 else "SELL", qty=qty)}
    payload = json.dumps(evidence, separators=(",", ":"))
    put(c, "cash", cash - margin - fee)
    c.execute("INSERT INTO positions(pair,qty,entry,entry_fee,stop,peak,opened,direction,leverage,margin,target,initial_stop,funding,funding_at,evidence,mark,mark_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (pair, qty, execution, fee, stop, execution, now, direction, LEVERAGE, margin, target, stop, 0, now, payload, quote["price"], quote["ts"]))
    c.execute("UPDATE positions SET strategy=?,expires=?,risk_amount=? WHERE pair=?", (strategy, now + SCALP_MAX_HOLD if plan else None, risk_amount, pair))
    reason = "sniper_sweep_retest" if plan else "news_aware_breakout" if sign == 1 else "news_aware_breakdown"
    c.execute("INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl,direction,action,leverage,margin,funding,evidence,strategy,risk_amount) VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,0,?,?,?)", (now, pair, "BUY" if sign == 1 else "SELL", qty, execution, fee, reason, direction, "OPEN_" + direction, LEVERAGE, margin, payload, strategy, risk_amount))
    put(c, "entry_bar_" + pair + "_" + strategy, evidence["bar"])
    if strategy == "BREAKOUT": put(c, "entry_bar_" + pair, evidence["bar"])
    return None


def close_position(c, p, quote, reason, now):
    if p.get("model") == futures.MODEL:
        return futures.close_position(sys.modules[__name__],c,p,quote,reason,now)
    sign = direction_sign(p["direction"])
    side = "SELL" if sign == 1 else "BUY"
    execution = execution_price(quote, side, qty=p["qty"])
    exit_fee = p["qty"] * execution * FEE
    gross_pnl = sign * (execution - p["entry"]) * p["qty"]
    credit = max(0., p["margin"] + gross_pnl - exit_fee - p["funding"])
    pnl = credit - p["margin"] - p["entry_fee"]
    exit_evidence = json.loads(p["evidence"])
    exit_evidence["exit_execution"] = {"model": "COINBASE_BOOK_VWAP", "book_time": iso(quote["ts"]), "fee_rate": FEE, "fee_source": FEE_SOURCE, "additional_stress_rate": SLIPPAGE, "book_impact_rate": impact_rate(quote, side, qty=p["qty"])}
    p = {**p, "evidence": json.dumps(exit_evidence)}
    put(c, "cash", float(get(c, "cash")) + credit)
    c.execute("DELETE FROM positions WHERE pair=?", (p["pair"],))
    c.execute("INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl,direction,action,leverage,margin,funding,evidence,strategy,hold_seconds,risk_amount,r_multiple) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (now, p["pair"], "SELL" if sign == 1 else "BUY", p["qty"], execution, exit_fee, reason, pnl, p["direction"], "CLOSE_" + p["direction"], p["leverage"], p["margin"], p["funding"], p["evidence"], p["strategy"], max(0, now - p["opened"]), p["risk_amount"], pnl / p["risk_amount"] if p["risk_amount"] > 0 else None))
    put(c, "exit_at_" + p["pair"], now)
    put(c, "exit_strategy_" + p["pair"], p["strategy"])


def mark_positions(quotes, now):
    if PRACTICE_VENUE == "COINBASE_US_FUTURES":
        return futures.mark_positions(sys.modules[__name__],quotes,now)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        # Day boundary starts before these new marks; a gap cannot erase the day's loss baseline.
        roll_day(c, now)
        for row in c.execute("SELECT * FROM positions").fetchall():
            p = dict(row); quote = quotes.get(p["pair"])
            if not quote or not -5 <= now - quote["ts"] <= (10 if p["strategy"] == "SNIPER_SCALP" else 45): continue
            elapsed = max(0, now - (p["funding_at"] or p["opened"]))
            p["funding"] += p["qty"] * p["entry"] * HOLDING_RATE * elapsed / 28800
            p["mark"], p["mark_ts"], p["funding_at"] = quote["price"], quote["ts"], now
            sign = direction_sign(p["direction"])
            # Longs exit at bid, shorts at ask. Never mark a short as a spot asset.
            exit_mark = quote["bid"] if sign == 1 else quote["ask"]
            isolated_equity = p["margin"] + sign * (exit_mark - p["entry"]) * p["qty"] - p["funding"]
            reason = "liquidation" if isolated_equity <= p["qty"] * exit_mark * MAINTENANCE else "stop" if sign * (exit_mark - p["stop"]) <= 0 else "target" if p["target"] and sign * (exit_mark - p["target"]) >= 0 else None
            if not reason and p["expires"] and now >= p["expires"]: reason = "time_stop"
            if reason:
                try: close_position(c, p, quote, reason, now)
                except ValueError:
                    c.execute("UPDATE positions SET funding=?,funding_at=?,mark=?,mark_ts=? WHERE pair=?", (p["funding"], now, p["mark"], p["mark_ts"], p["pair"]))
                    log.warning("Protective exit waiting for sufficient visible book depth: %s", p["pair"])
            else:
                p["peak"] = max(p["peak"], exit_mark) if sign == 1 else min(p["peak"], exit_mark)
                if p["strategy"] == "SNIPER_SCALP":
                    notional = p["qty"] * p["entry"]
                    try:
                        exit_impact = impact_rate(quote, "SELL" if sign == 1 else "BUY", qty=p["qty"])
                        exit_factor = (1 - sign * exit_impact) * (1 - sign * SLIPPAGE)
                        ratio = exit_mark * exit_factor / p["entry"]
                    except ValueError:
                        exit_factor, ratio = None, 0 if sign == 1 else float("inf")
                    entry_cost = p["entry_fee"] / notional
                    net_fraction = sign * (ratio - 1) - entry_cost - FEE * ratio - p["funding"] / notional
                    if net_fraction >= p["risk_amount"] / notional:
                        cost = p["funding"] / notional
                        be_ratio = (1 + entry_cost + cost) / (1 - FEE) if sign == 1 else (1 - entry_cost - cost) / (1 + FEE)
                        breakeven = p["entry"] * be_ratio / exit_factor
                        p["stop"] = max(p["stop"], breakeven) if sign == 1 else min(p["stop"], breakeven)
                elif sign * (p["peak"] / p["entry"] - 1) >= STOP:
                    trail = p["peak"] * (1 - sign * .02)
                    p["stop"] = max(p["stop"], trail) if sign == 1 else min(p["stop"], trail)
                c.execute("UPDATE positions SET stop=?,peak=?,funding=?,funding_at=?,mark=?,mark_ts=? WHERE pair=?", (p["stop"], p["peak"], p["funding"], now, p["mark"], p["mark_ts"], p["pair"]))
        roll_day(c, now)
        c.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?)", (now, equity_value(c), float(get(c, "cash"))))
        if quotes: put(c, "last_mark", iso(now))


def decide(pair, quote, now, strategy="BREAKOUT"):
    if strategy not in STRATEGIES: raise ValueError("Unknown paper strategy")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        ev = scalp_evidence(c, pair, now) if strategy == "SNIPER_SCALP" else orb_evidence(c, pair, now) if strategy == "ORB" else technical_evidence(c, pair, now)
        ev["strategy"] = strategy
        news = news_context(c, pair, now)
        ev["news"] = news
        reasons = list(ev["reasons"]) + risk_blocks(c, now)
        if PRACTICE_VENUE == "COINBASE_US_FUTURES":
            try: ev=map_signal_evidence(pair,quote,ev,now)
            except MarketDataUnavailable as exc: reasons.append(str(exc)); ev["mapping_error"]=exc.code
        direction = ev["direction"]
        bar = ev.get("bar", int(now // 300) * 300 - 300)
        ev["bar"] = bar
        if c.execute("SELECT 1 FROM positions WHERE pair=?", (pair,)).fetchone(): reasons.append("Position already open; monitoring protective exits")
        used = get(c, "entry_bar_" + pair + "_" + strategy) or (get(c, "entry_bar_" + pair) if strategy == "BREAKOUT" else "")
        if used == str(bar): reasons.append("Entry already used this strategy's completed candle")
        cooldown = 600 if strategy == "SNIPER_SCALP" and get(c, "exit_strategy_" + pair) == "SNIPER_SCALP" else 1800
        if now - float(get(c, "exit_at_" + pair) or 0) < cooldown: reasons.append(f"{cooldown // 60}-minute cooldown after prior exit")
        if not quote or not -5 <= now - quote["ts"] <= (10 if strategy in ("SNIPER_SCALP","ORB") else 45): reasons.append("Fresh executable bid/ask unavailable")
        elif (quote["ask"] - quote["bid"]) / quote["price"] > .003: reasons.append("Bid/ask spread exceeds 0.3%; entry blocked")
        elif direction and abs(quote["price"] / ev.get("one_minute_close" if strategy in ("SNIPER_SCALP","ORB") else "five_minute_close", quote["price"]) - 1) > (.0015 if strategy in ("SNIPER_SCALP","ORB") else .01): reasons.append("Price moved beyond confirmation tolerance; do not chase")
        if PRACTICE_VENUE == "COINBASE_US_FUTURES" and quote and direction and not ev.get("mapping_error"):
            plan, plan_error = futures.plan(sys.modules[__name__],c,pair,direction,quote,ev,now)
            ev["plan"] = plan
            if plan_error: reasons.append(plan_error)
        elif strategy == "SNIPER_SCALP" and quote and -5 <= now - quote["ts"] <= 45 and ev.get("structural_stop"):
            quote = {**quote, "plan_notional": max(10., min(equity_value(c) * .5, float(get(c, "cash")) / (1 / LEVERAGE + FEE)))}
            plan, plan_error = scalp_trade_plan(quote, ev)
            ev["plan"] = plan
            if plan_error: reasons.append(plan_error)
        if not news["available"]: reasons.append("No fresh verified news feed coverage; entry blocked")
        if news["cooldown"]: reasons.append("Breaking-event cooldown; wait for post-news confirmation")
        if news["bias"] in ("CONFLICT", "UNCONFIRMED"): reasons.append("Directional news conflicting or not corroborated")
        if direction == "LONG" and news["bias"] == "BEARISH" or direction == "SHORT" and news["bias"] == "BULLISH": reasons.append("Corroborated news opposes technical direction")
        status = "WAIT"
        if not reasons:
            error = open_position(c, pair, direction, quote, ev, now)
            if error: reasons.append(error)
            else: status = "OPEN_" + direction; reasons = ["Sniper: higher-timeframe alignment, price sweep/reclaim, structure break/retest, volume, news, 2R NET cost gate and shared risk limits passed" if strategy == "SNIPER_SCALP" else "ORB: New York 15-minute range, 1H trend, confirmed break/retest, volume, news and net 2R gates passed" if strategy == "ORB" else "1H bias, 15M/5M confirmation, verified structural level, news and margin/risk gates passed"]
        c.execute("INSERT INTO strategy_decisions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(pair,strategy,bar) DO UPDATE SET ts=excluded.ts,status=excluded.status,direction=excluded.direction,reasons=excluded.reasons,evidence=excluded.evidence WHERE strategy_decisions.status='WAIT'", (pair, strategy, bar, now, status, direction, json.dumps(reasons), json.dumps(ev)))
        return {"status": status, "reasons": reasons}


async def refresh_futures_budget(client):
    try:
        with db() as c:
            if time.time()-float(get(c,"futures_specs_checked") or 0)<240:return
        products=[];seen=set();offset=0
        for _ in range(10):
            r=await coinbase_get(client,"https://api.coinbase.com/api/v3/brokerage/market/products",params={"product_type":"FUTURE","limit":100,"offset":offset,"_cb":str(time.time_ns())},headers={"Cache-Control":"no-cache"})
            r.raise_for_status();page=r.json().get("products",[])
            added=[p for p in page if p.get("product_id") not in seen]
            if not added:break
            products.extend(added);seen.update(p.get("product_id") for p in added)
            if len(page)<100:break
            offset+=len(page)
        snapshots,contracts,now={},{},time.time()
        with db() as c:
            held={row["pair"]:row["product_id"] for row in c.execute("SELECT pair,product_id FROM positions WHERE model=?",(futures.MODEL,))}
        sem=asyncio.Semaphore(3)
        async def detail(pair,root):
            candidates=[]
            for p in products:
                if not p.get("product_id","").startswith(root+"-"):continue
                d=p.get("future_product_details",{})
                if d.get("funding_interval") or d.get("funding_rate") not in (None,"") or d.get("contract_expiry_type")=="PERPETUAL":continue
                try:
                    expiry=datetime.fromisoformat(d["contract_expiry"].replace("Z","+00:00")).timestamp()
                    if expiry>now+86400:candidates.append((expiry,p["product_id"]))
                except (KeyError,ValueError,TypeError):continue
            product=held.get(pair) or (min(candidates)[1] if candidates else None)
            if not product:return
            try:
                async with sem:
                    r=await coinbase_get(client,"https://api.coinbase.com/api/v3/brokerage/market/products/"+product,params={"_cb":str(time.time_ns())},headers={"Cache-Control":"no-cache"})
                    r.raise_for_status();snap=practice.contract_snapshot(r.json(),time.time(),held=pair in held)
                    snapshots[pair]=snap;contracts[product]=snap
            except (httpx.HTTPError,KeyError,TypeError,ValueError):log.warning("Futures specifications unavailable: %s",pair)
        await asyncio.gather(*(detail(pair,root) for pair,root in practice.ROOTS.items()))
        with db() as c:
            for pair,snap in snapshots.items():
                if get(c,"bar_product_"+pair)!=snap["product_id"]:
                    c.execute("DELETE FROM bars WHERE pair=?",(pair,));put(c,"bar_product_"+pair,snap["product_id"])
            put(c,"futures_snapshots",json.dumps(snapshots));put(c,"futures_contracts",json.dumps(contracts));put(c,"futures_specs_checked",time.time())
    except Exception:log.warning("Futures specifications unavailable; stale inputs cannot produce paper fills")


async def scan_once(client,slow_due=True):
    errors=[]
    if slow_due:await refresh_news(client)
    if PRACTICE_VENUE=="COINBASE_US_FUTURES":await refresh_futures_budget(client)
    semaphore=asyncio.Semaphore(3)
    async def scan_market(pair):
        checked=int(time.time());status={"checked_at":checked,"signal_source":"SPOT_REFERENCE" if signal_table()=="signal_bars" else "CONTRACT","state":"UNAVAILABLE","errors":[],"decisions":[]}
        try:
            if signal_table()=="signal_bars":
                # Refresh a reference interval when its next candle has closed.
                # This also repairs a failed warmup on the next minute, rather
                # than leaving 5M data stale between slower scans.
                for interval in (3600,300,86400,60):
                    with db() as c:
                        last=c.execute("SELECT MAX(ts) FROM signal_bars WHERE pair=? AND interval=?",(pair,interval)).fetchone()[0]
                    if interval!=60 and last is not None and last>=int(time.time()//interval)*interval-interval:continue
                    rows=await fetch_signal_bars(client,pair,interval)
                    with db() as c:store_signal_bars(c,pair,interval,rows)
                intervals=(3600,300,60) if slow_due else (60,)
            else:intervals=(300,3600,86400,60) if slow_due else (60,)
            # Contract candles are historical chart evidence, never gap-filled.
            for interval in intervals:
                try:
                    rows=await fetch_bars(client,pair,interval)
                    with db() as c:store_bars(c,pair,interval,rows)
                except Exception as exc:
                    if signal_table()=="bars":raise
                    status["chart_warning"]=type(exc).__name__
            quote=None
            try:
                if signal_table()=="signal_bars":
                    quote,ref=await asyncio.gather(fetch_quote(client,pair),read_book(client,pair))
                    ref["product_id"]=pair;quote["signal_quote"]=ref
                    map_signal_evidence(pair,quote,{},int(time.time()))
                else:quote=await fetch_quote(client,pair)
                mark_positions({pair:quote},int(time.time()))
                status.update(product_id=quote.get("product_id",pair),bid=quote["bid"],ask=quote["ask"],mid=quote["price"],book_at=quote["ts"],book_age_seconds=round(time.time()-quote["ts"],2))
            except (ValueError,httpx.HTTPError) as exc:
                code=getattr(exc,"code","QUOTE_UNAVAILABLE")
                status["errors"].append(code+": "+str(exc));quote=None
            now=int(time.time())
            with db() as c:readiness=input_readiness(c,pair,now)
            status["signal_intervals"]=readiness
            status["state"]="READY" if quote and all(v["state"]=="READY" for v in readiness.values()) else "DATA_BLOCKED"
            if quote and quote.get("contract",{}).get("session_open") is False:status["state"]="SESSION_CLOSED"
            for strategy in STRATEGIES if slow_due else ("SNIPER_SCALP","ORB"):
                decision=decide(pair,quote,now,strategy)
                status["decisions"].append({"strategy":strategy,**decision})
                log.info("PAPER %s %s %s: %s",strategy,pair,decision["status"],"; ".join(decision["reasons"]))
        except Exception as exc:
            message=getattr(exc,"code",type(exc).__name__)+": "+str(exc)[:180]
            status["errors"].append(message);errors.append(pair+" "+message)
            log.warning("Market inputs unavailable: %s: %s",pair,message)
            with db() as c:
                now=int(time.time())
                for strategy in STRATEGIES if slow_due else ("SNIPER_SCALP","ORB"):
                    step=60 if strategy in ("SNIPER_SCALP","ORB") else 300
                    c.execute("INSERT INTO strategy_decisions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(pair,strategy,bar) DO UPDATE SET ts=excluded.ts,status=excluded.status,direction=excluded.direction,reasons=excluded.reasons,evidence=excluded.evidence WHERE strategy_decisions.status='WAIT'",(pair,strategy,now//step*step-step,now,"WAIT",None,json.dumps(["Market inputs unavailable: "+message]),json.dumps({"input_state":"UNAVAILABLE"})))
        status["checked_at"]=int(time.time());save_market_status(pair,status)
    async def bounded(pair):
        async with semaphore:
            await scan_market(pair)
    await asyncio.gather(*(bounded(pair) for pair in PAIRS))
    with db() as c:
        now=int(time.time());put(c,"last_scan",iso(now));put(c,"scan_version",VERSION);put(c,"last_error","; ".join(errors));put(c,"scalp_last_scan",iso(now))
        if not errors:put(c,"last_success",iso(now));put(c,"scalp_last_success",iso(now))
        c.execute("DELETE FROM strategy_decisions WHERE status='WAIT' AND ts<? AND strategy='BREAKOUT'",(now-30*86400,))
        c.execute("DELETE FROM strategy_decisions WHERE status='WAIT' AND ts<? AND strategy IN ('SNIPER_SCALP','ORB')",(now-7*86400,))
        c.execute("DELETE FROM equity WHERE ts<?",(now-90*86400,))
        for table in ("bars","signal_bars"):
            c.execute("DELETE FROM "+table+" WHERE ts<? AND interval IN (300,3600)",(now-35*86400,))
            c.execute("DELETE FROM "+table+" WHERE ts<? AND interval=60",(now-2*86400,))


async def scanner():
    await asyncio.sleep(2)
    async with httpx.AsyncClient(headers={"User-Agent":"ChiefPaperResearch/2.6"},timeout=12,follow_redirects=False) as client:
        next_slow=0
        while True:
            slow_due=time.time()>=next_slow
            try:await scan_once(client,slow_due)
            except Exception:log.exception("Scanner iteration failed; retrying next minute")
            if slow_due:next_slow=time.time()+POLL
            await asyncio.sleep(max(1,int(time.time()//60)*60+62-time.time()))


async def protective_monitor():
    async with httpx.AsyncClient(headers={"User-Agent":"ChiefPaperProtection/2.6"},timeout=8,follow_redirects=False) as client:
        while True:
            try:
                with db() as c:pairs=[r[0] for r in c.execute("SELECT pair FROM positions")]
                quotes={}
                for pair in pairs:
                    try:quotes[pair]=await fetch_quote(client,pair)
                    except Exception as exc:log.warning("Protective quote unavailable: %s: %s; no invented fill",pair,str(exc)[:120])
                if quotes:mark_positions(quotes,int(time.time()))
                with db() as c:put(c,"protection_heartbeat",iso());put(c,"protection_error","")
            except Exception as exc:
                log.exception("Protection iteration failed; retrying")
                try:
                    with db() as c:put(c,"protection_error",type(exc).__name__)
                except Exception:pass
            await asyncio.sleep(MARK_POLL)


@asynccontextmanager
async def lifespan(app):
    init()
    tasks = [asyncio.create_task(scanner()), asyncio.create_task(protective_monitor())]
    try: yield
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Chief | Long/Short PAPER Only", lifespan=lifespan)


@app.middleware("http")
async def auth(req: Request, call_next):
    if req.url.path == "/health": return await call_next(req)
    user, password = os.getenv("DASHBOARD_USER", ""), os.getenv("DASHBOARD_PASSWORD", "")
    if not user or not password: return Response("Dashboard credentials must be set", status_code=503)
    valid = False
    try:
        scheme, value = req.headers.get("authorization", "").split(" ", 1)
        name, pw = base64.b64decode(value, validate=True).decode().split(":", 1)
        valid = scheme.lower() == "basic" and secrets.compare_digest(name, user) and secrets.compare_digest(pw, password)
    except (ValueError, UnicodeDecodeError): pass
    if not valid: return Response("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Chief Paper Trading"'})
    response = await call_next(req)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    with db() as c:
        scan,err=get(c,"last_scan"),get(c,"last_error")
        try:loop_ok=time.time()-datetime.fromisoformat(scan).timestamp()<180 and get(c,"scan_version")==VERSION
        except (ValueError,TypeError):loop_ok=False
        markets=json.loads(get(c,"market_status") or "{}")
        inputs={pair:{"state":markets.get(pair,{}).get("state","WARMING"),"checked_at":markets.get(pair,{}).get("checked_at"),"errors":markets.get(pair,{}).get("errors",[])} for pair in PAIRS}
        ready=sum(v["state"]=="READY" and time.time()-(v["checked_at"] or 0)<180 for v in inputs.values())
        closed=sum(v["state"]=="SESSION_CLOSED" and time.time()-(v["checked_at"] or 0)<180 for v in inputs.values())
        n=news_context(c,PAIRS[0],int(time.time()))
        try:protection_ok=time.time()-datetime.fromisoformat(get(c,"protection_heartbeat")).timestamp()<45
        except (ValueError,TypeError):protection_ok=False
    ok=loop_ok and protection_ok and (ready>0 or closed==len(PAIRS))
    return JSONResponse({"status":"ok" if ok else "degraded","version":VERSION,"mode":"PAPER_ONLY","live_trading":False,"practice_venue":PRACTICE_VENUE,"practice_starting_cash":START_CASH,"sizing_mode":SIZING_MODE,"signal_source":SIGNAL_SOURCE,"futures_execution_ready":PRACTICE_VENUE=="COINBASE_US_FUTURES","last_scan":scan,"scan_loop_ok":loop_ok,"protection_loop_ok":protection_ok,"strategies":STRATEGIES,"capabilities":["LONG","SHORT","CROSS_MARGIN_PAPER","NEWS_GATES","SNIPER_SCALP","ORB"],"markets_ready":ready,"markets_closed":closed,"market_inputs":inputs,"error":err,"news_ready":n["available"],"fee_rate":practice.TAKER_RATE if PRACTICE_VENUE=="COINBASE_US_FUTURES" else FEE,"slippage_model":futures.MODEL if PRACTICE_VENUE=="COINBASE_US_FUTURES" else "COINBASE_BOOK_VWAP","additional_stress_rate":SLIPPAGE},status_code=200 if ok else 503)


def parsed_decision(row):
    return {**dict(row), "reasons": json.loads(row["reasons"]), "evidence": json.loads(row["evidence"])}


@app.get("/api/dashboard")
def dashboard():
    now = int(time.time())
    with db() as c:
        roll_day(c, now)
        positions = [dict(p) for p in c.execute("SELECT * FROM positions")]
        for p in positions:
            p["unrealized_pnl"] = direction_sign(p["direction"]) * (p["mark"] - p["entry"]) * p["qty"] - p["entry_fee"] - p["funding"]
            p["liquidation_estimate"] = None if p["model"] == futures.MODEL else p["entry"] * (1 + direction_sign(p["direction"]) * (MAINTENANCE - 1 / p["leverage"]))
            p["unsettled_pnl"] = direction_sign(p["direction"]) * (p["mark"] - (p["settlement_basis"] or p["entry"])) * p["qty"]
            p["evidence"] = json.loads(p["evidence"])
        trades = [dict(t) for t in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 100")]
        for t in trades: t["evidence"] = json.loads(t["evidence"])
        decisions = [parsed_decision(r) for r in c.execute("SELECT d.* FROM strategy_decisions d JOIN (SELECT pair,strategy,MAX(bar) bar FROM strategy_decisions GROUP BY pair,strategy) x USING(pair,strategy,bar) ORDER BY strategy,pair")]
        j = c.execute("SELECT COUNT(CASE WHEN action LIKE 'CLOSE_%' THEN 1 END) completed,COUNT(CASE WHEN action LIKE 'OPEN_%' THEN 1 END) opened,COALESCE(SUM(pnl),0) net,COALESCE(SUM(fee),0) fees,COALESCE(SUM(funding),0) funding FROM trades WHERE ts>=?", (day_floor(now),)).fetchone()
        totals = dict(c.execute("SELECT COUNT(CASE WHEN action LIKE 'CLOSE_%' THEN 1 END) completed,COALESCE(SUM(pnl),0) net,COALESCE(SUM(fee),0) fees,COALESCE(SUM(funding),0) funding FROM trades").fetchone())
        strategy_results = []
        for strategy in STRATEGIES:
            stats = dict(c.execute("SELECT COUNT(CASE WHEN action LIKE 'OPEN_%' THEN 1 END) entries,COUNT(CASE WHEN action LIKE 'CLOSE_%' THEN 1 END) completed,COUNT(CASE WHEN pnl>0 THEN 1 END) wins,COALESCE(SUM(pnl),0) net,COALESCE(SUM(fee),0) fees,AVG(CASE WHEN action LIKE 'CLOSE_%' THEN hold_seconds END) avg_hold_seconds,AVG(r_multiple) avg_net_r FROM trades WHERE strategy=?", (strategy,)).fetchone())
            stats["strategy"] = strategy
            stats["win_rate"] = stats["wins"] / stats["completed"] * 100 if stats["completed"] else None
            strategy_results.append(stats)
        curve = [dict(e) for e in c.execute("SELECT * FROM equity ORDER BY ts DESC LIMIT 200")][::-1]
        result = {"version": VERSION, "mode": "PAPER_ONLY", "account_profile": {"starting_cash": float(get(c, "starting_cash") or START_CASH), "target_balance_range": [250, 280], "practice_venue": PRACTICE_VENUE, "legacy_history_preserved": PRACTICE_VENUE == "COINBASE_US_FUTURES", "practice_run": "250-dollar forward practice", "sizing_mode": SIZING_MODE, "signal_source": SIGNAL_SOURCE}, "futures_budget": practice.report(json.loads(get(c, "futures_snapshots") or "{}"), max(.01, equity_value(c)), now) if PRACTICE_VENUE == "COINBASE_US_FUTURES" else None, "markets": PAIRS, "cash": round(float(get(c, "cash")), 6), "equity": round(equity_value(c), 6), "positions": positions, "trades": trades, "equity_history": curve, "last_scan": get(c, "last_scan"), "last_success": get(c, "last_success"), "last_mark": get(c, "last_mark"), "last_error": get(c, "last_error"), "entries_paused": bool(risk_blocks(c, now)), "pause_reasons": risk_blocks(c, now), "emergency_paused": get(c, "emergency_paused") == "true", "controls_supported": True, "fee_rate": FEE, "slippage_rate": SLIPPAGE, "slippage_model": "COINBASE_BOOK_VWAP", "fee_profile": {"market": "SPOT_PRICE_PROXY", "tier": "Intro", "order_type": "TAKER", "maker_rate": .005, "taker_rate": FEE, "source": FEE_SOURCE, "futures_reference": {"maker_rate": .00095, "taker_rate": .001, "additional_contract_charges": True, "applied": False}}, "journal": {"day": get(c, "day"), **dict(j), "limit": 5, "reserved": len(positions)}, "totals": totals, "strategy_results": strategy_results, "decisions": decisions, "news": {p: news_context(c, p, now) for p in PAIRS}, "settings": {"leverage": LEVERAGE, "max_leverage": 3, "risk_per_trade_pct": RISK * 100, "daily_loss_pct": DAILY_LOSS * 100, "stop_pct": STOP * 100, "target_r": 2, "mark_poll_seconds": MARK_POLL, "holding_cost_per_8h": HOLDING_RATE, "maintenance_margin_rate": MAINTENANCE, "max_positions": 2, "scalp_risk_pct": SCALP_RISK * 100, "scalp_max_hold_seconds": SCALP_MAX_HOLD, "scalp_min_net_rr": 2, "scalp_scan_seconds": 60}, "disclosure": "Synthetic isolated-margin simulation on Coinbase SPOT prices, not a Coinbase derivatives account. Entry/exit fills use size-weighted Coinbase spot book depth and the verified-account taker-fee snapshot. Optional additional stress is separate. Snapshot depth is not a guaranteed future fill; unseen market changes can add slippage. Assumed holding cost on both sides, not actual funding. Stops/targets sampled about every 5s; fast crossings can be missed and outages delay exits. Liquidation is an assumed model, not exchange rules. Sniper is a fixed-rule paper strategy, not a trained predictive model. No historical news backtest or profitability claim."}

        if PRACTICE_VENUE == "COINBASE_US_FUTURES":
            result.update(fee_rate=practice.TAKER_RATE, slippage_model=futures.MODEL, disclosure=futures.DISCLOSURE)
            result["fee_profile"]={"market":"COINBASE_US_FUTURES","tier":"Intro","order_type":"TAKER","maker_rate":.00095,"taker_rate":practice.TAKER_RATE,"source":practice.FEE_SOURCE,"futures_reference":{"maker_rate":.00095,"taker_rate":practice.TAKER_RATE,"additional_contract_charges":True,"applied":True}}
            result["settings"].update(holding_cost_per_8h=0,maintenance_margin_rate=None,leverage=None,max_leverage=None,margin_model="HIGHER_PUBLISHED_INITIAL_RATE",settlement_model="PAPER_DAILY_VARIATION_ESTIMATE")
            result["settlements"]=[dict(row) for row in c.execute("SELECT * FROM futures_settlements ORDER BY id DESC LIMIT 50")]
            result["margin_guard"]={"required":sum(p["margin"]+futures.fee(p["contracts"],p["contract_size"],p["mark"]) for p in positions if p["model"]==futures.MODEL),"equity":equity_value(c),"policy":"Account-wide conservative initial-margin proxy; not actual liquidation margin"}
        result["market_status"]=json.loads(get(c,"market_status") or "{}")
        result["activity"]={"state":"IN_TRADE" if positions else "PAUSED" if result["entries_paused"] else "SCANNING", "open_positions":len(positions), "latest_fill_at":trades[0]["ts"] if trades else None}
        result["settings"].update(sizing_mode=SIZING_MODE,signal_source=SIGNAL_SOURCE,risk_limit_enforced=SIZING_MODE!="FULL_MARGIN",max_positions=1 if SIZING_MODE=="FULL_MARGIN" else 2)
        return result


@app.get("/api/contract-candles")
def contract_candles(pair: str = "BTC-USD", interval: int = 300):
    if PRACTICE_VENUE != "COINBASE_US_FUTURES": return JSONResponse({"error":"Futures venue not selected"},status_code=409)
    if pair not in PAIRS or interval not in (60,300,3600): return JSONResponse({"error":"Invalid futures candle request"},status_code=400)
    now=int(time.time())
    with db() as c:
        try:s=active_contract(c,pair,now)
        except ValueError:return JSONResponse({"error":"Futures contract metadata unavailable"},status_code=503)
        if get(c,"bar_product_"+pair)!=s["product_id"]:return JSONResponse({"error":"Contract candle history not ready"},status_code=503)
        rows=[dict(row) for row in c.execute("SELECT * FROM bars WHERE pair=? AND interval=? AND ts<=? ORDER BY ts DESC LIMIT 350",(pair,interval,now))][::-1]
    bars=[[b["ts"],b["low"],b["high"],b["open"],b["close"],b["volume"]] for b in rows]
    return {"mode":"PAPER_ONLY","product":pair,"product_id":s["product_id"],"interval":interval,"candles":bars,"source":"FUTURES_CONTRACT_CANDLES","last_candle_at":iso(rows[-1]["ts"]) if rows else None,"stale":not rows or now-(rows[-1]["ts"]+interval)>interval+15}


@app.get("/api/journal")
def journal(before: int = 9223372036854775807):
    with db() as c:
        trades = [dict(r) for r in c.execute("SELECT * FROM trades WHERE id<? ORDER BY id DESC LIMIT 100", (before,))]
        for trade in trades: trade["evidence"] = json.loads(trade["evidence"])
        return {"mode": "PAPER_ONLY", "trades": trades, "next_before": trades[-1]["id"] if len(trades) == 100 else None, "decisions": [parsed_decision(r) for r in c.execute("SELECT * FROM strategy_decisions ORDER BY ts DESC LIMIT 100")]}


@app.post("/api/control")
async def control(req: Request):
    raw = await req.body()
    if len(raw) > 200: return JSONResponse({"error": "Invalid control"}, status_code=400)
    try: d = json.loads(raw)
    except (ValueError, UnicodeDecodeError): return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    action = d.get("action") if isinstance(d, dict) else None
    if action not in ("pause", "resume") or d.get("confirmation") != ("STOP PAPER ENTRIES" if action == "pause" else "RESUME PAPER ENTRIES"):
        return JSONResponse({"error": "Explicit paper confirmation required"}, status_code=400)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        put(c, "emergency_paused", "true" if action == "pause" else "false")
        put(c, "last_control", iso())
    return {"mode": "PAPER_ONLY", "emergency_paused": action == "pause", "protective_exits_enabled": True}


HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Chief Paper Gym</title><style>body{background:#0b1510;color:#e7efe9;font:16px system-ui;max-width:1100px;margin:auto;padding:24px}section{border:1px solid #405446;border-radius:12px;padding:18px;margin:16px 0;overflow:auto}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:10px;border-bottom:1px solid #405446;white-space:nowrap}small{color:#b5c5ba}a{color:#e3bd65}</style></head><body><h1>Chief Paper Gym · LONG / SHORT</h1><p>Paper simulation only. No real orders. Current venue and margin policy appear below.</p><p id="status">Loading authenticated engine…</p><section id="balance"></section><section><h2>Open positions</h2><table id="positions"></table></section><section><h2>Why Chief is waiting</h2><div id="decisions"></div></section><section><h2>Recorded paper fills</h2><table id="trades"></table></section><p id="disclosure"></p><script>
function table(id,headers,rows){let t=document.getElementById(id);t.replaceChildren();let h=t.createTHead().insertRow();for(let s of headers){let c=document.createElement('th');c.textContent=s;h.append(c)}let b=t.createTBody();for(let r of rows){let tr=b.insertRow();for(let v of r)tr.insertCell().textContent=String(v)}if(!rows.length)b.insertRow().insertCell().textContent='No records yet'}
async function refresh(){try{let r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw Error('Connect with your engine dashboard login');let d=await r.json();document.getElementById('status').textContent=d.version+' · '+(d.futures_budget?'Dated U.S. futures paper':d.settings.leverage+'x simulated leverage')+' · last scan '+d.last_scan;document.getElementById('balance').textContent='Equity $'+d.equity.toFixed(2)+' · Free margin $'+d.cash.toFixed(2)+' · Closed trades '+d.totals.completed+' · Net $'+d.totals.net.toFixed(2);document.getElementById('disclosure').textContent=d.disclosure;table('positions',['Strategy','Pair','Direction','Leverage','Entry','Mark','Stop','Target','Margin','Net P&L'],d.positions.map(p=>[p.strategy,p.pair,p.direction,p.leverage+'x',p.entry,p.mark,p.stop,p.target,p.margin,p.unrealized_pnl]));table('trades',['UTC','Strategy','Pair','Action','Leverage','Price','Fee','Holding cost','Net P&L','Reason'],d.trades.map(t=>[new Date(t.ts*1000).toISOString(),t.strategy,t.pair,t.action,t.leverage+'x',t.price,t.fee,t.funding,t.pnl===null?'—':t.pnl,t.reason]));let el=document.getElementById('decisions');el.replaceChildren();for(let v of d.decisions){let p=document.createElement('p');p.textContent=v.strategy+' · '+v.pair+' · '+v.status+' · '+v.reasons.join('; ');el.append(p)}}catch(e){document.getElementById('status').textContent=e.message}}refresh();setInterval(refresh,15000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)

