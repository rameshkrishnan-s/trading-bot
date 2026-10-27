from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.config import Config
from app.market import Option, Snapshot
from app.costs import order_costs
from app.session_calendar import previous_trading_day


# Strategies that read completed broker candles and record an index-level plan at entry.
CANDLE_STRATEGIES = frozenset({"orb", "noise"})

# Noise-area intraday momentum (Zarattini, Aziz & Barbon, 2024), adapted to the 09:15 NSE open.
# Source parameters: 14-session lookback, volatility multiplier 1, decisions only every 30 minutes.
NOISE_LOOKBACK = 14
NOISE_MIN_OBSERVATIONS = 10
NOISE_MULTIPLIER = 1.0
NOISE_CHECKS = tuple(time(9 + (45 + 30 * i) // 60, (45 + 30 * i) % 60) for i in range(11))  # 09:45 … 14:45
NOISE_GRACE = timedelta(minutes=5)  # A late-published broker candle must not skip a check.


def ema(values: list[float], span: int) -> float:
    result = values[0]
    alpha = 2 / (span + 1)
    for value in values[1:]:
        result += alpha * (value - result)
    return result


def entry_window(ts: datetime) -> bool:
    return ts.weekday() < 5 and time(9, 25) <= ts.time() < time(14, 45)


def exit_window(ts: datetime) -> bool:
    return ts.time() >= time(15, 15)


def trend_signal(spots: list[float], vix: float) -> tuple[str, str, dict]:
    if len(spots) < 30:
        return "WAIT", f"Warming up: {len(spots)}/30 one-minute observations", {}
    if not 9 <= vix <= 28:
        return "WAIT", "India VIX is outside the configured 9–28 regime", {}
    fast = ema(spots[-30:], 8)
    slow = ema(spots[-30:], 21)
    gap_bps = (fast / slow - 1) * 10_000
    momentum_bps = (spots[-1] / spots[-4] - 1) * 10_000
    metrics = {"ema_fast": round(fast, 2), "ema_slow": round(slow, 2),
               "gap_bps": round(gap_bps, 1), "momentum_bps": round(momentum_bps, 1)}
    if gap_bps >= 2.2 and momentum_bps >= 2.5:
        return "CALL", "8-minute trend and recent momentum agree upward", metrics
    if gap_bps <= -2.2 and momentum_bps <= -2.5:
        return "PUT", "8-minute trend and recent momentum agree downward", metrics
    return "WAIT", "Trend and momentum do not agree", metrics


def select_option(snapshot: Snapshot, direction: str, config: Config, metrics: dict | None = None,
                  equity: float | None = None, risk_budget: float | None = None) -> tuple[Option | None, str]:
    equity = config.starting_capital if equity is None else equity
    risk_budget = config.max_loss_per_trade if risk_budget is None else risk_budget
    kind = "CE" if direction == "CALL" else "PE"
    candidates = []
    for option in snapshot.options:
        if option.option_type != kind or option.lot_size <= 0:
            continue
        try:
            days = (date.fromisoformat(option.expiry) - snapshot.ts.date()).days
        except ValueError:
            continue
        if days < config.min_days_to_expiry:
            continue
        if not (option.ask > option.bid > 0) or option.spread_pct > config.max_spread_pct:
            continue
        if option.oi < config.min_option_oi or option.volume <= 0:
            continue
        if not (0.35 <= abs(option.delta) <= 0.65):
            continue
        slippage = 0.002 if config.mode == "live" else config.paper_slippage_bps / 10_000
        notional = round(option.ask * (1 + slippage), 2) * option.lot_size
        if notional > min(config.max_premium, equity * .5) or notional + paper_fee(notional / option.lot_size, option.lot_size, config) > equity:
            continue
        # A 25% premium stop can slip. This is a sizing check, not a loss guarantee.
        planned_risk = notional * 0.25 + 2 * config.paper_fee_per_order
        if metrics and "stop_distance" in metrics:
            planned_risk = option_risk(option, snapshot, metrics, config)
        if planned_risk > risk_budget:
            continue
        candidates.append(option)
    if not candidates:
        return None, "No liquid whole lot fits delta, expiry, premium, available cash and remaining stop-risk budget"
    return min(candidates, key=lambda o: (abs(abs(o.delta) - 0.5), o.spread_pct)), "Contract passed liquidity and premium checks"


def paper_fee(price: float, qty: int, config: Config, side: str = "BUY") -> float:
    return order_costs(price, qty, config, side)["total"]


def breakout_signal(snapshot: Snapshot) -> tuple[str, str, dict]:
    """15-minute ORB, two closed-bar confirmations. Parameters are a paper hypothesis."""
    from datetime import timedelta
    ts = snapshot.ts
    start = ts.replace(hour=9, minute=15, second=0, microsecond=0)
    cutoff = ts.replace(second=0, microsecond=0)
    bars = {datetime.fromisoformat(b['ts']): b for b in snapshot.candles
            if b['ts'][:10] == ts.date().isoformat() and datetime.fromisoformat(b['ts']) < cutoff}
    opening = [bars.get(start + timedelta(minutes=i)) for i in range(15)]
    metrics = {"strategy": "orb", "range_bars": sum(b is not None for b in opening), "required_bars": 15}
    if snapshot.candle_error:
        return "WAIT", snapshot.candle_error, metrics
    if ts < start + timedelta(minutes=15) or any(b is None for b in opening):
        return "WAIT", f"Opening range incomplete: {metrics['range_bars']}/15 closed candles (09:15–09:30)", metrics
    high, low = max(b['high'] for b in opening), min(b['low'] for b in opening)
    width = high - low
    metrics.update(range_high=round(high, 2), range_low=round(low, 2), range_width=round(width, 2))
    # Require 16 consecutive closed candles, ending immediately before this snapshot.
    recent = [bars.get(cutoff - timedelta(minutes=i)) for i in range(16, 0, -1)]
    if any(b is None for b in recent):
        return "WAIT", "Waiting for 16 consecutive, fresh closed candles", metrics
    atr = sum(max(b['high'] - b['low'], abs(b['high'] - a['close']), abs(b['low'] - a['close']))
              for a, b in zip(recent[-15:-1], recent[-14:])) / 14
    buffer = max(2.0, atr * .1)
    metrics.update(atr=round(atr, 2), buffer=round(buffer, 2), confirmation="Waiting", candle_time=recent[-1]['ts'])
    if not 9 <= snapshot.vix <= 28:
        return "WAIT", "India VIX is outside the configured 9–28 regime", metrics
    if not .0005 <= width / snapshot.spot <= .012:
        return "WAIT", "Opening range outside 0.05%–1.2% width filter", metrics
    previous, first, second = recent[-3:]
    # Both confirmations must be after the range, with a fresh cross immediately before them.
    if datetime.fromisoformat(first['ts']) < start + timedelta(minutes=15):
        return "WAIT", "Waiting for two closes after the opening range", metrics
    direction = 0
    if previous['close'] <= high + buffer < min(first['close'], second['close']):
        direction = 1
    elif previous['close'] >= low - buffer > max(first['close'], second['close']):
        direction = -1
    if not direction:
        return "WAIT", "No fresh breakout confirmed by two consecutive closes", metrics
    edge = high if direction == 1 else low
    if direction * (snapshot.spot - edge) <= buffer:
        return "WAIT", "Breakout faded back to the range before entry", metrics
    if direction * (snapshot.spot - second['close']) > max(2, .5 * atr):
        return "WAIT", "Price has moved too far beyond confirmation; do not chase", metrics
    stop = edge - direction * buffer
    distance = direction * (snapshot.spot - stop)
    metrics.update(confirmation="2 closed candles", signal_id=f"{second['ts']}:{direction}",
                   stop_spot=round(stop, 2), target_spot=round(snapshot.spot + direction * 2 * distance, 2),
                   stop_distance=round(distance, 2))
    return ("CALL" if direction == 1 else "PUT"), "Opening-range breakout confirmed; structural stop inside the broken edge", metrics


def noise_sigma(history: list[dict], minute: str) -> float | None:
    """Average absolute move from the 09:15 open to the close of `minute` over prior sessions."""
    moves = [abs(s["closes"][minute] / s["open"] - 1) for s in history if minute in s["closes"]]
    return sum(moves) / len(moves) if len(moves) >= NOISE_MIN_OBSERVATIONS else None


def noise_signal(snapshot: Snapshot, history: list[dict]) -> tuple[str, str, dict]:
    """Enter when a half-hour check closes outside the time-of-day noise band around the open."""
    ts = snapshot.ts
    day = ts.date().isoformat()
    cutoff = ts.replace(second=0, microsecond=0)

    def at(check):
        return ts.replace(hour=check.hour, minute=check.minute, second=0, microsecond=0)

    metrics = {"strategy": "noise", "history_sessions": len(history), "required_sessions": NOISE_LOOKBACK,
               "multiplier": NOISE_MULTIPLIER}
    if snapshot.candle_error:
        return "WAIT", snapshot.candle_error, metrics
    if len(history) < NOISE_LOOKBACK:
        return "WAIT", f"Noise area needs {NOISE_LOOKBACK} prior sessions; {len(history)} available", metrics
    previous = history[-1]
    expected = previous_trading_day(ts.date())
    if expected and previous["date"] != expected.isoformat():
        return "WAIT", f"Prior-session history lacks {expected.isoformat()}", metrics
    if previous["last_close"] is None:
        return "WAIT", f"Prior session {previous['date']} has no 15:29 close", metrics
    candles = {b["ts"][11:16]: b for b in snapshot.candles
               if b["ts"][:10] == day and datetime.fromisoformat(b["ts"]) < cutoff}
    if "09:15" not in candles:
        return "WAIT", "Waiting for today's 09:15 opening candle", metrics
    session_open = candles["09:15"]["open"]
    # Gap adjustment from the source: the band starts from the farther of the open and prior close.
    upper_anchor = max(session_open, previous["last_close"])
    lower_anchor = min(session_open, previous["last_close"])
    metrics.update(session_open=session_open, previous_close=previous["last_close"])
    due = next((c for c in NOISE_CHECKS[:-1] if at(c) <= ts < at(c) + NOISE_GRACE), None)
    check = due or next((c for c in NOISE_CHECKS[:-1] if at(c) > ts), None)
    if check is None:
        return "WAIT", "No noise-band entry checks remain today", metrics
    minute = (at(check) - timedelta(minutes=1)).strftime("%H:%M")
    sigma = noise_sigma(history, minute)
    if sigma is None:
        return "WAIT", f"Prior sessions lack enough {minute} candles", metrics
    upper = upper_anchor * (1 + NOISE_MULTIPLIER * sigma)
    lower = lower_anchor * (1 - NOISE_MULTIPLIER * sigma)
    metrics.update(check_time=f"{check:%H:%M}", sigma_pct=round(sigma * 100, 3),
                   upper_band=round(upper, 2), lower_band=round(lower, 2), confirmation="Waiting")
    if not due:
        return "WAIT", f"Next noise-band check at {check:%H:%M} IST", metrics
    reference = candles.get(minute)
    if not reference:
        return "WAIT", f"Waiting for the {minute} candle for the {due:%H:%M} check", metrics
    metrics.update(candle_time=reference["ts"], reference_close=reference["close"])
    direction = 1 if reference["close"] > upper else -1 if reference["close"] < lower else 0
    if not direction:
        return "WAIT", f"{due:%H:%M} check: NIFTY is inside the noise area", metrics
    band = upper if direction == 1 else lower
    if direction * (snapshot.spot - band) <= 0:
        return "WAIT", "Move faded back inside the noise area before entry", metrics
    # The trailing stop is the same-side band at each later check, fixed now so exits need no candles.
    exits = {}
    for later in NOISE_CHECKS:
        if later <= due:
            continue
        exit_sigma = noise_sigma(history, (at(later) - timedelta(minutes=1)).strftime("%H:%M"))
        if exit_sigma is None:
            return "WAIT", f"Prior sessions lack candles for the {later:%H:%M} exit check", metrics
        level = (upper_anchor * (1 + NOISE_MULTIPLIER * exit_sigma) if direction == 1
                 else lower_anchor * (1 - NOISE_MULTIPLIER * exit_sigma))
        exits[f"{later:%H:%M}"] = round(level, 2)
    # Losses are bounded by the engine's per-poll rupee stop, not by an index level: sizing checks only
    # spread, slippage and charges. Sizing to the band distance would skip every check where the move
    # is strong and keep the ones where it has faded back toward the band.
    metrics.update(confirmation=f"{due:%H:%M} close outside the noise area",
                   signal_id=f"{day}T{due:%H:%M}:{direction}", session_date=day,
                   stop_spot=round(band, 2), target_spot=None, stop_distance=0.0, exit_checks=exits)
    return ("CALL" if direction == 1 else "PUT"), "Half-hour close outside the noise area; band trailing stop", metrics


def noise_exit(plan: dict, direction: int, ts: datetime, previous_ts: datetime | None, spot: float) -> str | None:
    """Band trailing stop, evaluated once per half-hour check like the source, not between checks."""
    def at(label):
        hour, minute = map(int, label.split(":"))
        return ts.replace(hour=hour, minute=minute, second=0, microsecond=0)

    due = [label for label in plan.get("exit_checks", {})
           if at(label) <= ts and (previous_ts is None or previous_ts < at(label))]
    if not due:
        return None
    label = max(due)
    if direction * (spot - plan["exit_checks"][label]) <= 0:
        return f"noise band check {label}: back inside the noise area"
    return None


def plan_exit(plan: dict, direction: int, snapshot: Snapshot, previous_ts: datetime | None = None) -> str | None:
    """Index-level exits recorded at entry; rupee, daily-loss, kill-switch and time exits stay in the engine."""
    if plan.get("strategy") == "noise":
        return noise_exit(plan, direction, snapshot.ts, previous_ts, snapshot.spot)
    if direction * (snapshot.spot - plan["stop_spot"]) <= 0:
        return "breakout failed: structural stop"
    if direction * (snapshot.spot - plan["target_spot"]) >= 0:
        return "breakout 2R index target"
    return None


def option_risk(option: Option, snapshot: Snapshot, metrics: dict, config: Config) -> float:
    """Delta-based sizing estimate, with 25% model cushion plus spread, slippage and fees."""
    slip = config.paper_slippage_bps / 10_000
    entry = round(option.ask * (1 + slip), 2)
    exit_estimate = max(0, option.bid - abs(option.delta) * metrics['stop_distance'] * 1.25) * (1 - slip)
    return round((entry - exit_estimate) * option.lot_size + paper_fee(entry, option.lot_size, config)
                 + paper_fee(exit_estimate, option.lot_size, config, "SELL"), 2)
