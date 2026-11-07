# Chief Paper Gym v2.2 · Sniper Scalp + Breakout

Paper-only BTC / ETH / SOL long/short simulator. It has no exchange credentials,
private exchange APIs, live mode, transfers, or manual order endpoint.

## Execution and risk model

- Synthetic isolated margin on Coinbase **spot** quotes; this is not a derivatives account.
- Defaults to 2x simulated leverage, capped at 3x; optional `PAPER_LEVERAGE`.
- Planned risk per new entry <=0.5% equity including modeled stop, fees, slippage and one day of holding costs. Gaps can exceed planned stop risk.
- Maximum two positions, gross notional <=2x equity, and five daily entries/completions with reservations for open positions. This is a ceiling, not a target.
- A 2% daily equity drawdown latches an entry halt until the next UTC day. Pause/resume never bypasses that halt.
- 3% initial stop, 2R target, trailing stop after a 1R advance. Exits use executable bid/ask snapshots about every 5 seconds, including while strategy/news data refreshes. Brief crossings can be missed; network failures delay exits.
- Connected Coinbase SPOT Intro fee snapshot verified November 6, 2026 (America/Chicago): maker 0.50%, taker 0.90% per side. Paper market fills use taker (`PAPER_FEE_RATE=0.009`). Snapshot must be rechecked when tiers change. Prior fills keep their original fees; exit fees use the current model.
- Execution walks fresh Coinbase public spot-book bids/asks (up to 50 levels per side), weighted by proposed size. Spread and depth impact are included once. Insufficient visible depth prevents a fabricated full fill; protective exits retry while continuing marks and holding costs. Future fills can differ from snapshot estimates.
- `PAPER_SLIPPAGE` is now optional additional stress ON TOP of book impact; default 0, not a claim of zero slippage. Holding cost remains an explicit 0.01% assumed cost per 8 hours, prorated on both sides, **not exchange funding**.
- Connected U.S. CFM futures reference: maker 0.095%, taker 0.10% per side plus additional contract charges. These rates are NOT applied to spot-price synthetic margin. Contract-specific execution, fees and margin would require a separate futures model.
- Sources: https://www.coinbase.com/advanced-vip ; https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/slippage ; https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-book
- Modeled maintenance margin is 5% of marked notional. Gap losses are capped at the position's isolated margin plus the entry fee. The liquidation model is illustrative, not exchange-specific.

## Entry evidence

Completed 1H close breakout/breakdown of the prior 24 closes, directional 4H momentum >2%, hourly volume >1.5x prior average, completed 15M/5M trend confirmation, and a verified prior-day/week or Monday high/low break. Both directions use symmetric checks. Daily/weekly/monthly/quarterly/yearly and Asia/London/New York ranges include coverage and DST-aware session boundaries; missing history and all-time extrema remain explicitly unavailable.

News gates consume fixed HTTPS RSS sources: CoinDesk, Cointelegraph, Decrypt,
Federal Reserve and BBC World. Publication and first-seen times, outlet, source URL,
and exact title are recorded. Future dates and invalid feeds are rejected. At least
one fresh crypto feed is required; directional headline rules require two distinct
outlets and de-duplicate identical syndicated titles. Conflicting, uncorroborated or
opposing directional evidence blocks entries. Breaking events trigger a 30-minute
publication / 15-minute first-seen cooldown. Neutral news permits qualified technical
setups. Headlines are rule-classified, not fact-checked or treated as proof of future returns.
This is an RSS news filter, not a complete economic-event calendar or a general-purpose
geopolitical reasoning model. There is no historical news-aware performance backtest.

## Persistence and API

Existing SQLite data is migrated in place; old long positions stay at 1x and old
fills remain intact. Each new fill retains the entry evidence and costs. `/api/dashboard`
returns verified daily/all-record totals, margin, direction, targets/stops, current
news status, and the latest recorded decision for each market. `/api/journal?before=ID`
paginates every persisted fill. Repetitive Breakout WAIT decisions retain 30 days; Sniper WAIT decisions retain 7 days; equity
snapshots retain 90 days. Fills and entry evidence are never automatically pruned.

Dashboard and private APIs require the existing Basic-auth dashboard login.
The only supported mutation is the confirmed paper entry pause/resume at `/api/control`.
Public `/health` reports version, paper-only mode and capabilities without balances.

## Railway configuration

Keep the existing mounted volume at `/data`, single replica, `/health` healthcheck,
and `DB_PATH=/data/chief.sqlite`. Existing `DASHBOARD_USER`, `DASHBOARD_PASSWORD`,
`PAPER_STARTING_CASH`, `PAPER_FEE_RATE`, `PAPER_SLIPPAGE`, `PAPER_PAUSED` and
`POLL_SECONDS` remain supported. `POLL_SECONDS` defaults to 300 for strategy scans;
protective quote polling remains separate at 5 seconds.

Tests: `pip install -r requirements.txt pytest && python -m pytest -q`.
Fixtures test accounting, both directions, stops, targets, liquidation gaps, migration,
restart idempotence, risk limits, RSS freshness/corroboration, and API isolation.
Test fills use a temporary database and are never published to the live paper journal.


## Sniper Scalp

The `SNIPER_SCALP` strategy is enabled alongside `BREAKOUT`, with separate decisions,
strategy-tagged fills and verified all-record performance statistics. Both share one
position per pair, two open positions, cash, gross exposure, a 2% daily drawdown halt,
existing simulated leverage, and the same combined five-trade daily ceiling.

- 1H EMA20/EMA50 trend and EMA20 slope must agree.
- Completed 5M trend and rolling 15-minute momentum must agree.
- A 1M candle sweeps and reclaims the prior 12-bar 5M range extreme. Its reference
  range must have closed before the sweep. This is price action, not order-book data.
- A later completed 1M candle breaks the sweep candle's structure; the newest
  completed 1M candle must retest and reject that level in the trend direction.
- Entry volume >=1.2x preceding 20-bar 1M average. Quote age <=10s; no chasing
  beyond 0.15% of confirmation price. Existing news gates apply unchanged.
- Stop beyond the sweep, 0.1%-0.8% from executable entry. Target before the opposite
  observed range, capped at 2x 5M ATR; target distance is never inflated to force 2R.
- Require >=2R NET reward/risk after configured fees, bid/ask, slippage and a full
  15 minutes of assumed holding cost. Planned risk <=0.25% equity. Leverage stays
  the existing configured paper value. High-cost small targets correctly mean WAIT.
- A cost-adjusted breakeven stop activates at +1R net. Maximum holding time is
  15 minutes, exited at the next valid sampled quote. Ten-minute cooldown follows
  a sniper exit; a preceding breakout exit retains its 30-minute cooldown.

One-minute bars are read once per completed minute; slower structure/news history
refreshes every five minutes. Protection is a separate loop at about five seconds;
network latency/outages and unobserved intra-sample crossings still apply.

Chief's chat playbook explains these rules. Relevant chat requests receive bounded,
authenticated paper evidence through the existing owner-bound encrypted session;
no engine login or cookie is passed to the model. If unauthenticated, Chief explains
that private records are unavailable instead of inventing activity.

This configures a fixed-rule forward-test strategy; it does not train a predictive
model, establish a profitable edge, or complete historical news-aware backtesting.
Offline tests include a deliberately lower-fee fixture to exercise successful scalp
accounting. Those fixture fees and trades NEVER replace production settings/history.

## Small-account U.S. futures readiness (v2.3)

Rene selected Coinbase U.S. futures and an $80–$150 live-capital range. The
production small-account run uses `PAPER_STARTING_CASH=80`,
`DB_PATH=/data/chief-practice-80.sqlite` and
`PAPER_PRACTICE_VENUE=COINBASE_US_FUTURES`. The previous `/data/chief.sqlite`
remains on the mounted volume intact; this is a new run, not a history rewrite.
Restarts preserve the new balance and never top it up automatically.

The authenticated budget report compares $80/$100/$150 using fresh public dated
nano-contract metadata and mid-book snapshots, whole minimum contracts, higher
of intraday/overnight margin by direction, and the connected FUTURE Intro taker
snapshot (0.10% plus published $0.15/contract per side). Estimates include both
fees; example price stop is 0.3%, risk 0.25%. These are affordability floors,
excluding spread/depth, liquidation charges and settlement details. Missing,
closed or stale (>600s) contract data never claims readiness. No rollover trades,
fractional contracts or margin offsets are invented.

Full U.S. futures fills, clearing, variation margin and settlement are not
implemented. For this selected venue, entries are explicitly blocked, including
direct calls to open_position: no spot-price proxy results are published as
futures performance. Strategy/news scans still record WAIT and the budget reason.
Legacy SPOT_PRICE_PROXY mode remains available for the preserved earlier run.
