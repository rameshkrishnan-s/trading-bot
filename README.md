# Meridian Options Desk

Oracle Cloud preparation and Telegram setup: [deployment and commissioning guide](./ORACLE_DEPLOYMENT.md). The latest prepared update adds transactional paper trades, account locks, verified backups, 2026 session holidays and independent monitoring. Activated locally on 15 October 2026 with explicit restart approval. The paper server now uses `data/angel-orb-25k-costs-v2.sqlite3` and itemized charges; the old experiment is preserved. Live-session verification and cloud commissioning remain pending.

Prepared reliability update: see [connection recovery, persistent candles, audit records and itemized charges](./RELIABILITY_AND_COSTS.md). The running process is not automatically upgraded. Existing databases retain their legacy charge model; the new Angel charge profile is selected explicitly for a separate paper experiment.

A local dashboard and autonomous, rules-based **NIFTY 50 long-options** engine for the Indian market. It runs without external packages on Python 3.12+. The dashboard shows the market tape, decisions, selected contracts, fills, open positions, estimated P&L, risk budget, and an audit trail.

The example configuration defaults to a clearly labeled synthetic demonstration. The strategy is a research hypothesis, **not a validated source of profit**. Angel One authentication and market-data access were verified on 14 October 2026 after market hours; fresh intraday snapshots and a complete paper trade remain to be verified. No real order has been sent.

## Run the dashboard

```bash
# First-time setup only: preserve an existing .env with broker credentials.
test -f .env || cp .env.example .env
python3 -m app.server
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The demo advances one simulated market minute every two seconds, including simulated option quotes and fills. The **Start**, **Pause**, and **Kill switch** controls work; pausing blocks entries but continues monitoring an open position. The kill switch blocks entries and attempts to close an open position at the next fresh quote. Demo data is reset when the server restarts; demo mode refuses a database that holds paper or live history.

Run the tests:

```bash
python3 -m unittest discover -s tests -v
```

The overview separates server connectivity from market-feed status and keeps feed errors visible, including an explanation of Angel's `AB9019` Greeks failure. The session timeline shows regular IST entry/exit times. The trade-setup panel displays the observed opening range, buffered call/put boundaries and potential structural stops; these levels are not orders, and every entry still requires the strategy's confirmation and risk checks. Open positions show their actual stop, target, rupee threshold and time exit. The option chain includes expiry and lot size.

Unavailable session P&L and risk capacity show as `—` until the first session snapshot. Previous-day ranges are not reused as today's levels, and old snapshots are labeled. The dashboard does not verify the external startup schedule. Restart an existing server to load updated dashboard API fields, then refresh the page. Optional dashboard checks require Node.js: `node tests/dashboard.test.cjs`; the bot itself remains Python-only.

## Modes

| `BOT_MODE` | Prices | Orders | Purpose |
| --- | --- | --- | --- |
| `demo` | Deterministic synthetic feed | Synthetic fills | Test the engine and UI anytime |
| `paper` | Upstox or Angel One NIFTY, VIX and option quotes | Simulated buy at ask / sell at bid, plus costs and configurable slippage | Forward test on real quotes |
| `live` | Upstox live quotes | Protected limit IOC exchange orders | Personal account only, after validation and broker setup |

For `paper`, set `UPSTOX_ACCESS_TOKEN` in `.env` and use a separate `BOT_DB_PATH`, for example `./data/paper.sqlite3`. Upstox's [one-year, read-only Analytics Token](https://upstox.com/developer/api-documentation/analytics-token/) is a good fit for this mode because it can read option-chain and quote APIs but cannot place orders.

### Angel One paper trading

Set `BOT_MODE=paper`, `MARKET_DATA_PROVIDER=angel`, and `BOT_DB_PATH=./data/angel-paper.sqlite3`. Supply `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PIN`, and `ANGEL_TOTP_SECRET` locally. The adapter authenticates using TOTP, refreshes its session on a new day or an expired/replaced-session response (AG8001–AG8003, AB1010), and keeps the session token in memory. It re-logs in at most once per 5 minutes, because Angel allows one session per API key: if another program logs in with the same key, each login logs the other out. Give each program its own API key; a repeated loss halts with that explanation. Angel's intermittent HTTP 403 throttle on candles and quotes is retried; no periodic restart is needed.

Optional scheduled re-login: `ANGEL_SESSION_REFRESH_MINUTES=14` makes the running bot log in to Angel again once its session is 14 minutes old; `0`, the default, turns it off. Allowed values are 0 or 5–1440. It runs inside the process: no restart and no gap in position monitoring. If a refresh fails on the network, the bot keeps its current session and tries again a minute later. **Each refresh logs out any other program using the same Angel API key**, because Angel keeps only one session per key, so give other programs their own key before turning it on. The 16–27 October telemetry showed no session expiry, so this setting is optional, not a fix for the candle throttle. Its endpoint allowlist contains only login, FULL market quotes, option Greeks and historical candles; Angel live trading is rejected by configuration and by the engine.

The feed resolves NIFTY/VIX and current option contracts from Angel's instrument master, reads nearby option depth in batches of at most 50 contracts, and includes a held contract even when it moves away from the money. It spaces broker requests by at least 1.05 seconds, checks exchange quote timestamps, and blocks entries when usable Greeks are missing. It queries `api.ipify.org` once per process to supply Angel's required public-IP header; no broker secrets are sent to that service.

Verify access without starting the engine:

```bash
python3 -m app.check_feed
```

The check prints only connection status, contract counts and quote timestamps. Outside regular hours, it tests available quote/Greek endpoints but does not claim a fresh tradable snapshot. Start with `python3 -m app.server`; keep the machine awake and online during market hours. Stale quotes and connection failures are retried automatically. Other paper-data errors halt entries and require checking the dashboard and using Start after resolving the error. No alerts are currently sent.

Replay Angel paper data with `python3 -m app.backtest data/angel-paper.sqlite3 --output data/replay-report.json`. Keep separate databases for each provider. Option quote capture begins when the paper engine runs. ORB also fetches the current day’s NIFTY one-minute candles, including the opening range when started after 09:30; it never substitutes sampled quotes for missing broker candles.

For unattended **live** daily token renewal, you can instead set **all three** of `UPSTOX_API_KEY`, `UPSTOX_PIN` and `UPSTOX_TOTP_SECRET`. The official [Upstox TOTP endpoint](https://upstox.com/developer/api-documentation/get-token/) issues a fresh trading token using the app key, six-digit PIN and authenticator TOTP. This optional mode generates a TOTP locally and keeps the returned token only in process memory. The API key, PIN and seed are never sent to the dashboard or written to SQLite. Supplying the TOTP seed to a process gives it powerful account access; use a private machine and a secret manager or an owner-only `.env` file.

For `live`, also set `LIVE_TRADING_ENABLED=true`, `LIVE_STATIC_IP_APPROVED=true`, a strong `BOT_CONTROL_TOKEN`, and a distinct database path. Live mode refuses to bind to a public network interface. A live halt can be cleared only after a process restart; Start then requires broker F&O positions to match the local record. Do not put the dashboard behind a public proxy without proper authentication and transport security.

Upstox's access token expires at 3:30 AM IST the next day. With optional TOTP credentials, the engine obtains a fresh token before the next trading session; with a manually supplied token, that daily renewal is your responsibility. No design can promise permanent zero human attention: invalid credentials, outages, uncertain order states and broker/local position mismatches halt trading for review. The engine does not invent fills or automatically retry an order with an unknown outcome. In paper mode, Upstox HTTP 429/5xx, timeouts and connection failures on read-only market-data requests (quotes, option chain/contracts, intraday candles) are retried with bounded backoff, as with Angel; order, order-status and portfolio requests are never retried, and live mode still halts on any feed error. A broker-approved static egress IP is required for order requests. See the [research notes](./RESEARCH.md).

## Opening-range breakout adaptation

The active local configuration is **paper mode, ₹25,000**, using `data/angel-orb-25k-premium12500-risk2pct-daily4pct.sqlite3` from 15 October 2026. The previous `data/angel-orb-25k-premium12500.sqlite3` is preserved with its ₹400/₹800 risk thresholds; it had no trades or market snapshots when the new database was created. The original `data/angel-orb-25k.sqlite3` is preserved with its ₹10,000 premium policy; it contained no trades or market snapshots when the new database was created. Saved historical context was carried over. The earlier `data/angel-orb-20k.sqlite3` and `data/angel-paper.sqlite3` are preserved; both had zero positions, orders and captured snapshots when checked before this change. The premium cap is ₹12,500; the per-trade risk threshold is ₹500 (2% of starting capital) and the daily loss threshold is ₹1,000 (4% of starting capital). These rupee limits do not automatically grow with equity; the entry budget is additionally capped at 2% of current equity and remaining daily capacity. Restart the server to apply configuration changes. Strategy, capital or risk changes require a separate `BOT_DB_PATH` because each database records a fixed policy for its trading history.

`STRATEGY=orb` is the new default. It adapts the [public opening-range breakout approach](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/p1) to long NIFTY options. It is a new research hypothesis: the source's US-equity backtest does **not** validate this implementation or these parameters.

1. Build the 09:15–09:30 IST range from **all 15 completed broker one-minute candles**. Require 16 consecutive recent completed candles for the ATR and confirmation checks. Missing, stale or malformed candles block entries; current and future candles are excluded.
2. A call requires a fresh crossing followed by **two consecutive closes above range high + buffer**. Puts are symmetric below range low. The buffer is the larger of 2 index points and 10% of the 14-period one-minute ATR (simple average true range). Earliest entry is 09:32.
3. Reject a range narrower than 0.05% or wider than 1.2% of spot, India VIX outside 9–28, a move back inside the breakout boundary, or a quote more than half an ATR (minimum 2 points) past the confirmation close. These are initial parameters, not optimized or proven thresholds. There is no index-volume confirmation.
4. Select one whole option lot, 0.35–0.65 absolute delta, at least two calendar days to expiry, nonzero option volume, at least 10,000 OI and at most 2% bid/ask spread. Lot size comes from the broker. Premium is capped at **₹12,500 or 50% of current equity**, whichever is lower; cash must also cover the entry fee.
5. Place the index stop just inside the broken edge: range high minus buffer for calls, range low plus buffer for puts. The index target is **2× the entry-to-stop distance**. Option P&L is not necessarily 2R because delta, volatility and time value change.
6. Estimate one-lot risk using stop distance × absolute delta × 1.25, plus spread, slippage and estimated fees. It must fit **₹500, 2% of current equity, and remaining daily loss capacity**, whichever is smallest. Otherwise **skip the trade**; never tighten the structural stop to force an affordable lot. This estimate is not a guarantee of maximum loss.
7. While holding, check every market poll for the structural stop, net rupee loss threshold (at most ₹500, including estimated exit friction), target, daily loss limit or kill switch. Attempt exit at 15:15 IST. Stops are local software checks, not broker-hosted stop orders, and can be exceeded by gaps, polling latency, illiquidity or outages. With Angel or Upstox, a held position is polled every `HELD_POLL_SECONDS` (default 3, range 2–15) using one combined spot + held-option quote request (no option chain, Greeks or VIX); entry polling stays at 15 seconds. Retries after a feed error while holding wait at least 5 seconds.
8. At most one position, two trades/day, a configurable cooldown after exit (`COOLDOWN_MINUTES`, default 30), and no new entries from 14:45. Set the cooldown to 0 to consider the next fresh confirmed breakout without a fixed wait. The same breakout cannot be traded twice. **₹1,000 daily loss lock** stops entries and attempts to exit an open position. A daily lock can clear next session; manual halts stay halted.

### Prepared paper test without a fixed cooldown

The current `.env` and running process retain the original 30-minute cooldown. The following command selects a separate paper-test database and disables only the cooldown. Use it for a subsequent test session after stopping the existing server, with no open position; do not switch to a fresh database mid-session to reset the daily trade/loss limits. This starts a separate ₹25,000 paper account, keeping the previous experiment intact. Other settings and credentials still come from `.env`.

```bash
COOLDOWN_MINUTES=0 BOT_DB_PATH=./data/angel-orb-25k-no-cooldown.sqlite3 python3 -m app.server
```

No-cooldown entries still require the normal entry hours, complete candles, a fresh two-close breakout, option selection and risk checks. This is not a promise of improved performance. Existing databases without a recorded cooldown retain their historical 30-minute setting when replayed. Changing the cooldown on a database with another recorded policy is rejected.

The former EMA 8/21 plus momentum strategy remains available as `STRATEGY=ema` for comparison with its original 25% premium stop/40% target. Use a separate database for another strategy, capital or risk policy. The ORB version is paper/demo only; live mode rejects it. Paper fees remain simplified estimates.

### Advisory agent

The **Plan from previous sessions** panel adds historical context without changing entry rules. In Angel paper mode with `REVIEWER_ENABLED=true`, a running, flat engine fetches the preceding ten calendar days of NIFTY one-minute candles once per planning date, between 08:00 and 14:45 IST on weekdays. It stores valid prior-session candles in `context_candles` and summarizes up to five observed sessions. Today's candles are excluded. Missing minutes, conflicting duplicate candles and calendar gaps are not filled in or treated as complete history. Full regular-session coverage is 375 minutes (09:15 through 15:29); special sessions may differ. The last one-minute close is not represented as an official settlement price.

The existing AI provider produces a dated **Context / Bullish scenario / Bearish scenario / Wait scenario / Unknowns** plan, with source dates, conditional levels and invalidation conditions. This is a price-history plan: it has no news, overnight/global-market feed, futures volume or directional probability model. The dashboard labels partial coverage and old plans. Intraday reviews also receive the dated historical summary when it belongs to the current planning date. AI text cannot authorize entries, veto trades, change risk, or move the earliest ORB entry before 09:32.

History is fetched only while flat, so held-position exits do not wait on this extra request. Model generation runs on the existing advisory worker. History failures retry after 15 minutes; failed AI plans respect the reviewer interval and share the existing 24-attempt daily limit. Completed plans persist across server restarts. Demo, live and replay do not fetch historical planning data or generate these plans.

Prepare the plan manually before starting the server (it refuses to run while a server owns the database):

```bash
python3 -m app.prepare_plan
```

This command fetches and stores history and generates the advisory plan, but never starts the trading engine. The existing 09:15 startup schedule also prepares it when the server starts; to have it before 09:15, run this command earlier or keep the server running from 08:00. No additional schedule was created for planning.

Verification on 15 October: the historical fetch succeeded and the latest five observed sessions were summarized. The 14 October session contained 361 of 375 expected one-minute candles, so its extrema are explicitly marked partial. Two actual Antigravity plan attempts timed out at the existing process deadline; no generated plan was saved. Historical collection, caching, advisory isolation and UI states pass automated tests; successful end-to-end AI plan generation remains unverified.

The dashboard's **Agent notes** panel now uses the existing **Antigravity `agy` CLI session**, selected with `REVIEWER_PROVIDER=antigravity`. No Gemini API key is required for that route. The other Trading Bot project uses `agy --print` in its `agent_approves_spike` function; its separate AI sentiment strategy uses a direct Gemini key. The earlier direct-API 403 did not test Antigravity authentication.

As requested, the reviewer explains only. Its returned text is never consumed by the signal or risk engine. It receives a small snapshot of prices, candles and decision facts. Calls run in a temporary directory, in `plan` mode with `--sandbox` and slash-command expansion disabled. The prompt prohibits tool use, browsing, file access and trading. Child-process environment variables exclude broker and model API credentials; the CLI uses its own existing sign-in. No permission-bypass flag is used. Reviews run on a separate worker so model latency does not delay exits.

The current settings use a 15-minute minimum interval and a 24-attempt/session cap. Each CLI call has a 45-second internal deadline and a 55-second process deadline; a timed-out process group is terminated. Only a successful JSON response with nonempty review text is accepted. If the session expires, sign in through Antigravity normally and restart this server. `ANTIGRAVITY_COMMAND` can point to the installed `agy` binary. Optional `REVIEWER_PROVIDER=gemini` still uses a local `GEMINI_API_KEY` and `REVIEWER_MODEL`.

The panel shows the review's source timestamp, signal and provider/session label, including when a note is older than the current decision. Reviews and their inputs are saved locally in SQLite's `reviews` table. CLI failures leave trading rules unchanged. Model responses may be wrong and do not establish whether the strategy has an edge.

### Verification update (15 October 2026)

- Capital is now ₹25,000; offline initialization confirms the dashboard capital with the existing ₹400/₹800 risk thresholds. All 40 automated tests pass.
- A read-only pre-market check authenticated with Angel and received index and option quotes, but the option Greeks request was rejected with `AB9019`. A fresh market-hours snapshot remains unverified. Re-run `python3 -m app.check_feed` after 09:15 IST; resolve any remaining feed errors before relying on paper execution.
- Neither earlier Angel paper database contained trades or captured snapshots when inspected. There is no recorded performance evidence yet. The server was not started as part of this configuration update.

### Earlier verification status (14 October 2026)

- Antigravity connection check and an actual after-hours advisory review succeeded. The review explicitly identifies missing candles/quotes; it is not a live breakout assessment.
- 40 automated tests pass, including completed-candle confirmation, missing data, false breaks, whole-lot affordability, structural and rupee stops, replay, restart persistence and reviewer isolation.
- Angel historical endpoint returned 370 valid completed candles for the day, including all 15 opening-range candles. This was an after-hours check.
- Local dashboard displays ₹20,000, ₹400 trade risk, ₹800 daily risk, ORB fields and provider-specific reviewer state.
- A full forward paper trade during market hours and profitable out-of-sample results remain unverified. Software tests validate behavior, not profitability.

## Noise-area intraday momentum (`STRATEGY=noise`)

Added 27 October 2026 as a second research hypothesis, paper/demo only. It adapts [Zarattini, Aziz & Barbon, *Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)*](https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy) (Swiss Finance Institute WP 24-97, 2024). On SPY from 2007 to 2024 the authors report a Sharpe ratio of 1.24 at constant size (1.33 with volatility targeting) after commissions and slippage. Those are the authors' own SPY share results; they are not independently verified and say nothing about NIFTY options. Nothing in this repository shows the strategy works on NIFTY.

Rules as implemented:

1. **Noise area.** For each minute, σ is the mean absolute move from the 09:15 open to that minute's close over the previous **14 sessions**. The upper band is `max(today's open, prior close) × (1 + σ)` and the lower band is `min(today's open, prior close) × (1 − σ)`. The prior close is the previous session's 15:29 candle close, and the volatility multiplier is 1, as in the source.
2. **Half-hour checks only.** Entries are evaluated at 09:45, 10:15 … 14:15 IST, using the completed candle just before each check. A close above the upper band buys a call, and a close below the lower band buys a put. A check can still be taken up to 5 minutes late if the broker publishes the candle late. Entry also requires the quote to still be outside the band.
3. **Exit.** The same-side band is a trailing stop. It is checked only at the next half-hour checks (10:15 … 14:45), not between them, as in the source. The rupee loss stop, daily loss lock, kill switch and 15:15 time exit still apply on every poll. There is no index target.
4. **Contract and risk.** Contract selection, the premium cap, delta, spread, OI and expiry filters, the trade limits and the cooldown are unchanged. The rupee loss stop, checked on every poll, bounds a noise trade's loss; no index level does. So sizing requires only that spread, slippage and charges fit the remaining budget, and the dashboard's estimated stop loss is that rupee threshold. As in the source, a check is traded however far outside the band it closes. Sizing to the band distance was tried and rejected: it kept only entries where the move had faded back toward the band.

Deviations from the source:

- **No VWAP stop.** The NIFTY index has no traded volume; VWAP would need futures data.
- **One option lot instead of shares.** One NIFTY lot is roughly 30 times ₹25,000 in delta-equivalent exposure (0.5 delta × 65 units × spot). With a ₹500 per-trade budget, the rupee stop usually fires after roughly 10–20 adverse index points, depending on delta. That is long before a half-hour band check would, so most losing trades will exit on the rupee stop, not the band.
- **No reversal on the opposite band.** The bot's one-position limit, two-trade limit and cooldown replace the source's immediate reversal.
- **No volatility targeting.**

**Prior sessions.** A flat engine requests 28 calendar days of NIFTY 1-minute candles once per session: Angel `getCandleData`, or Upstox V3 historical candles ending the previous day. They are stored in `history_candles`, and a failed request retries after 15 minutes. Entries wait until 14 sessions, including the previous trading day and its 15:29 close, are available. The monitor reports `history_missing` if that blocks entries during the session.

Try it in the demo:

```bash
BOT_MODE=demo STRATEGY=noise BOT_DB_PATH=./data/demo-noise.sqlite3 python3 -m app.server
```

For paper trading, stop the existing server first, while it is flat, and use a new database. Policy changes are rejected on an existing one.

```bash
STRATEGY=noise BOT_DB_PATH=./data/angel-noise-25k.sqlite3 python3 -m app.server
```

Compare it with ORB on the same recorded quotes using a what-if replay:

```bash
python3 -m app.backtest data/angel-orb-25k-costs-v2.sqlite3 --strategy noise
```

The replay uses saved candles from strictly earlier days as history. Databases recorded so far contain only a few sessions, so it will report no noise trades until at least 14 sessions of candles exist.

## Capture and replay real data

`paper` mode now stores near-money option quotes at each poll’s actual timestamp, along with the completed candles available to that decision. It records a non-secret policy snapshot so replay uses the same capital, risk and strategy settings. Older minute-only captures retain their limited execution resolution. After collecting real sessions, replay the same decision engine under three execution-friction assumptions:

```bash
python3 -m app.backtest data/angel-orb-25k.sqlite3 --output data/replay-report.json
```

The report shows trade count, win rate, estimated equity, max drawdown and profit factor with **0, 10 and 25 additional basis points of slippage on each side**. It marks samples below 20 trading days and 4,000 minute snapshots as insufficient. This threshold is an initial floor, not proof of an edge. Review out-of-sample months, expiry regimes, costs, rejected fills and forward paper results before considering live use. The replay tool opens the database read-only, rejects synthetic demo data and disables model calls. A scenario that hits an engine error is halted, as in paper trading, and reported in `halted_by_error` instead of aborting the report. Additional slippage is applied on top of the recorded base slippage. Candle fetches are skipped while holding to prioritize exits; the report counts snapshots without candles. If a slippage scenario would be flat while the original run held a position, missing candles block alternative entries. Replay cannot reconstruct those opportunities, missed intra-poll moves or real queue fills; broad history and a sufficiently large trade sample are still required.

## Files

- [Engine](./app/engine.py): session state, execution lifecycle, kill switch, P&L and audit trail.
- [Broker adapter](./app/market.py): synthetic feed and Upstox REST adapter.
- [Angel paper data](./app/angel.py): Angel One authentication, contract master, quote depth and Greeks.
- [Strategy](./app/strategy.py): signals, contract selection and sizing checks.
- [Prior sessions](./app/history.py): validated earlier-session candles for the noise-area strategy.
- [Replay](./app/backtest.py): scenario testing from captured real option-chain quotes.
- [Dashboard](./static/index.html): responsive tracking UI, with [styles](./static/app.css) and [interactions](./static/app.js).
- [Research](./RESEARCH.md): sources, product decisions and remaining validation work.

## Deployment notes

Use a machine whose outbound static IP is registered with Upstox before configuring live mode. Keep the dashboard on `127.0.0.1`; use an authenticated tunnel if remote viewing is needed. Store `.env` outside backups or give it owner-only permissions (`chmod 600 .env`). The code is a personal local system, with no multi-user accounts, alert delivery or redundant execution host. Angel credentials are configured for paper market data only; real broker order execution remains untested.
