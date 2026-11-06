# Chief Paper Trading v1

A **paper-only** BTC / ETH / SOL Coinbase public-market-data simulator.

- No Coinbase API keys, private trading API, order placement or transfers.
- Scans completed 1-hour candles every 5 minutes; simulates trades and fees.
- Limits simulated position risk, open positions and daily new entries.
- Logs simulated orders, positions, equity and scanner status in SQLite.
- Provides password-protected dashboard and public `/health` endpoint.

## Railway configuration

Dockerfile is in repository root. Mount a persistent volume at `/data`.
Set `DB_PATH=/data/chief.sqlite`, `PAPER_STARTING_CASH=500`,
`PAPER_FEE_RATE=0.006`, `PAPER_SLIPPAGE=0.001`, `POLL_SECONDS=300`,
`DASHBOARD_USER` and a strong `DASHBOARD_PASSWORD`.

Configure Railway's HTTP healthcheck to `/health`, restart policy ALWAYS,
and a single application instance. Healthcheck returns HTTP 503 if the
market-data scanner becomes unhealthy.

All performance is simulated and not a forecast. Paper stop execution is
subject to hourly candle gaps and assumed slippage. Coinbase spot data is
accessed through a public unauthenticated HTTPS API.
