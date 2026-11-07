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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chief")
VERSION = "2.0.0-paper"
PAIRS = ("BTC-USD", "ETH-USD", "SOL-USD")
DB_PATH = os.getenv("DB_PATH", "/data/chief.sqlite")
START_CASH = float(os.getenv("PAPER_STARTING_CASH", "500"))
FEE = float(os.getenv("PAPER_FEE_RATE", "0.006"))
SLIPPAGE = float(os.getenv("PAPER_SLIPPAGE", "0.001"))
LEVERAGE = min(3., max(1., float(os.getenv("PAPER_LEVERAGE", "2"))))
POLL = max(60, int(os.getenv("POLL_SECONDS", "300")))
MARK_POLL = 15
PAUSED = os.getenv("PAPER_PAUSED", "false").lower() == "true"
RISK = .005
DAILY_LOSS = .02
STOP = .03
MAINTENANCE = .05
HOLDING_RATE = .0001  # assumed cost per 8h, both sides; NOT actual exchange funding
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
        # Preserve the old spot account and every fill. Its full purchase cost becomes 1x margin.
        c.execute("UPDATE positions SET margin=qty*entry,initial_stop=stop,target=entry*1.06,funding_at=?,mark=entry,mark_ts=opened WHERE margin=0", (int(time.time()),))
        c.execute("UPDATE trades SET action=CASE WHEN side='BUY' THEN 'OPEN_LONG' ELSE 'CLOSE_LONG' END WHERE action IS NULL")
        c.execute("CREATE TABLE IF NOT EXISTS bars(pair TEXT,interval INTEGER,ts INTEGER,low REAL,high REAL,open REAL,close REAL,volume REAL,PRIMARY KEY(pair,interval,ts))")
        c.execute("CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY,source TEXT,title TEXT,url TEXT,published INTEGER,first_seen INTEGER,assets TEXT,bias INTEGER,urgent INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS news_sources(source TEXT PRIMARY KEY,checked INTEGER,success INTEGER,last_item INTEGER,error TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS decisions(pair TEXT,bar INTEGER,ts INTEGER,status TEXT,direction TEXT,reasons TEXT,evidence TEXT,PRIMARY KEY(pair,bar))")
        for k, v in (("cash", START_CASH), ("day", ""), ("day_start", START_CASH), ("last_scan", "never"), ("last_success", "never"), ("last_error", ""), ("last_mark", "never")):
            c.execute("INSERT OR IGNORE INTO state VALUES (?,?)", (k, str(v)))
        put(c, "version", VERSION)


def iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat()


def direction_sign(direction):
    return 1 if direction == "LONG" else -1


def equity_value(c):
    positions = c.execute("SELECT * FROM positions").fetchall()
    return float(get(c, "cash")) + sum(max(0, p["margin"] + direction_sign(p["direction"]) * (p["mark"] - p["entry"]) * p["qty"] - p["funding"]) for p in positions)


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
    if len(positions) >= 2: reasons.append("Two-position limit reached")
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


async def fetch_bars(client, pair, interval):
    r = await client.get("https://api.exchange.coinbase.com/products/" + pair + "/candles", params={"granularity": interval})
    r.raise_for_status()
    bars = normalize_bars(r.json(), interval, time.time())
    if not bars: raise ValueError("Empty valid candle history")
    return bars


def store_bars(c, pair, interval, bars):
    c.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", [(pair, interval, b["ts"], b["low"], b["high"], b["open"], b["close"], b["volume"]) for b in bars])


async def fetch_quote(client, pair):
    r = await client.get("https://api.exchange.coinbase.com/products/" + pair + "/ticker")
    r.raise_for_status()
    d = r.json()
    price, bid, ask = [float(d[k]) for k in ("price", "bid", "ask")]
    ts = datetime.fromisoformat(d["time"].replace("Z", "+00:00")).timestamp()
    if not all(math.isfinite(x) and x > 0 for x in (price, bid, ask)) or bid > ask or not -5 <= time.time() - ts <= 45:
        raise ValueError("Stale or invalid Coinbase quote")
    return {"price": price, "bid": bid, "ask": ask, "ts": int(ts)}


def classify_headline(title):
    """Explicit title rules, not an LLM, fact check or win-probability model."""
    t = title.lower()
    assets = [p for p, terms in {"BTC-USD": (r"\bbitcoin\b", r"\bbtc\b"), "ETH-USD": (r"\bethereum\b", r"\bether\b", r"\beth\b"), "SOL-USD": (r"\bsolana\b", r"\bsol\b")}.items() if any(re.search(term, t) for term in terms)]
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
        return [dict(r) for r in c.execute("SELECT * FROM bars WHERE pair=? AND interval=? ORDER BY ts", (pair, interval)) if not closed or r["ts"] + interval <= now]
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


def open_position(c, pair, direction, quote, evidence, now):
    sign = direction_sign(direction)
    execution = (quote["ask"] if sign == 1 else quote["bid"]) * (1 + sign * SLIPPAGE)
    eq, cash = equity_value(c), float(get(c, "cash"))
    exposure = sum(p["qty"] * p["mark"] for p in c.execute("SELECT * FROM positions"))
    # Include entry/exit fees, assumed adverse slippage and one day of holding cost in planned risk.
    risk_per_dollar = STOP + FEE * (2 + STOP) + 2 * SLIPPAGE + 3 * HOLDING_RATE
    notional = min(eq * RISK / risk_per_dollar, eq * .5, max(0, eq * 2 - exposure), cash / (1 / LEVERAGE + FEE))
    if notional < 10: return "Insufficient free margin or exposure allowance"
    margin, qty, fee = notional / LEVERAGE, notional / execution, notional * FEE
    stop, target = execution * (1 - sign * STOP), execution * (1 + sign * STOP * 2)
    payload = json.dumps(evidence, separators=(",", ":"))
    put(c, "cash", cash - margin - fee)
    c.execute("INSERT INTO positions(pair,qty,entry,entry_fee,stop,peak,opened,direction,leverage,margin,target,initial_stop,funding,funding_at,evidence,mark,mark_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (pair, qty, execution, fee, stop, execution, now, direction, LEVERAGE, margin, target, stop, 0, now, payload, quote["price"], quote["ts"]))
    c.execute("INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl,direction,action,leverage,margin,funding,evidence) VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,0,?)", (now, pair, "BUY" if sign == 1 else "SELL", qty, execution, fee, "news_aware_breakout" if sign == 1 else "news_aware_breakdown", direction, "OPEN_" + direction, LEVERAGE, margin, payload))
    put(c, "entry_bar_" + pair, evidence["bar"])
    return None


def close_position(c, p, quote, reason, now):
    sign = direction_sign(p["direction"])
    execution = (quote["bid"] if sign == 1 else quote["ask"]) * (1 - sign * SLIPPAGE)
    exit_fee = p["qty"] * execution * FEE
    gross_pnl = sign * (execution - p["entry"]) * p["qty"]
    credit = max(0., p["margin"] + gross_pnl - exit_fee - p["funding"])
    pnl = credit - p["margin"] - p["entry_fee"]
    put(c, "cash", float(get(c, "cash")) + credit)
    c.execute("DELETE FROM positions WHERE pair=?", (p["pair"],))
    c.execute("INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl,direction,action,leverage,margin,funding,evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (now, p["pair"], "SELL" if sign == 1 else "BUY", p["qty"], execution, exit_fee, reason, pnl, p["direction"], "CLOSE_" + p["direction"], p["leverage"], p["margin"], p["funding"], p["evidence"]))
    put(c, "exit_at_" + p["pair"], now)


def mark_positions(quotes, now):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        # Day boundary starts before these new marks; a gap cannot erase the day's loss baseline.
        roll_day(c, now)
        for row in c.execute("SELECT * FROM positions").fetchall():
            p = dict(row); quote = quotes.get(p["pair"])
            if not quote or not -5 <= now - quote["ts"] <= 45: continue
            elapsed = max(0, now - (p["funding_at"] or p["opened"]))
            p["funding"] += p["qty"] * p["entry"] * HOLDING_RATE * elapsed / 28800
            p["mark"], p["mark_ts"], p["funding_at"] = quote["price"], quote["ts"], now
            sign = direction_sign(p["direction"])
            # Longs exit at bid, shorts at ask. Never mark a short as a spot asset.
            exit_mark = quote["bid"] if sign == 1 else quote["ask"]
            isolated_equity = p["margin"] + sign * (exit_mark - p["entry"]) * p["qty"] - p["funding"]
            reason = "liquidation" if isolated_equity <= p["qty"] * exit_mark * MAINTENANCE else "stop" if sign * (exit_mark - p["stop"]) <= 0 else "target" if p["target"] and sign * (exit_mark - p["target"]) >= 0 else None
            if reason:
                close_position(c, p, quote, reason, now)
            else:
                p["peak"] = max(p["peak"], exit_mark) if sign == 1 else min(p["peak"], exit_mark)
                if sign * (p["peak"] / p["entry"] - 1) >= STOP:
                    trail = p["peak"] * (1 - sign * .02)
                    p["stop"] = max(p["stop"], trail) if sign == 1 else min(p["stop"], trail)
                c.execute("UPDATE positions SET stop=?,peak=?,funding=?,funding_at=?,mark=?,mark_ts=? WHERE pair=?", (p["stop"], p["peak"], p["funding"], now, p["mark"], p["mark_ts"], p["pair"]))
        roll_day(c, now)
        c.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?)", (now, equity_value(c), float(get(c, "cash"))))
        if quotes: put(c, "last_mark", iso(now))


def decide(pair, quote, now):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        ev = technical_evidence(c, pair, now)
        news = news_context(c, pair, now)
        ev["news"] = news
        reasons = list(ev["reasons"]) + risk_blocks(c, now)
        direction = ev["direction"]
        bar = ev.get("bar", int(now // 300) * 300 - 300)
        ev["bar"] = bar
        if c.execute("SELECT 1 FROM positions WHERE pair=?", (pair,)).fetchone(): reasons.append("Position already open; monitoring protective exits")
        if get(c, "entry_bar_" + pair) == str(bar): reasons.append("Entry already used this 5M candle")
        if now - float(get(c, "exit_at_" + pair) or 0) < 1800: reasons.append("30-minute cooldown after prior exit")
        if not quote or not -5 <= now - quote["ts"] <= 45: reasons.append("Fresh executable bid/ask unavailable")
        elif (quote["ask"] - quote["bid"]) / quote["price"] > .003: reasons.append("Bid/ask spread exceeds 0.3%; entry blocked")
        elif direction and abs(quote["price"] / ev.get("five_minute_close", quote["price"]) - 1) > .01: reasons.append("Price moved over 1% from confirmation; do not chase")
        if not news["available"]: reasons.append("No fresh verified news feed coverage; entry blocked")
        if news["cooldown"]: reasons.append("Breaking-event cooldown; wait for post-news confirmation")
        if news["bias"] in ("CONFLICT", "UNCONFIRMED"): reasons.append("Directional news conflicting or not corroborated")
        if direction == "LONG" and news["bias"] == "BEARISH" or direction == "SHORT" and news["bias"] == "BULLISH": reasons.append("Corroborated news opposes technical direction")
        status = "WAIT"
        if not reasons:
            error = open_position(c, pair, direction, quote, ev, now)
            if error: reasons.append(error)
            else: status = "OPEN_" + direction; reasons = ["1H bias, 15M/5M confirmation, verified structural level, news and margin/risk gates passed"]
        c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?) ON CONFLICT(pair,bar) DO UPDATE SET ts=excluded.ts,status=excluded.status,direction=excluded.direction,reasons=excluded.reasons,evidence=excluded.evidence WHERE decisions.status='WAIT'", (pair, bar, now, status, direction, json.dumps(reasons), json.dumps(ev)))
        return {"status": status, "reasons": reasons}


async def scanner():
    await asyncio.sleep(2)
    async with httpx.AsyncClient(headers={"User-Agent": "ChiefPaperResearch/2.0"}, timeout=15, follow_redirects=False) as client:
        next_signal = 0
        while True:
            now = int(time.time()); quotes = {}; errors = []
            for pair in PAIRS:
                try: quotes[pair] = await fetch_quote(client, pair)
                except Exception as exc: errors.append(pair + " quote: " + type(exc).__name__)
            mark_positions(quotes, int(time.time()))
            if now >= next_signal:
                # Protective marks run in their own task while news/candles refresh.
                await refresh_news(client)
                for pair in PAIRS:
                    try:
                        for interval in (300, 3600, 86400):
                            rows = await fetch_bars(client, pair, interval)
                            with db() as c: store_bars(c, pair, interval, rows)
                        # Re-read executable prices after network calls; no stale snapshot entries.
                        quote = await fetch_quote(client, pair)
                        decision = decide(pair, quote, int(time.time()))
                        log.info("PAPER %s %s: %s", pair, decision["status"], "; ".join(decision["reasons"]))
                    except Exception as exc:
                        errors.append(pair + " strategy: " + type(exc).__name__)
                        log.exception("Strategy scan failed: %s", pair)
                        with db() as c:
                            t = int(time.time()); b = t // 300 * 300 - 300
                            c.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?)", (pair, b, t, "WAIT", None, json.dumps(["Strategy input failed: " + type(exc).__name__]), "{}"))
                with db() as c:
                    put(c, "last_scan", iso())
                    if not errors: put(c, "last_success", iso())
                    # Retain every fill and its entry evidence; bound repetitive wait/mark history.
                    c.execute("DELETE FROM decisions WHERE status='WAIT' AND ts<?", (now - 30 * 86400,))
                    c.execute("DELETE FROM equity WHERE ts<?", (now - 90 * 86400,))
                    c.execute("DELETE FROM bars WHERE ts<? AND interval IN (300,3600)", (now - 35 * 86400,))
                next_signal = time.time() + POLL
            with db() as c: put(c, "last_error", "; ".join(errors))
            await asyncio.sleep(MARK_POLL)


async def protective_monitor():
    async with httpx.AsyncClient(headers={"User-Agent": "ChiefPaperProtection/2.0"}, timeout=8, follow_redirects=False) as client:
        while True:
            with db() as c: pairs = [r[0] for r in c.execute("SELECT pair FROM positions")]
            quotes = {}
            for pair in pairs:
                try: quotes[pair] = await fetch_quote(client, pair)
                except Exception:
                    log.warning("Protective quote unavailable: %s; no invented fill", pair)
            if quotes: mark_positions(quotes, int(time.time()))
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
        success, scan, err = get(c, "last_success"), get(c, "last_scan"), get(c, "last_error")
        try: ok = time.time() - datetime.fromisoformat(success).timestamp() < max(900, POLL * 3) and not err
        except (ValueError, TypeError): ok = False
        n = news_context(c, PAIRS[0], int(time.time()))
    return JSONResponse({"status": "ok" if ok else "degraded", "version": VERSION, "mode": "PAPER_ONLY", "live_trading": False, "last_scan": scan, "error": err, "capabilities": ["LONG", "SHORT", "SIMULATED_ISOLATED_MARGIN", "NEWS_GATES", "DURABLE_JOURNAL"], "leverage": LEVERAGE, "news_ready": n["available"]}, status_code=200 if ok else 503)


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
            p["liquidation_estimate"] = p["entry"] * (1 + direction_sign(p["direction"]) * (MAINTENANCE - 1 / p["leverage"]))
            p["evidence"] = json.loads(p["evidence"])
        trades = [dict(t) for t in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 100")]
        for t in trades: t["evidence"] = json.loads(t["evidence"])
        decisions = [parsed_decision(r) for r in c.execute("SELECT d.* FROM decisions d JOIN (SELECT pair,MAX(bar) bar FROM decisions GROUP BY pair) x USING(pair,bar) ORDER BY pair")]
        j = c.execute("SELECT COUNT(CASE WHEN action LIKE 'CLOSE_%' THEN 1 END) completed,COUNT(CASE WHEN action LIKE 'OPEN_%' THEN 1 END) opened,COALESCE(SUM(pnl),0) net,COALESCE(SUM(fee),0) fees,COALESCE(SUM(funding),0) funding FROM trades WHERE ts>=?", (day_floor(now),)).fetchone()
        totals = dict(c.execute("SELECT COUNT(CASE WHEN action LIKE 'CLOSE_%' THEN 1 END) completed,COALESCE(SUM(pnl),0) net,COALESCE(SUM(fee),0) fees,COALESCE(SUM(funding),0) funding FROM trades").fetchone())
        curve = [dict(e) for e in c.execute("SELECT * FROM equity ORDER BY ts DESC LIMIT 200")][::-1]
        return {"version": VERSION, "mode": "PAPER_ONLY", "markets": PAIRS, "cash": round(float(get(c, "cash")), 6), "equity": round(equity_value(c), 6), "positions": positions, "trades": trades, "equity_history": curve, "last_scan": get(c, "last_scan"), "last_success": get(c, "last_success"), "last_mark": get(c, "last_mark"), "last_error": get(c, "last_error"), "entries_paused": bool(risk_blocks(c, now)), "pause_reasons": risk_blocks(c, now), "emergency_paused": get(c, "emergency_paused") == "true", "controls_supported": True, "fee_rate": FEE, "slippage_rate": SLIPPAGE, "journal": {"day": get(c, "day"), **dict(j), "limit": 5, "reserved": len(positions)}, "totals": totals, "decisions": decisions, "news": {p: news_context(c, p, now) for p in PAIRS}, "settings": {"leverage": LEVERAGE, "max_leverage": 3, "risk_per_trade_pct": RISK * 100, "daily_loss_pct": DAILY_LOSS * 100, "stop_pct": STOP * 100, "target_r": 2, "mark_poll_seconds": MARK_POLL, "holding_cost_per_8h": HOLDING_RATE, "maintenance_margin_rate": MAINTENANCE, "max_positions": 2}, "disclosure": "Synthetic isolated-margin simulation on Coinbase SPOT prices, not a Coinbase derivatives account. Bid/ask + assumed slippage/fees. Assumed holding cost on both sides, not actual funding. Stops/targets sampled about every 15s; fast crossings can be missed and outages delay exits. Liquidation is an assumed model, not exchange rules. No historical news backtest or profitability claim."}


@app.get("/api/journal")
def journal(before: int = 9223372036854775807):
    with db() as c:
        trades = [dict(r) for r in c.execute("SELECT * FROM trades WHERE id<? ORDER BY id DESC LIMIT 100", (before,))]
        return {"mode": "PAPER_ONLY", "trades": trades, "next_before": trades[-1]["id"] if len(trades) == 100 else None, "decisions": [parsed_decision(r) for r in c.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT 100")]}


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


HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Chief Paper Gym</title><style>body{background:#0b1510;color:#e7efe9;font:16px system-ui;max-width:1100px;margin:auto;padding:24px}section{border:1px solid #405446;border-radius:12px;padding:18px;margin:16px 0;overflow:auto}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:10px;border-bottom:1px solid #405446;white-space:nowrap}small{color:#b5c5ba}a{color:#e3bd65}</style></head><body><h1>Chief Paper Gym · LONG / SHORT</h1><p>Simulated isolated margin only. No real orders.</p><p id="status">Loading authenticated engine…</p><section id="balance"></section><section><h2>Open positions</h2><table id="positions"></table></section><section><h2>Why Chief is waiting</h2><div id="decisions"></div></section><section><h2>Recorded paper fills</h2><table id="trades"></table></section><p id="disclosure"></p><script>
function table(id,headers,rows){let t=document.getElementById(id);t.replaceChildren();let h=t.createTHead().insertRow();for(let s of headers){let c=document.createElement('th');c.textContent=s;h.append(c)}let b=t.createTBody();for(let r of rows){let tr=b.insertRow();for(let v of r)tr.insertCell().textContent=String(v)}if(!rows.length)b.insertRow().insertCell().textContent='No records yet'}
async function refresh(){try{let r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw Error('Connect with your engine dashboard login');let d=await r.json();document.getElementById('status').textContent=d.version+' · '+d.settings.leverage+'x simulated leverage · last scan '+d.last_scan;document.getElementById('balance').textContent='Equity $'+d.equity.toFixed(2)+' · Free margin $'+d.cash.toFixed(2)+' · Closed trades '+d.totals.completed+' · Net $'+d.totals.net.toFixed(2);document.getElementById('disclosure').textContent=d.disclosure;table('positions',['Pair','Direction','Leverage','Entry','Mark','Stop','Target','Margin','Net P&L'],d.positions.map(p=>[p.pair,p.direction,p.leverage+'x',p.entry,p.mark,p.stop,p.target,p.margin,p.unrealized_pnl]));table('trades',['UTC','Pair','Action','Leverage','Price','Fee','Holding cost','Net P&L','Reason'],d.trades.map(t=>[new Date(t.ts*1000).toISOString(),t.pair,t.action,t.leverage+'x',t.price,t.fee,t.funding,t.pnl===null?'—':t.pnl,t.reason]));let el=document.getElementById('decisions');el.replaceChildren();for(let v of d.decisions){let p=document.createElement('p');p.textContent=v.pair+' · '+v.status+' · '+v.reasons.join('; ');el.append(p)}}catch(e){document.getElementById('status').textContent=e.message}}refresh();setInterval(refresh,15000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)
