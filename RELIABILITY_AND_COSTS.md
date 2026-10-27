# Prepared reliability and cost update — 15 October 2026

Follow-up operations work is documented in `ORACLE_DEPLOYMENT.md` and the implementation checkpoint in `CLOUD_READINESS.md`. The latest test count is 86 plus dashboard checks. The user explicitly approved the restart on 15 October 2026. The updated server is now running locally with the itemized-fee database `data/angel-orb-25k-costs-v2.sqlite3` and the unchanged 30-minute cooldown. Old trading history and the prior environment were backed up. Startup checks passed after close; fresh-market verification remains pending. The zero-cooldown command below is an optional separate experiment, not the active settings.

The versioned itemized fee model is now active in a separate paper experiment. It never rewrites old fills. No live broker orders were sent.

## Implemented

- Explicit transient Angel network/timeout, HTTP 429/5xx and AB9019 failures retry with 15/30/60/120-second backoff plus up to 10% jitter. While a position is held, retries remain at 15 seconds plus jitter. Request duration is additional to this wait. Invalid credentials, malformed responses, uncertain broker state and unexpected exceptions still require attention. Manual pause/halt and daily locks are preserved. There is no periodic process restart.
- Completed session candles persist in `session_candles`. Successful downloads merge by provider/minute; gaps are not synthesized. Failed downloads retain saved candles for display but block entries. A successful subsequent download can backfill the session. The record is available across restarts and separated by trading date/provider.
- Independent dashboard fields show quote status, candle availability/error and saved count, option-delta availability, request collection duration, and next retry. Only a successful snapshot clears a quote error. A no-op halted loop no longer clears it.
- Every public Angel quote/Greek/history response from the adapter is stored in `market_requests`, along with sanitized errors, endpoint, collection time and duration. Authentication payloads/responses, headers and secrets are excluded. Instrument-master files are not archived in full. This is the data fetched by this bot, not a complete exchange tick archive.
- All selected option quotes and completed candles in each snapshot are retained, rather than trimming the saved option list/candles. Each decision includes snapshot reference and policy in `decisions`; orders include cost breakdown and quote/slippage/collection-time metadata. Data captured by older server versions cannot be reconstructed retroactively.
- `app.session_report` reports observed days, no-entry days, completed trades, recorded costs/P&L, endpoint errors/timings and decision reasons. `app.backtest` also reports net expectancy, average win/loss and estimated charges.
- The server reserves its port before database/engine initialization, so an accidental duplicate launch cannot mutate the experiment before discovering the occupied port. This does not replace the planned cloud account/process lock.

## Itemized cost model

`PAPER_FEE_MODEL=angel_nse_2026_10` uses the user's confirmed standard INR 20 per executed options order (`PAPER_FEE_PER_ORDER=20`) and the following versioned estimates for intraday NSE options:

| Component | Rate/base |
| --- | --- |
| Exchange | 0.0355299% of premium turnover, each side |
| STT | 0.15% of sell premium turnover |
| Stamp duty | 0.003% of buy premium turnover |
| SEBI | INR 10 per crore of premium turnover, each side |
| IPFT | 0.0000001% of premium turnover, each side |
| GST | 18% of brokerage + exchange + SEBI + IPFT |

Checked against [Angel's current charge schedule](https://www.angelone.in/exchange-transaction-charges) and [NSE's STT schedule](https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax). Use this profile for the prospective trial, not data before the applicable rates. Estimates use per-order paise rounding; broker aggregation/rounding can differ. They exclude exercise/assignment, funding, subscriptions and other account-level charges. The profile is immutable; future tariff changes require a new profile/version.

For INR 10,000 premium turnover at entry and exit, the model estimates INR 28.10 entry charges and INR 42.80 exit charges, or INR 70.90 total, before spread and slippage. This is an illustrative calculation, not a real contract note.

User example checked at the approximate INR 214 premium for NIFTY 22,800 CE, assuming 65 units per lot and a single executed order on each side:

| Quantity | Premium turnover per side | Buy charges | Sell charges at the same premium | Round-trip charges |
| --- | ---: | ---: | ---: | ---: |
| 1 lot / 65 units | INR 13,910 | INR 29.86 | INR 50.31 | INR 80.17 |
| 10 lots / 650 units | INR 139,100 | INR 86.25 | INR 290.73 | INR 376.98 |

These buy estimates are within one paisa of the user's INR 29.87 and INR 86.26 examples; the premium was approximate and broker rounding may differ. No flat per-lot surcharge or equal buy/sell assumption is used. Brokerage remains INR 20 per executed order, while turnover-based charges scale with actual premium and quantity. Exit charges use the actual simulated exit price; the table assumes INR 214 on both sides and excludes spread/slippage. The bot uses the selected contract's quantity, not a hardcoded 65-unit lot size. These examples validate the prepared profile; they do not activate it on the existing server.

`legacy` preserves the old INR 20 plus five-basis-point charge estimate. It does not separately represent every tax. Old policies without a fee-model field continue to use it; changing fee models on their database is intentionally rejected.

## Slippage versus latency

The existing paper fill uses ask plus 5 bps on entry, bid minus 5 bps on exit (configurable). The spread is therefore also paid. This is an assumed cost, not measured live slippage. The update measures API/collection duration but does not simulate execution delay, order queue priority, depth depletion or partial fills. No claim of realistic live execution should be made from these results alone.

## Activation after stopping the old server, for a new test session

Confirm there is no open position and do not use a fresh database to bypass a same-day risk limit. The example below starts a separate INR 25,000 paper experiment with no fixed cooldown and itemized charges; broker credentials and other limits still come from `.env`:

```bash
PAPER_FEE_MODEL=angel_nse_2026_10 COOLDOWN_MINUTES=0 BOT_DB_PATH=./data/angel-orb-25k-costs-v2.sqlite3 python3 -m app.server
```

Existing historical data remains in the old database. Historical context is fetched again for the new experiment when enabled. Save a stable chosen configuration for the three-month trial; do not switch rules after each loss. The desktop startup automation now validates this database with the active 30-minute cooldown; it does not select the optional zero-cooldown override.

For a read-only report, supply the database for the experiment being evaluated:

```bash
python3 -m app.session_report data/angel-orb-25k-costs-v2.sqlite3 --output data/session-report.json
```

No unattended cloud deployment, notification channel, complete market-calendar support, execution-delay simulator or live-order adapter is included in this update. See `CLOUD_READINESS.md` for the remaining work.

## Trade count and broker choice

One or zero trades on a valid session is normal for a selective strategy. Record why it waited and separate no-signal days from data-outage days. One hundred closed trades is a review target, not a mandatory daily quota or proof. Extend the trial if necessary; do not weaken entry rules or count several simulations of the same event as independent forward trades.

Do not assume a 15-minute expiry: [Angel's authentication documentation](https://smartapi.angelbroking.com/docs/Historical) describes sessions lasting until midnight. Today's recorded error events are not uniformly 15 minutes apart, though logs alone do not establish the root cause. New endpoint telemetry is intended to identify it.

**Telemetry answer (27 October 2026).** The 16–27 October `market_requests` records of `angel-orb-25k-costs-v2` cover 7,556 requests over four sessions. They show no session expiry at all: no AG8001–AG8003 or AB1010 codes.

- **Candle throttle.** 702 of the 775 failures were `getCandleData` returning an HTML HTTP 403, Angel's gateway throttle ("access denied because of exceeding access rate"). About 55% of candle requests failed this way, at roughly two candle requests per minute. The rate barely varied with the second of the minute or with request bursts, and each block cleared by itself on a following poll, usually within 20–60 seconds. Angel's forum has many reports of the same throttle at trivial request rates.
- **Restarts didn't help.** The first candle request after each server start was also throttled on all three days with a restart (21, 26 and 27 October).
- **Other failures.** 45 quote and 3 Greeks requests failed with connection errors, which have been retried since 16 October; there were no halts.
- **The 15 October halts** that motivated restarts were connection failures that halted the engine before that retry logic existed.

A periodic restart would therefore not restore data. It would interrupt held-position monitoring, re-download the instrument master and log in each time. The adapter now retries the 403 throttle on any market-data endpoint, and re-logs in on AB1010 at most once per 5 minutes. Angel allows one session per API key, so a second program using the same key would otherwise log this one out repeatedly; a repeated loss halts with that explanation.

For operators who still want a scheduled refresh, `ANGEL_SESSION_REFRESH_MINUTES` (off by default; 5–1440) re-logs in once the session reaches that age. It runs inside the process, so position monitoring has no gap. A failed network refresh keeps the current session and retries after a minute. A scheduled refresh does not count toward the lost-session guard. Each refresh logs out any other program using the same API key.

[Upstox's Analytics Token](https://upstox.com/developer/api-documentation/analytics-token/) is a one-year read-only option supporting historical data, market quotes and option chains. It cannot place real orders. It is worth evaluating on the same sessions, but token lifetime is not evidence of better feed uptime. Keep provider-specific datasets separate, measure completeness/errors/latency first, and avoid mixing brokers' prices into an open trade.

## Fault fixes — 27 October 2026

Each fix has a regression test in `tests/test_fault_regressions.py`. Each behavioural test was confirmed to fail with its fix reverted. The exception is the dead-code removal, whose test pins unchanged behaviour.

- **Demo mode** refuses a database holding paper or live history. Previously it emptied every table before the mode check.
- **Late broker candles:** candles are refetched within the minute until the last completed candle is published. Previously a few seconds' delay could make ORB miss a confirmed breakout entirely.
- **Kill switch and dashboard:** Pause and the kill switch need only the database lock, and the dashboard reads without the engine lock, so neither waits for a broker request in progress. The engine re-reads status after each fetch, so a halt during a fetch still blocks that cycle's entry.
- **Replay** uses the paper loop's supervision. An engine error halts that scenario and appears in `halted_by_error`; the report no longer aborts.
- **Stale quotes:** stale exchange timestamps (Angel and Upstox) and an empty set of fresh option quotes are retryable. Paper mode retries, and live mode still halts. A timestamp ahead of the local clock, or a missing one, still halts.
- **Decision audit:** a decision is written to `decisions` once per snapshot. Unchanged closed-market polls no longer add a row every 15 seconds.
- **Live resume:** a halt can be cleared only by a later process. Start then reconciles broker F&O positions with the local record and clears `broker_uncertain` only when they match. Previously a live halt could never be resumed.
- `prepare_plan` takes the server's database and account locks.
- `backtest` opens the analysed database read-only.
- **Config** rejects `MAX_TRADES_PER_DAY` below 1, negative `MIN_OPTION_OI` or `MIN_DAYS_TO_EXPIRY`, a non-positive or non-finite `BROKER_ORDER_TICK`, and ports outside 1–65535.
- **Dead code:** the held-option branch in `Store.add_snapshot` was removed. Every observed option was already stored.
- The control token is compared in constant time.

**Not changed:** storage growth from raw broker responses in `market_requests` and the unpruned daily backups. A retention policy is still to be chosen.
