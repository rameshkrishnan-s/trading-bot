# Oracle Cloud paper deployment

Prepared 15 October 2026. No VM has been provisioned. These files are for one Linux VM and one paper engine; they do not enable live orders. Local tests use Python 3.14.4. Use Python 3.12 or newer on the VM and rerun the complete test suite there before enabling a service. No third-party Python dependencies are required.

## Architecture and access

Run the engine continuously under systemd. It waits outside the regular session, collects during the session and preserves manual pauses and daily locks. Do not schedule repeated restarts or a blind afternoon kill. The dashboard binds only to loopback. Reach it through authenticated SSH; do not open port 8765 in OCI or the host firewall. The account lock works across ports/checkouts on the same host/user, not across VMs. Stop the local engine and retire its desktop automation before starting the cloud engine.

Use a dedicated Ubuntu or Oracle Linux VM with Python 3.12+, persistent disk and synchronized time. Configure the OCI security list and host firewall to admit SSH only from your permitted addresses, with outbound HTTPS/DNS for broker and Telegram APIs. Do not assume a particular free-tier shape is available or free for your account. See [Oracle instance access](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/accessinginstance.htm) and [OCI security lists](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securitylists.htm).

Install the application source at `/opt/option-trading-bot` with root-owned, read-only code, and create a dedicated `optionbot` system user. Keep the database, reports and backups in `/var/lib/option-trading-bot`, owned by that user with mode 700. Copy only source/static/tests/deploy/docs; never bundle `.env`, `.git`, logs or databases in a public release. Record a code release hash from preflight; preserve the exact release for replay/rollback.

## Configuration and commissioning

1. Copy `deploy/oracle.env.example` (Angel) or `deploy/upstox.env.example` (Upstox, read-only Analytics Token, `PAPER_FEE_MODEL=upstox_nse_2026_10`, new database) to `/etc/option-trading-bot.env`, owned by `optionbot` with mode 600. Fill Angel credentials on the VM. Generate a unique `BOT_CONTROL_TOKEN` locally. Do not paste tokens into chats or shell command arguments. This template freezes the current 30-minute cooldown and INR 25,000/500/1,000 capital/risk policy; costs use the itemized Angel profile.
2. For continuity, transfer a verified SQLite backup of the **new cost-model experiment** while the local engine is stopped and flat, then restore it at the configured cloud database path. Preserve old legacy-cost history separately. Never switch to a fresh database to bypass an intraday loss/trade limit or manual halt.
3. Run `python3 -m unittest discover -s tests` on the VM. Run `BOT_ENV_FILE=/etc/option-trading-bot.env python3 -m app.preflight --cloud` as `optionbot` from the application directory. It validates settings, free space, calendar coverage and database policy/integrity without contacting the broker. It records a source hash and does not prove the feed works.
4. Copy `deploy/systemd/option-bot*` into `/etc/systemd/system/`. Run `sudo systemd-analyze verify /etc/systemd/system/option-bot*`, then `sudo systemctl daemon-reload`. Validate `timedatectl status` shows synchronized time. The timers explicitly use Asia/Kolkata.
5. With the local bot stopped, start only the cloud engine: `sudo systemctl start option-bot.service`. Inspect `journalctl -u option-bot.service` and `/api/health`. Verify a real full session, retained candles after a transient outage, safe restart, and the next session's startup before treating the deployment as commissioned.
6. Set up Telegram as below, create an initial backup with `sudo systemctl start option-bot-backup.service`, and restore that backup into a **new test file**. Only then enable services: `sudo systemctl enable option-bot.service` and `sudo systemctl enable --now option-bot-monitor.timer option-bot-backup.timer option-bot-report.timer`.

From your own computer, open an SSH tunnel using the VM alias you configured:

```bash
ssh -N -L 8765:127.0.0.1:8765 oracle-paper
```

Open `http://127.0.0.1:8765`. Stop any local server on that port before creating the tunnel. Enter the control token when the dashboard requests it. SSH provides private authenticated transport for dashboard data; Host/Origin checks and the control token guard browser control actions. This is not a public multi-user web deployment.

## Telegram

Create your bot through Telegram's verified **@BotFather**, open the bot's private chat and send `/start`. Configure `TELEGRAM_BOT_TOKEN` and your intended `TELEGRAM_CHAT_ID` in the protected environment file. Obtain the chat ID through the [official Bot API](https://core.telegram.org/bots/api#getupdates) locally; do not use third-party token-checking websites. The implementation uses [sendMessage](https://core.telegram.org/bots/api#sendmessage) and never logs the token-bearing URL or broker credentials.

The independent monitor checks every minute, sends only changed failures and recovery, and does not acknowledge a failed delivery. It reports unavailable/stalled engines, stale in-session data, missing entry candles/deltas, paused/halted state, overdue positions, low disk and missing backups. An initial healthy check is quiet. Use `python3 -m app.monitor --state /var/lib/option-trading-bot/monitor.json --dry-run` to inspect without sending. Test actual delivery during commissioning; currently no Telegram message has been sent.

The 15:45 IST timer saves a daily JSON report and sends trade count, net closed P&L, fees, snapshots and remaining positions. Zero observations are flagged as a data gap, not called a valid no-trade day. This monitor runs separately from the bot **on the same VM**: it cannot notify if the whole VM or network disappears. Independent external uptime monitoring remains required before unattended operation.

## Backups and recovery

The weekday 16:00 IST timer uses SQLite's online backup API, checks integrity, and publishes an owner-only file without overwriting existing history. It is safe to back up a running SQLite database. Restore into a new file:

```bash
python3 -m app.backup /path/to/backup.sqlite3 --destination /path/to/restore-test.sqlite3
```

Never replace the active database while its engine runs. A backup on the same disk is insufficient: encrypted off-host copies, a retention policy and recovery from VM loss still need commissioning. Automatic deletion is intentionally absent until that retention policy is chosen. Monitoring flags low disk and backups older than 96 hours.

## Current limits and release procedure

- The calendar includes official regular-session 2026 F&O holidays. Muhurat and other special sessions are intentionally disabled; this ORB has fixed regular-session hours. Unknown years block entries and alert. Add and verify the official **2027** calendar before January, or the three-month trial will stop at year-end. Review new exchange amendments. [NSE 2026 circular](https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf).
- Retryable missing entry Greeks now preserve quote/candle collection with zero deltas, which cannot pass contract selection. Held-position monitoring requests only NIFTY and the held option; VIX is shown unavailable during that path. Missing NIFTY/held quotes still block safe execution.
- Paper entry/exit, consumed-breakout markers, daily rollover and controls commit atomically. Crash-injection and restore tests pass. This does not make external live orders transactional; live execution is outside this deployment.
- Fixed slippage remains an assumption; latency/partial-fill/depth simulation is outstanding. Reports add net expectancy, win rate, profit factor, closed-trade drawdown and losing streaks; closed-trade drawdown is not full intratrade drawdown.
- Deploy when flat outside the session. Back up the database and environment privately, stop the service, install the tested release, run preflight and restart. Preserve pause/halt state. If validation fails, restore the previous code; never roll back database state over later recorded trades. Keep a deployment ledger with the hash, configuration and time.
- Do not begin the official three-month measurement until live-feed commissioning, Telegram delivery, external monitoring and off-host restore pass. No VM, external alert delivery or cloud service has yet been activated.
