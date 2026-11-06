"""Chief paper-only crypto simulator. Public Coinbase data; no trading APIs."""
import asyncio
import base64
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chief")
PAIRS = ("BTC-USD", "ETH-USD", "SOL-USD")
DB_PATH = os.getenv("DB_PATH", "/data/chief.sqlite")
START_CASH = float(os.getenv("PAPER_STARTING_CASH", "500"))
FEE = float(os.getenv("PAPER_FEE_RATE", "0.006"))
SLIPPAGE = float(os.getenv("PAPER_SLIPPAGE", "0.001"))
POLL = max(60, int(os.getenv("POLL_SECONDS", "300")))
PAUSED = os.getenv("PAPER_PAUSED", "false").lower() == "true"

def db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def init():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS candles(pair TEXT, ts INTEGER, close REAL, volume REAL, PRIMARY KEY(pair,ts))")
        c.execute("CREATE TABLE IF NOT EXISTS positions(pair TEXT PRIMARY KEY, qty REAL, entry REAL, entry_fee REAL, stop REAL, peak REAL, opened INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, pair TEXT, side TEXT, qty REAL, price REAL, fee REAL, reason TEXT, pnl REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS equity(ts INTEGER PRIMARY KEY, value REAL, cash REAL)")
        for k,v in (("cash",str(START_CASH)),("day",""),("day_start",str(START_CASH)),("last_scan","never"),("last_success","never"),("last_error","")):
            c.execute("INSERT OR IGNORE INTO state VALUES (?,?)",(k,v))

def get(c,k):
    row=c.execute("SELECT v FROM state WHERE k=?",(k,)).fetchone()
    return row["v"] if row else ""

def put(c,k,v):
    c.execute("INSERT OR REPLACE INTO state VALUES (?,?)",(k,str(v)))

def marks(c):
    rows=c.execute("SELECT a.pair,a.close FROM candles a JOIN (SELECT pair,MAX(ts) ts FROM candles GROUP BY pair) b ON a.pair=b.pair AND a.ts=b.ts").fetchall()
    return {r["pair"]:r["close"] for r in rows}

def equity_value(c):
    cash=float(get(c,"cash"))
    prices=marks(c)
    positions=c.execute("SELECT * FROM positions").fetchall()
    return cash+sum(p["qty"]*prices.get(p["pair"],p["entry"]) for p in positions)

async def fetch_bars(client,pair):
    r=await client.get("https://api.exchange.coinbase.com/products/"+pair+"/candles",params={"granularity":3600},timeout=20)
    r.raise_for_status()
    data=r.json()
    if not isinstance(data,list):
        raise ValueError("Unexpected public market data")
    now=int(time.time())
    return sorted([(int(x[0]),float(x[4]),float(x[5])) for x in data if isinstance(x,list) and len(x)>=6 and int(x[0])+3600<=now])

def evaluate(pair,bars):
    now=int(time.time())
    today=datetime.now(timezone.utc).date().isoformat()
    with db() as c:
        for ts,close,volume in bars:
            c.execute("INSERT OR REPLACE INTO candles VALUES (?,?,?,?)",(pair,ts,close,volume))
        rows=list(reversed(c.execute("SELECT * FROM candles WHERE pair=? ORDER BY ts DESC LIMIT 55",(pair,)).fetchall()))
        if len(rows)<26:
            return
        last=rows[-1]
        key="bar_"+pair
        if get(c,key)==str(last["ts"]):
            return  # never trade the same closed candle twice
        price=last["close"]
        if price<=0:
            raise ValueError("Non-positive market close")
        v=last["volume"]
        prev=rows[-25:-1]
        avg_v=sum(r["volume"] for r in prev)/len(prev)
        mom=price/rows[-5]["close"]-1
        breakout=price>max(r["close"] for r in prev)
        cash=float(get(c,"cash"))
        eq=equity_value(c)
        if get(c,"day")!=today:
            put(c,"day",today)
            put(c,"day_start",eq)
        day_start=float(get(c,"day_start"))
        positions=c.execute("SELECT * FROM positions").fetchall()
        pos=next((p for p in positions if p["pair"]==pair),None)
        if pos is not None:
            peak=max(pos["peak"],price)
            stop=max(pos["stop"],peak*.97 if peak>=pos["entry"]*1.04 else pos["stop"])
            c.execute("UPDATE positions SET peak=?,stop=? WHERE pair=?",(peak,stop,pair))
            reason="stop" if price<=stop else ("momentum_fade" if mom<-.025 else None)
            if reason:
                execution=price*(1-SLIPPAGE)
                gross=pos["qty"]*execution
                exit_fee=gross*FEE
                pnl=gross-exit_fee-(pos["qty"]*pos["entry"]+pos["entry_fee"])
                put(c,"cash",cash+gross-exit_fee)
                c.execute("DELETE FROM positions WHERE pair=?",(pair,))
                c.execute("INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl) VALUES (?,?,?,?,?,?,?,?)",(now,pair,"SELL",pos["qty"],execution,exit_fee,reason,pnl))
        elif (not PAUSED and eq>=day_start*.98 and len(positions)<2
              and breakout and mom>.02 and avg_v>0 and v>avg_v*1.5):
            # Notional <=20% equity; modeled stop-loss risk <=0.5% equity.
            budget=min(cash/(1+FEE),eq*.20,eq*.005/.03)
            if budget>=10:
                execution=price*(1+SLIPPAGE)
                qty=budget/execution
                fee=budget*FEE
                put(c,"cash",cash-budget-fee)
                c.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",(pair,qty,execution,fee,execution*.97,execution,now))
                c.execute("INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl) VALUES (?,?,?,?,?,?,?,?)",(now,pair,"BUY",qty,execution,fee,"breakout_volume",None))
        put(c,key,last["ts"])
        c.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?)",(now,equity_value(c),float(get(c,"cash"))))

async def scanner():
    await asyncio.sleep(2)
    async with httpx.AsyncClient(headers={"User-Agent":"ChiefPaperTrader/1.0"}) as client:
        while True:
            errors=[]
            for pair in PAIRS:
                try:
                    bars=await fetch_bars(client,pair)
                    if len(bars)<26:
                        raise ValueError("Insufficient historical candles")
                    evaluate(pair,bars)
                except Exception as exc:
                    errors.append(pair+": "+str(exc)[:160])
                    log.exception("Scanner failed: %s",pair)
            with db() as c:
                put(c,"last_scan",datetime.now(timezone.utc).isoformat())
                put(c,"last_error","; ".join(errors))
                if not errors:
                    put(c,"last_success",datetime.now(timezone.utc).isoformat())
            await asyncio.sleep(POLL)

@asynccontextmanager
async def lifespan(app):
    init()
    task=asyncio.create_task(scanner())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app=FastAPI(title="Chief | Paper Only",lifespan=lifespan)

@app.middleware("http")
async def auth(req:Request,call_next):
    if req.url.path=="/health":
        return await call_next(req)
    user=os.getenv("DASHBOARD_USER","")
    password=os.getenv("DASHBOARD_PASSWORD","")
    if not user or not password:
        return Response("Dashboard credentials must be set",status_code=503)
    provided=req.headers.get("authorization","")
    valid=False
    try:
        scheme,value=provided.split(" ",1)
        name,pw=base64.b64decode(value,validate=True).decode().split(":",1)
        valid=(scheme.lower()=="basic" and secrets.compare_digest(name,user) and secrets.compare_digest(pw,password))
    except (ValueError,UnicodeDecodeError):
        pass
    if not valid:
        return Response("Authentication required",status_code=401,headers={"WWW-Authenticate":'Basic realm="Chief Paper Trading"'})
    response=await call_next(req)
    response.headers["Cache-Control"]="no-store"
    return response

@app.get("/health")
def health():
    with db() as c:
        scan=get(c,"last_scan")
        success=get(c,"last_success")
        err=get(c,"last_error")
    try:
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(success)).total_seconds()
        ok=age<max(900,POLL*3) and not err
    except (ValueError,TypeError):
        ok=False
    return JSONResponse({"status":"ok" if ok else "degraded","mode":"PAPER_ONLY","live_trading":False,"last_scan":scan,"error":err},status_code=200 if ok else 503)

@app.get("/api/dashboard")
def dashboard():
    with db() as c:
        positions=[dict(p) for p in c.execute("SELECT * FROM positions")]
        pmarks=marks(c)
        for p in positions:
            p["mark"]=pmarks.get(p["pair"],p["entry"])
            p["unrealized_pnl"]=(p["mark"]-p["entry"])*p["qty"]-p["entry_fee"]
        cash=float(get(c,"cash"))
        trades=[dict(t) for t in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 100")]
        curve=[dict(e) for e in c.execute("SELECT * FROM equity ORDER BY ts DESC LIMIT 200")][::-1]
        return {"mode":"PAPER_ONLY","markets":PAIRS,"cash":round(cash,2),"equity":round(equity_value(c),2),"positions":positions,"trades":trades,"equity_history":curve,"last_scan":get(c,"last_scan"),"last_success":get(c,"last_success"),"last_error":get(c,"last_error"),"entries_paused":PAUSED or equity_value(c)<float(get(c,"day_start"))*.98}

HTML=String = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8"><title>CHIEF | Paper Only</title><style>
body{background:#0d1421;color:#e7efff;font:15px system-ui;margin:0;padding:25px;max-width:1050px}
h1{margin:0}small{color:#a9bad0}b.mode{color:#68e6ad}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin:24px 0}
.tile,section{background:#19243a;border:1px solid #35425c;border-radius:14px;padding:18px}strong{font-size:25px}section{margin-top:18px;overflow:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:10px;border-bottom:1px solid #344158;white-space:nowrap;font-size:13px}
#error{color:#ffb0b0}</style></head><body>
<h1>CHIEF <b class="mode">● PAPER ONLY</b></h1><small>BTC / ETH / SOL · Coinbase public candles · simulated trades · no real money</small>
<div class="tiles"><div class="tile">Equity<br><strong id="equity">—</strong></div><div class="tile">Cash<br><strong id="cash">—</strong></div><div class="tile">Open positions<br><strong id="open">—</strong></div><div class="tile">Last scan<br><small id="scan">—</small></div></div>
<p id="error"></p><section><h2>Open simulated positions</h2><table><thead><tr><th>Pair</th><th>Quantity</th><th>Entry</th><th>Mark</th><th>Unrealized P&L</th></tr></thead><tbody id="positions"></tbody></table></section>
<section><h2>Trade log</h2><table><thead><tr><th>UTC</th><th>Pair</th><th>Side</th><th>Quantity</th><th>Price</th><th>Fee</th><th>Reason</th><th>Realized P&L</th></tr></thead><tbody id="trades"></tbody></table></section>
<p><small>Updates every 30 seconds. Modeling includes assumed fees, slippage and stops at hourly closes; actual execution can differ dramatically.</small></p>
<script>
function money(v){return '$'+Number(v).toFixed(2)}function qty(v){return Number(v).toLocaleString('en-US',{maximumFractionDigits:8})}
function table(id,rows,fields){let body=document.getElementById(id);body.replaceChildren();if(!rows.length){let tr=body.insertRow();let td=tr.insertCell();td.colSpan=fields.length;td.textContent='No records yet';return}for(let r of rows){let tr=body.insertRow();for(let f of fields){let td=tr.insertCell();td.textContent=f(r)}}}
async function refresh(){try{let r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw Error('Dashboard unavailable');let d=await r.json();document.getElementById('equity').textContent=money(d.equity);document.getElementById('cash').textContent=money(d.cash);document.getElementById('open').textContent=d.positions.length;document.getElementById('scan').textContent=d.last_scan;document.getElementById('error').textContent=d.last_error||'';table('positions',d.positions,[x=>x.pair,x=>qty(x.qty),x=>money(x.entry),x=>money(x.mark),x=>money(x.unrealized_pnl)]);table('trades',d.trades,[x=>new Date(x.ts*1000).toISOString(),x=>x.pair,x=>x.side,x=>qty(x.qty),x=>money(x.price),x=>money(x.fee),x=>x.reason,x=>x.pnl===null?'—':money(x.pnl)])}catch(e){document.getElementById('error').textContent=e.message}}
refresh();setInterval(refresh,30000)
</script></body></html>"""

@app.get("/",response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)
