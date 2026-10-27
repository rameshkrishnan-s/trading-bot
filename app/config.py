from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    mode: str = "demo"
    data_provider: str = "upstox"
    angel_api_key: str = field(default="", repr=False)
    angel_client_id: str = field(default="", repr=False)
    angel_pin: str = field(default="", repr=False)
    angel_totp_secret: str = field(default="", repr=False)
    # Optional scheduled Angel re-login; 0 is off. Each login ends any other session on the same API key.
    angel_session_refresh_minutes: int = 0
    auto_start: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    db_path: Path = Path("data/desk.sqlite3")
    upstox_token: str = field(default="", repr=False)
    algo_name: str = ""
    upstox_api_key: str = field(default="", repr=False)
    upstox_pin: str = field(default="", repr=False)
    upstox_totp_secret: str = field(default="", repr=False)
    live_enabled: bool = False
    static_ip_approved: bool = False
    control_token: str = field(default="", repr=False)
    strategy: str = "orb"
    reviewer_enabled: bool = False
    reviewer_provider: str = "gemini"
    antigravity_command: str = "agy"
    gemini_api_key: str = field(default="", repr=False)
    reviewer_model: str = "gemini-2.5-flash"
    reviewer_interval: int = 900
    starting_capital: float = 25_000
    max_premium: float = 12_500
    max_loss_per_trade: float = 500
    max_daily_loss: float = 1000
    max_trades_per_day: int = 2
    cooldown_minutes: int = 30
    held_poll_seconds: float = 3.0
    max_spread_pct: float = 2.0
    min_option_oi: int = 10_000
    min_days_to_expiry: int = 2
    order_tick: float = 0.05
    paper_fee_per_order: float = 20.0
    paper_fee_model: str = "legacy"
    paper_fee_bps: float = 5.0
    paper_slippage_bps: float = 5.0

    @classmethod
    def from_env(cls) -> "Config":
        mode = os.getenv("BOT_MODE", "demo").strip().lower()
        if mode not in {"demo", "paper", "live"}:
            raise ValueError("BOT_MODE must be demo, paper, or live")
        config = cls(
            mode=mode,
            strategy=os.getenv("STRATEGY", "orb").lower(),
            reviewer_enabled=_bool("REVIEWER_ENABLED", False),
            reviewer_provider=os.getenv("REVIEWER_PROVIDER", "gemini").strip().lower(),
            antigravity_command=os.getenv("ANTIGRAVITY_COMMAND", "agy"),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            reviewer_model=os.getenv("REVIEWER_MODEL", "gemini-2.5-flash"),
            reviewer_interval=int(os.getenv("REVIEWER_INTERVAL_SECONDS", "900")),
            data_provider=os.getenv("MARKET_DATA_PROVIDER", "upstox").strip().lower(),
            angel_api_key=os.getenv("ANGEL_API_KEY", ""),
            angel_client_id=os.getenv("ANGEL_CLIENT_ID", ""),
            angel_pin=os.getenv("ANGEL_PIN", ""),
            angel_totp_secret=os.getenv("ANGEL_TOTP_SECRET", ""),
            angel_session_refresh_minutes=int(os.getenv("ANGEL_SESSION_REFRESH_MINUTES") or 0),
            auto_start=_bool("BOT_AUTO_START", True),
            host=os.getenv("BOT_HOST", "127.0.0.1"),
            port=int(os.getenv("BOT_PORT", "8765")),
            db_path=Path(os.getenv("BOT_DB_PATH", "data/desk.sqlite3")),
            upstox_token=os.getenv("UPSTOX_ACCESS_TOKEN", ""),
            algo_name=os.getenv("UPSTOX_ALGO_NAME", ""),
            upstox_api_key=os.getenv("UPSTOX_API_KEY", ""),
            upstox_pin=os.getenv("UPSTOX_PIN", ""),
            upstox_totp_secret=os.getenv("UPSTOX_TOTP_SECRET", ""),
            live_enabled=_bool("LIVE_TRADING_ENABLED", False),
            static_ip_approved=_bool("LIVE_STATIC_IP_APPROVED", False),
            control_token=os.getenv("BOT_CONTROL_TOKEN", ""),
            starting_capital=float(os.getenv("STARTING_CAPITAL", "25000")),
            max_premium=float(os.getenv("MAX_PREMIUM_PER_TRADE", "12500")),
            max_loss_per_trade=float(os.getenv("MAX_LOSS_PER_TRADE", "500")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "1000")),
            max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "2")),
            cooldown_minutes=int(os.getenv("COOLDOWN_MINUTES", "30")),
            held_poll_seconds=float(os.getenv("HELD_POLL_SECONDS", "3")),
            max_spread_pct=float(os.getenv("MAX_SPREAD_PCT", "2.0")),
            min_option_oi=int(os.getenv("MIN_OPTION_OI", "10000")),
            min_days_to_expiry=int(os.getenv("MIN_DAYS_TO_EXPIRY", "2")),
            order_tick=float(os.getenv("BROKER_ORDER_TICK", "0.05")),
            paper_fee_per_order=float(os.getenv("PAPER_FEE_PER_ORDER", "20")),
            paper_fee_model=os.getenv("PAPER_FEE_MODEL", "legacy").strip(),
            paper_fee_bps=float(os.getenv("PAPER_FEE_BPS", "5")),
            paper_slippage_bps=float(os.getenv("PAPER_SLIPPAGE_BPS", "5")),
        )
        auto_fields = (config.upstox_api_key, config.upstox_pin, config.upstox_totp_secret)
        if any(auto_fields) and not all(auto_fields):
            raise ValueError("Set all of UPSTOX_API_KEY, UPSTOX_PIN and UPSTOX_TOTP_SECRET for automatic login")
        if config.upstox_pin and (not config.upstox_pin.isdigit() or len(config.upstox_pin) != 6):
            raise ValueError("UPSTOX_PIN must contain six digits")
        if config.data_provider not in {"upstox", "angel"}:
            raise ValueError("MARKET_DATA_PROVIDER must be upstox or angel")
        if config.mode != "demo" and config.data_provider == "angel" and not all((
                config.angel_api_key, config.angel_client_id, config.angel_pin, config.angel_totp_secret)):
            raise ValueError("Angel paper data requires ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PIN and ANGEL_TOTP_SECRET")
        if config.mode != "demo" and config.data_provider == "upstox" and not config.upstox_token and not all(auto_fields):
            raise ValueError("Provide UPSTOX_ACCESS_TOKEN or the three automatic-login credentials")
        if config.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Dashboard must bind to localhost")
        if config.mode == "live":
            if config.data_provider != "upstox":
                raise ValueError("Angel integration supports paper trading only")
            if not config.live_enabled:
                raise ValueError("Set LIVE_TRADING_ENABLED=true to use live mode")
            if not config.static_ip_approved:
                raise ValueError("Broker-approved static IP is required for live mode")
            if not config.control_token:
                raise ValueError("BOT_CONTROL_TOKEN is required for live mode")
        if min(config.starting_capital, config.max_premium, config.max_loss_per_trade, config.max_daily_loss) <= 0:
            raise ValueError("Capital and risk limits must be positive")
        if config.strategy not in {"orb", "ema", "noise"}:
            raise ValueError("STRATEGY must be orb, noise or ema")
        if config.strategy in {"orb", "noise"} and config.mode == "live":
            raise ValueError("Breakout adaptation is available for paper/demo validation only")
        if config.reviewer_provider not in {"gemini", "antigravity"}:
            raise ValueError("REVIEWER_PROVIDER must be gemini or antigravity")
        if not re.fullmatch(r"gemini-[a-zA-Z0-9.-]+", config.reviewer_model):
            raise ValueError("Invalid REVIEWER_MODEL")
        if config.reviewer_interval < 300:
            raise ValueError("Reviewer interval must be at least 300 seconds")
        numbers = (config.starting_capital, config.max_premium, config.max_loss_per_trade,
                   config.max_daily_loss, config.max_spread_pct, config.paper_fee_per_order,
                   config.paper_fee_bps, config.paper_slippage_bps)
        if not all(math.isfinite(v) and v >= 0 for v in numbers):
            raise ValueError("Risk and cost settings must be finite and nonnegative")
        if config.max_premium > config.starting_capital or config.max_loss_per_trade > config.max_daily_loss:
            raise ValueError("Premium must fit capital and per-trade risk must fit daily risk")
        if not 0 <= config.cooldown_minutes <= 1440:
            raise ValueError("COOLDOWN_MINUTES must be between 0 and 1440")
        if config.max_trades_per_day < 1:
            raise ValueError("MAX_TRADES_PER_DAY must be at least 1")
        if config.min_option_oi < 0 or config.min_days_to_expiry < 0:
            raise ValueError("MIN_OPTION_OI and MIN_DAYS_TO_EXPIRY cannot be negative")
        if not (math.isfinite(config.order_tick) and config.order_tick > 0):
            # Live limit prices are rounded to this tick; zero would divide by zero.
            raise ValueError("BROKER_ORDER_TICK must be positive")
        if not 1 <= config.port <= 65535:
            raise ValueError("BOT_PORT must be between 1 and 65535")
        if not 2 <= config.held_poll_seconds <= 15:
            raise ValueError("HELD_POLL_SECONDS must be between 2 and 15")
        if config.angel_session_refresh_minutes and not 5 <= config.angel_session_refresh_minutes <= 1440:
            # At least 5 minutes: frequent logins risk Angel's login limits and the lost-session guard.
            raise ValueError("ANGEL_SESSION_REFRESH_MINUTES must be 0 (off) or between 5 and 1440")
        from app.costs import ITEMIZED
        if config.paper_fee_model != "legacy" and config.paper_fee_model not in ITEMIZED:
            raise ValueError("Unknown PAPER_FEE_MODEL")
        if ITEMIZED.get(config.paper_fee_model, config.data_provider) != config.data_provider:
            raise ValueError("Itemized fee profile must match MARKET_DATA_PROVIDER")
        return config


# Explicit allowlist: broker/model credentials must never enter recorded policy metadata.
POLICY_FIELDS = ("strategy", "starting_capital", "max_premium", "max_loss_per_trade", "max_daily_loss",
                 "max_trades_per_day", "cooldown_minutes", "max_spread_pct", "min_option_oi", "min_days_to_expiry",
                 "paper_fee_per_order", "paper_fee_model", "paper_fee_bps", "paper_slippage_bps", "data_provider")


def policy(config: Config) -> dict:
    return {name: getattr(config, name) for name in POLICY_FIELDS}
