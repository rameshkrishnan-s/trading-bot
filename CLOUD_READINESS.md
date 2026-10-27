# Cloud paper-trading readiness and three-month evaluation

Reviewed 15 October 2026. This is a proposed work list, not a deployment or approval to use real money. Existing runtime settings and the running server were not changed by this review.

## Implementation checkpoint — 15 October 2026, afternoon

See `ORACLE_DEPLOYMENT.md` for the prepared Oracle/Telegram deployment. The implementation below passed 86 Python tests and the dashboard checks; systemd verified all seven service/timer files. A backup of the real legacy experiment was integrity-checked and restored into a disposable test database. The user subsequently explicitly approved restarting the paper bot. On 15 October 2026, the old flat server stopped gracefully and the updated local server started with `data/angel-orb-25k-costs-v2.sqlite3`, itemized fees and the unchanged 30-minute cooldown. Old history and the environment were backed up. The morning desktop automation now validates the new profile. Startup/liveness checks passed after close; fresh market-feed verification remains pending.

Implemented since the original checklist: atomic paper entry/exit and signal consumption, daily rollover/locks and control updates; same-host account/database locks before initialization; held-position quote collection without entry Greeks, VIX or unrelated contracts; continued quote/candle collection during transient entry-Greeks failure; regular NSE 2026 holiday gating with unknown years blocked; lightweight engine health endpoint; read-only preflight and release hash; owner-only verified SQLite backups and non-overwriting restore; independent local Telegram health monitor with deduplication and daily summaries; closed-trade expectancy/drawdown/streak reports; loopback Host/Origin protection; protected credential representations; and systemd supervision, monitor/report/backup timers.

Prepared is not commissioned. Still required: actual next-session feed/recovery verification; Oracle VM and SSH access; Telegram token/chat ID configured privately and delivery tested; cloud installation/reboot checks; retirement of the desktop scheduler at cloud cutover; independent off-VM uptime monitoring; encrypted off-host backups and retention/restore drills; official 2027 calendar update; broader crash/auth/disk-full/session-rollover drills; endpoint-specific resilience and complete audit coverage; realistic execution-delay/liquidity/partial-fill simulation; and live-execution work if later authorized. The original unchecked items below describe the broader acceptance criteria, some now partially implemented.

Subsequent prepared update: `RELIABILITY_AND_COSTS.md` documents implemented transient Angel recovery, persistent candles, health fields, request/decision recording, itemized fee estimates and reports. These changes passed 70 Python tests plus dashboard checks; they have not yet been deployed to the running process. The remaining cloud supervision, alerts, calendar, backup/restore, atomic trade lifecycle and realistic execution-delay work below is still outstanding.

## Current baseline

- Angel One market data; paper ORB; INR 25,000 starting capital; INR 12,500 premium cap; INR 500 per-trade cap; INR 1,000 daily loss threshold; one position and two trades per day.
- Trade risk is additionally limited to 2% of current equity and remaining daily capacity. The daily INR 1,000 cap is fixed, not dynamically 4% of equity.
- Completed candles, fresh quotes, contract selection, costs, snapshots, daily limits, duplicate-breakout protection and replay already exist.
- A configurable cooldown is implemented; the current local configuration still defaults to 30 minutes. Zero cooldown is a separate proposed paper experiment.
- The last automated run passed 57 Python tests. That establishes tested behaviors, not a profitable strategy or unattended reliability.
- Angel has no order endpoints in its adapter; ORB live mode is intentionally rejected. Live trading requires additional development and validation.

## P0 — complete before an unattended cloud trial

- [ ] Replace the catch-all persistent halt for transient feed failures with explicit states for reconnecting, data unavailable, manual halt, daily loss lock and uncertain broker state. Retry read-only market requests with bounded backoff and jitter. Resume entries only after all required data is valid; preserve manual halts and uncertainty locks.
- [ ] Keep feed collection and held-position monitoring alive when entries are blocked. Missing entry Greeks, advisory failures and unrelated chain requests must not delay management of the held contract.
- [ ] Preserve valid completed candles by session/minute, track coverage, and backfill gaps. Never synthesize missing candles or treat cached candles as current without freshness/completeness checks. Record provider revisions/conflicts.
- [ ] Show separate quote, candle, Greek and execution health. Persist the cause of a halt until a real recovery. Do not turn an old snapshot green merely because a halted loop returned successfully. Display the latest failed endpoint and next retry time without credentials.
- [ ] Introduce endpoint-aware rate limiting, bounded request timeouts and authenticated-session recovery. Measure each request's latency, status and age. Avoid repeated full instrument-master/IP-discovery requests when validated cached data can be used.
- [ ] Use an NSE session calendar, including holidays and special sessions, plus explicit IST boundaries and synchronized clocks. Verify whether a late start has sufficient backfilled data before allowing entries.
- [ ] Deploy a single dedicated engine process under a service supervisor on one always-on Linux VM. Auto-start on reboot and recover crashes with limits; retain strategy pause/halt state. Acquire a process/account lock before database writes or engine initialization.
- [ ] Run premarket checks around 08:50–09:00 IST: configuration, credentials, calendar, instruments, storage, clock, account/session state and data access. Require actual market data after 09:15. Verify exits and final session state after the configured exit time; do not blindly terminate a process managing a position.
- [ ] Replace the local Codex heartbeat as the operational scheduler. It depends on the desktop and still references an obsolete database. Retire it after cloud cutover so local and cloud engines do not run competing sessions.
- [ ] Store SQLite on persistent local disk, outside disposable releases. Use consistent SQLite backups, encrypted off-host copies, retention and tested restores. Add disk-space/database-integrity alerts. SQLite is sufficient for a single-engine trial; multiple writers or replicas would require a different design.
- [ ] Separate dashboard serving from the execution loop and its network calls. Use a production web server or keep the dashboard private through a secure tunnel. Do not expose the current standard-library HTTP server directly to the internet.
- [ ] Require authentication and authorization for both dashboard data and control actions, HTTPS for remote access, protected sessions/CSRF handling as applicable, a firewall and an audit trail of Start/Pause/Kill actions.
- [ ] Store broker credentials and TOTP secrets in a restricted secret store or service environment, with redaction and rotation procedures. Remove secret fields from object representations. Document which login steps the broker permits unattended.
- [ ] Add external monitoring independent of the trading process. Alert on missing heartbeat, unrecovered feed gaps, manual-action halts, failed exits, position/state mismatches and failed backups. Add a daily session summary and a clear escalation procedure; choose the notification channel before enabling delivery.
- [ ] Provide repeatable installation, a pinned tested Python runtime, service definitions, environment validation, deployment instructions and rollback. Deploy outside active trading or use an explicit position-aware maintenance procedure.
- [ ] Make each position/order/state update atomic. Test crash recovery around entry, exit, daily rollover and shutdown. A timeout must not leave a partially recorded simulated trade or reset daily limits on restart.

## P1 — make the experiment measurable

- [ ] Establish real source control, release identifiers, strategy/configuration hashes, migration versions and a deployment audit trail. Do not mix different configurations into an unlabeled performance series.
- [ ] Choose the primary cooldown before the trial. Compare 0/15/30 minutes through separate replay/shadow portfolios receiving the same observations, rather than changing the primary rules after a loss or resetting its account.
- [ ] Store a structured audit record for every decision, not only changes in its reason. Include complete input candles, quote/Greek timestamps, selected and rejected contracts, rejection reasons, risk calculation, latency, configuration version and any data-quality flag.
- [ ] Preserve raw observations needed for repeatable replay, including held-option quotes and data during entry blocks. Log intended versus actual polling intervals and outages; do not invent fills during gaps.
- [ ] Replace simplified charges with a broker/instrument/date-aware cost model covering applicable brokerage, taxes and exchange charges. Add bid/ask quantity, latency, partial/no-fill and stressed-slippage scenarios. A paper fill at displayed bid/ask does not prove an executable live fill.
- [ ] Report net expectancy per trade, win rate, average win/loss, profit factor, drawdown, losing streaks, trade count, time in market, exposure and capital utilization. Report rejected trades and data completeness alongside P&L.
- [ ] Reconcile daily positions/orders/P&L and export machine-readable trade and data-quality reports. Keep exchange-source timestamps separate from local receipt and processing timestamps.
- [ ] Preserve an untouched forward period after any tuning. Report parameter variants transparently, sample uncertainty and concentration of profit in a few trades/days. Avoid selecting the best historical variant and calling that independent validation.
- [ ] Run failure drills: timeouts, HTTP 429/5xx, missing Greeks/candles, stale quotes, authentication expiry, disconnect/reconnect, restart with a position, disk full, shutdown during requests, duplicate startup and corrupted responses.
- [ ] Keep the AI plan optional and outside trading decisions. The current local Antigravity CLI depends on desktop/session setup and has timed out. Either disable it for cloud trading or run an independently monitored supported cloud integration with bounded cost/time; record any later AI trading filter as a new experiment.

## Proposed three-month forward trial

Begin the measured trial after the P0 checks and a commissioning period pass. If it began on 15 October 2026, the three-calendar-month review date would be 15 January 2027; a later readiness date shifts the review date.

1. Commissioning: demonstrate premarket startup, one complete session, stable candle recovery, controlled shutdown/restart, backup restore and actionable alerts. Existing captures are useful diagnostics but are not a substitute for commissioning.
2. Month 1: freeze the primary rules; run every eligible session and measure reliability and realistic costs. Record all manual interventions. Mark interrupted/data-incomplete sessions explicitly.
3. Month 2: continue the same primary rules and account. Run parameter comparisons separately on identical observations. Repeat outage/restart drills without silently contaminating the primary performance record.
4. Month 3: evaluate the untouched forward results, cost/slippage stress, losses and operational incidents. Require additional observation if there are too few trades or conditions are too narrow. A material strategy change starts a new evaluation period for that version.

An initial review target of roughly 60 trading sessions and 100 completed trades is a planning heuristic, not proof or a compulsory target. With at most two trades a day, 100 trades may take longer than three months. Never force trades to reach a sample count. Choose acceptable drawdown and operational criteria before seeing the results. Profit after realistic costs and tolerable drawdown are necessary evidence, not a guarantee of future returns.

## P2 — additional gate before real-money trading

- [ ] Choose and confirm the intended live broker. If Angel remains the choice, build a separate execution adapter and comprehensive tests; do not simply remove the existing live-mode rejection.
- [ ] Confirm current broker/exchange API eligibility, static outbound IP registration, authentication, order restrictions and any applicable algorithm registration with the broker. Recheck requirements at go-live.
- [ ] Persist an order intent before submission, use broker-supported identifiers, reconcile order status/fills/positions/cash on startup and continuously, and handle rejection, partial fill, cancellation and unknown outcomes. Never blindly retry an order submission after a timeout.
- [ ] Define position protection during network/server failure, including broker-hosted protection where supported and a manual broker-app exit procedure. Current software stops are polled and cannot guarantee INR 500/1,000 loss ceilings.
- [ ] Validate instrument/expiry/lot/tick mappings, actual charges, available funds, order types and account restrictions against broker responses. Test an expiry rollover and missing held-contract data.
- [ ] Perform available broker sandbox/integration validation, then explicitly authorize and supervise a small live pilot using the smallest eligible lot only if it fits the chosen capital/risk budget. Do not assume the paper results establish the quality of live fills.
- [ ] Require an explicit human go-live decision after both performance and operational criteria pass. Three months elapsed, a positive backtest, or a passing unit-test suite is insufficient on its own.

## Official references checked

- Python cautions that `http.server` is not recommended for production: https://docs.python.org/3/library/http.server.html
- SQLite online backup guidance: https://www.sqlite.org/backup.html
- Angel SmartAPI documentation and rate-limit reference: https://smartapi.angelbroking.com/docs and https://smartapi.angelone.in/docs/RateLimit
- Angel's published static-IP notice for API order execution: https://smartapi.angelone.in/exchange-regulations (search-index notice retrieved; direct page access was restricted during this review).
- NSE implementation standards: https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf — verify any subsequent amendments and broker-specific implementation before live execution.
