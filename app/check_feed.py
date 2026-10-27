"""Read-only Angel connection check; never starts the trading engine."""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

from app.angel import AngelMarket, GREEKS
from app.config import Config
from app.market import IST, MarketError
from app.server import load_env


def main():
    load_env(Path(__file__).resolve().parent.parent / ".env")
    config = Config.from_env()
    if config.mode != "paper" or config.data_provider != "angel":
        raise SystemExit("This connection check requires Angel paper mode")
    feed = AngelMarket(config.angel_api_key, config.angel_client_id, config.angel_pin, config.angel_totp_secret)
    report = {"mode": "paper", "provider": "Angel One", "authenticated": False,
              "snapshot_verified": False}
    try:
        feed.login()
        report["authenticated"] = True
        now = datetime.now(IST)
        feed._load_contracts(now.date())
        report["nifty_contracts"] = len(feed.contracts)
        quotes = feed._quotes("NSE", list(feed.indices.values()))
        report["index_quotes_received"] = len(quotes)
        report["exchange_timestamps"] = [q.get("exchFeedTime") for q in quotes.values()]
        if now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 30):
            snapshot = feed.snapshot(config.min_days_to_expiry)
            report.update(snapshot_verified=True, fresh_option_quotes=len(snapshot.options),
                          contracts_with_delta=sum(0.35 <= abs(o.delta) <= 0.65 for o in snapshot.options))
        else:
            report["note"] = "Outside regular market hours; fresh option snapshot verification remains pending"
            expiries = sorted({c["expiry"] for c in feed.contracts.values()
                               if (date.fromisoformat(c["expiry"]) - now.date()).days >= config.min_days_to_expiry})
            if expiries:
                expiry = expiries[0]
                spot = float(quotes[feed.indices["spot"]]["ltp"])
                nearby = sorted(((t, c) for t, c in feed.contracts.items() if c["expiry"] == expiry),
                                key=lambda pair: abs(pair[1]["strike"] - spot))[:4]
                option_quotes = feed._quotes("NFO", [t for t, _ in nearby])
                report["option_quotes_received"] = len(option_quotes)
                report["option_quotes_with_depth"] = sum(bool(q.get("depth", {}).get("buy")) and
                                                        bool(q.get("depth", {}).get("sell"))
                                                        for q in option_quotes.values())
                rows = feed._post(GREEKS, {"name": "NIFTY", "expirydate": date.fromisoformat(expiry).strftime("%d%b%Y").upper()})
                report["option_greek_rows"] = len(rows) if isinstance(rows, list) else 0
    except MarketError as exc:
        report["error"] = str(exc)
    except Exception as exc:
        # Do not print exception payloads from any unknown broker response.
        report["error"] = "Unexpected response: " + type(exc).__name__
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
