# Chief Paper Gym v2.5 · U.S. nano-futures

Private, paper-only BTC/ETH/SOL practice. No exchange credentials, private
exchange API, order endpoint, transfers or live trading. The owner selected
Coinbase U.S. futures with an $80–$150 intended starting range.

## Active account and history

Production uses `PAPER_PRACTICE_VENUE=COINBASE_US_FUTURES`,
`PAPER_SIGNAL_SOURCE=SPOT_REFERENCE`, `PAPER_SIZING_MODE=FULL_MARGIN`,
`PAPER_STARTING_CASH=80`, `DB_PATH=/data/chief-practice-80.sqlite` on the
existing mounted volume. Earlier `/data/chief.sqlite` history stays intact.
Initialization migrates schemas in place and never resets cash or tops up losses.
The authenticated dashboard compares $80/$100/$150 minimum-contract examples;
those comparisons are affordability floors, not three independent performance runs.

## Contract execution and risk

- Select the nearest dated nano BIT / ET / SOL contract more than 24h from expiry
  using public Coinbase product metadata. Perpetual-style products are excluded;
  dated contracts have no perpetual funding or assumed holding charge.
- Setup signals use continuous liquid spot-reference candles stored separately
  in `signal_bars`. Futures fills always use the actual contract book. Both fresh
  prices map structural levels by their current price ratio; >2% basis, wrong
  identity or stale paired quotes block entries. Original reference levels, ratio,
  quote times and mapped levels are retained with the entry evidence.
- Contract candle gaps from sparse trading remain historical chart evidence,
  separate from setup readiness; no trade or volume is fabricated.
  A held contract is pinned across metadata refreshes; selection can roll only
  while flat. Rollover clears old candle history, not fills or accounting records.
- Validate product ID, contract multiplier, whole minimum/increment, maximum
  contracts, tick size, expiry, market-order restrictions and session state.
  Metadata expires after 600s, books after 10s. Book reads use cache-busting and
  one bounded retry; timestamps are never overwritten or freshness relaxed.
  Missing inputs mean WAIT with a structured per-market reason.
- Convert order-book contract quantities to underlying units; walk fresh bids/asks
  for the full whole-contract size, then apply optional extra stress. Spread and
  depth impact enter once. Insufficient depth never invents a complete fill.
- Connected FUTURE Intro fee snapshot: 0.095% maker / 0.10% taker, plus published
  $0.15/contract per side. Paper market fills pay taker. Snapshot checked November
  6, 2026 America/Chicago, not automatically refreshed from a private account.
  Maker fills are not fabricated; recheck the snapshot when the tier changes.
- Selected FULL_MARGIN paper mode uses the maximum affordable whole-contract
  count with entry/exit-fee reserves and one global position. Remainders stay
  unused; whole contracts cannot consume an exact percentage of cash. Structural
  stops, >=2R net scalp gate and daily drawdown halt remain active; the old
  0.25%/0.5% per-trade caps and 2x-equity gross ceiling do not apply in FULL_MARGIN.
  Planned dollar loss and equity/margin percentages are recorded explicitly.
- Optional RISK_LIMITED mode retains <=0.25% scalp / <=0.5% Breakout planned risk,
  <=2x-equity gross exposure and max two markets. Both modes use whole counts,
  one position per market, combined five daily entries/completions and open-slot
  reservation. Five is a ceiling. No real allocation or order access exists.
- Higher of current published intraday/overnight initial margin by direction is
  reserved throughout. No intraday opt-in, margin offsets, isolated-loss cap or
  invented fixed leverage. Account equity and cash deficits can go negative.
- Account-wide protective margin guard uses published INITIAL margin plus exit
  fee reserves as a conservative maintenance proxy. It is not a replica of CFM's
  liquidation engine. Liquidation surcharges and external account transfers are
  not modeled. A gap can exceed the initial deposit; no negative-balance guarantee.
- Protection checks about every 5s. Stop, target, time limit, pre-expiry exit and
  margin protection use the next valid book. Insufficient depth queues the exit
  persistently and blocks new entries until it completes, even if price recovers.
  Outages and intra-sample crossings can delay or miss exits.

## Daily variation and expiry

At each 5pm America/New_York paper cycle, the next fresh contract mid-book mark
moves accrued variation into paper cash and rebases the unsettled position value.
One durable transfer per held position/cycle; no extra trade, fee or doubled P&L.
Original entry and whole-trade net remain intact. This is explicitly
`PAPER_DAILY_VARIATION_ESTIMATE`, not an official exchange settlement price,
clearing statement or clearing schedule. It is paper bookkeeping, not a claim
of exchange-identical variation timing. Exits reconcile already transferred
variation with the remaining price change exactly once.

Positions attempt to close 24h before contract expiry. If the market is closed,
book stale or depth unavailable, the exit stays unresolved until executable
data returns; no cash settlement price or rollover fill is invented.

## Strategies and news

`SNIPER_SCALP`: aligned completed 1H EMA20/50 trend and slope; completed 5M trend
and rolling 15M momentum; 1M sweep/reclaim of the preceding 12-bar 5M range,
then later structure break and newest-candle retest/rejection; entry volume >=1.2x.
Structural stop 0.1–0.8%, target before opposite range capped at 2x 5M ATR,
minimum 2R NET costs. Max 15-minute hold, cost-adjusted breakeven after +1R net,
10-minute scalp cooldown (30 minutes after a preceding Breakout exit).

`BREAKOUT`: 24-bar completed 1H close breakout/breakdown, directional 4H momentum
>2%, volume >1.5x average, completed 15M/5M confirmation and a verified prior
 day/week or Monday range break. Stop 3%, target 2R before costs; trailing after
+1R. Both directions share all margin, exposure, cash and risk controls.

Timestamped CoinDesk, Cointelegraph, Decrypt, Fed and BBC RSS: at least one fresh
crypto feed; two-source directional corroboration; ambiguity, conflict, opposition
and breaking-event cooldowns. Explicit title rules are not fact-checking,
complete economic-calendar coverage or a predictive news model. Missing/gapped
candles are not filled with fabricated trades or volume. Quiet valid markets
remain WAIT without being misreported as transport errors.

2% daily equity drawdown latches an entry halt until the next UTC day.
Authenticated confirmed pause/resume only affects entries; protection continues.
No model training, proven edge or historical news-aware backtest is claimed.

## Private API and UI

Existing Basic-auth dashboard credentials remain unchanged. `/api/dashboard`
returns whole contracts, contract IDs, actual entry/mark prices, net P&L,
margin guard, strategy decisions, all-record totals and last 50 paper variation
transfers. `/api/journal?before=ID` paginates all fills; `/api/contract-candles`
serves bounded verified contract history for fixed pairs and 1m/5m/1h intervals.
Chief proxies these reads through an owner-bound encrypted HttpOnly session;
credentials never enter model context or browser storage. Contract charts and
fill markers stay separate from the public spot research chart.

Wait history: Sniper 7 days / Breakout 30 days. Equity 90 days. Fills, entry
proof and paper variation transfers are not automatically deleted.

Public `/health` exposes capability and input freshness without balances.
Legacy `SPOT_PRICE_PROXY` execution remains supported for the preserved old run;
its synthetic isolated-margin and assumed holding costs never become futures
performance. `PAPER_FEE_RATE` is a legacy spot setting; FUTURE costs are separate.

## Deployment and checks

Keep single replica, `/health`, mounted 1GB `/data`, existing credentials and
all other service configuration. Docker must copy `main.py`,
`practice_budget.py` and `futures_runtime.py`. Only public GET market-data calls.
`python -m pytest -q` verifies unit/accounting and forward integration fixtures.
Fixture funding and successful sample trades are temporary and never injected
into production. Build/dashboard tests additionally verify owner session,
contract identity, deficit preservation and variation display semantics.

Primary references:
- https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product
- https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-book
- https://help.coinbase.com/en/coinbase/derivatives/us-derivatives-leverage-margin
- https://help.coinbase.com/en/coinbase/derivatives/us-derivatives-risk-management-and-liquidations
- https://www.coinbase.com/advanced-vip


## Readiness, recovery and visible activity

`scan_once` isolates reference inputs, contract chart evidence and fresh quotes.
Expected stale quotes become per-market DATA_BLOCKED rather than uncaught scan
errors; successful markets continue. Repeated input errors cannot overwrite a
recorded OPEN decision. Both scan and protection loops retry after exceptions.
Health verifies current-build scan heartbeat, protection heartbeat, and at least
one data-ready market; it exposes each market's status rather than implying
that a running process guarantees ready signals. Authenticated dashboard adds
market_status, signal-source/sizing-mode fields and current activity. The UI
shows IN A PAPER TRADE / NO OPEN PAPER TRADE plus stale status and real positions.
No test fixture or fabricated entry is copied into the production journal.
