# Chief Paper Gym v2

Paper-only BTC / ETH / SOL long/short simulator. It has no exchange credentials,
private exchange APIs, live mode, transfers, or manual order endpoint.

## Execution and risk model

- Synthetic isolated margin on Coinbase **spot** quotes; this is not a derivatives account.
- Defaults to 2x simulated leverage, capped at 3x; optional `PAPER_LEVERAGE`.
- Planned risk per new entry <=0.5% equity including modeled stop, fees, slippage and one day of holding costs. Gaps can exceed planned stop risk.
- Maximum two positions, gross notional <=2x equity, and five daily entries/completions with reservations for open positions. This is a ceiling, not a target.
- A 2% daily equity drawdown latches an entry halt until the next UTC day. Pause/resume never bypasses that halt.
- 3% initial stop, 2R target, trailing stop after a 1R advance. Exits use executable bid/ask snapshots about every 15 seconds, including while strategy/news data refreshes. Brief crossings can be missed; network failures delay exits.
- Fees and slippage use existing `PAPER_FEE_RATE` and `PAPER_SLIPPAGE`. Holding cost is an explicit 0.01% assumed cost per 8 hours, prorated on both sides, **not exchange funding**.
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
paginates every persisted fill. Repetitive WAIT decisions retain 30 days; equity
snapshots retain 90 days. Fills and entry evidence are never automatically pruned.

Dashboard and private APIs require the existing Basic-auth dashboard login.
The only supported mutation is the confirmed paper entry pause/resume at `/api/control`.
Public `/health` reports version, paper-only mode and capabilities without balances.

## Railway configuration

Keep the existing mounted volume at `/data`, single replica, `/health` healthcheck,
and `DB_PATH=/data/chief.sqlite`. Existing `DASHBOARD_USER`, `DASHBOARD_PASSWORD`,
`PAPER_STARTING_CASH`, `PAPER_FEE_RATE`, `PAPER_SLIPPAGE`, `PAPER_PAUSED` and
`POLL_SECONDS` remain supported. `POLL_SECONDS` defaults to 300 for strategy scans;
protective quote polling remains separate at 15 seconds.

Tests: `pip install -r requirements.txt pytest && python -m pytest -q`.
Fixtures test accounting, both directions, stops, targets, liquidation gaps, migration,
restart idempotence, risk limits, RSS freshness/corroboration, and API isolation.
Test fills use a temporary database and are never published to the live paper journal.
