from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "NSE_INDEX|Nifty 50"
# Read-only Upstox endpoints whose temporary failures may be retried. Order, order-status and
# portfolio requests are never retryable: their outcome must be reconciled, not repeated.
UPSTOX_MARKET_DATA = ("/v3/market-quote/", "/v2/option/", "/v3/historical-candle/")


class MarketError(RuntimeError):
    pass


class TransientMarketError(MarketError):
    """Read-only feed request can be retried without resubmitting an order."""


@dataclass(frozen=True)
class Option:
    key: str
    symbol: str
    option_type: str
    strike: float
    expiry: str
    lot_size: int
    tick_size: float
    bid: float
    ask: float
    ltp: float
    oi: int
    volume: int
    delta: float

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.ask * 100 if self.ask > 0 else 999.0


@dataclass(frozen=True)
class Snapshot:
    ts: datetime
    spot: float
    vix: float
    options: list[Option]
    source: str
    previous_close: float
    candles: list[dict] = field(default_factory=list)
    candle_error: str = ""
    collection_ms: float | None = None


def check_quote_time(traded_ms, now: datetime):
    """A missing timestamp is malformed; an old one is usually a feed delay that paper mode retries."""
    if not traded_ms:
        raise MarketError("NIFTY quote has no timestamp")
    if now.timestamp() - int(traded_ms) / 1000 > 90:
        raise TransientMarketError("NIFTY quote is stale (over 90 seconds)")


class DemoMarket:
    """Deterministic synthetic tape. Every call advances one market minute."""

    def __init__(self):
        self.random = random.Random(34071)
        self.minute = 0
        self.candles = []
        self.spot = 24680.0
        self.previous_close = 24580.0
        self.day = datetime.now(IST).date()
        self.sessions = self._synthetic_sessions(20)

    def _synthetic_sessions(self, count: int) -> dict[str, list[dict]]:
        """Random-walk prior sessions ending at the demo's previous close, for multi-day strategies."""
        from app.session_calendar import previous_trading_day
        days, day = [], self.day
        while len(days) < count:
            day = previous_trading_day(day) or day - timedelta(days=3 if day.weekday() == 0 else 2 if day.weekday() == 6 else 1)
            days.append(day)
        walk = random.Random(9071)
        sessions, price = {}, self.previous_close
        for day in reversed(days):
            start = datetime.combine(day, datetime.min.time(), IST) + timedelta(hours=9, minutes=15)
            candles = []
            for minute in range(375):
                opened, price = price, max(1000, price + walk.gauss(0, 4.4))
                candles.append({"ts": (start + timedelta(minutes=minute)).isoformat(), "open": opened,
                                "high": max(opened, price), "low": min(opened, price), "close": price})
            sessions[day.isoformat()] = candles
        shift = self.previous_close - price
        return {day: [{**c, **{k: round(c[k] + shift, 2) for k in ("open", "high", "low", "close")}} for c in candles]
                for day, candles in sessions.items()}

    def prior_session_candles(self, now: datetime, days: int = 10) -> list:
        first = (now - timedelta(days=days)).date().isoformat()
        return [[c["ts"], c["open"], c["high"], c["low"], c["close"]] for day, candles in sorted(self.sessions.items())
                if first <= day < now.date().isoformat() for c in candles]

    def next(self) -> Snapshot:
        self.minute += 1
        if self.minute > 375:
            self.sessions[self.day.isoformat()] = [c for c in self.candles if c["ts"][:10] == self.day.isoformat()]
            self.minute = 1
            self.day += timedelta(days=1)
        # A changing trend creates inspectable decisions; it is not real market data.
        drift = 0.9 if self.minute < 125 else (-1.25 if self.minute < 255 else 0.7)
        wave = math.sin(self.minute / 11) * 1.8
        self.spot = max(1000, self.spot + drift + wave + self.random.gauss(0, 4.4))
        vix = 13.8 + 1.4 * math.sin(self.minute / 39)
        ts = datetime.combine(self.day, datetime.min.time(), IST) + timedelta(hours=9, minutes=14 + self.minute)
        expiry = (self.day + timedelta(days=7)).isoformat()
        center = round(self.spot / 50) * 50
        options = []
        for strike in range(center - 300, center + 301, 50):
            for kind in ("CE", "PE"):
                intrinsic = max(0, self.spot - strike) if kind == "CE" else max(0, strike - self.spot)
                extrinsic = 104 * math.exp(-abs(self.spot - strike) / 325) + 17
                fair = max(1.0, intrinsic + extrinsic)
                spread = max(0.5, fair * 0.007)
                bid = round((fair - spread / 2) / 0.05) * 0.05
                ask = round((fair + spread / 2) / 0.05) * 0.05
                signed_delta = 0.5 + (self.spot - strike) / 850
                delta = max(0.05, min(0.95, signed_delta))
                if kind == "PE":
                    delta -= 1
                options.append(Option(
                    key=f"DEMO|{expiry}|{strike}|{kind}",
                    symbol=f"NIFTY {strike} {kind} {(self.day + timedelta(days=7)).strftime('%d %b').upper()}",
                    option_type=kind, strike=float(strike), expiry=expiry,
                    lot_size=75, tick_size=0.05, bid=round(bid, 2),
                    ask=round(ask, 2), ltp=round(fair, 2), oi=350_000,
                    volume=1_200_000, delta=round(delta, 3),
                ))
        closed = [c for c in self.candles if c["ts"][:10] == ts.date().isoformat()]
        self.candles = closed + [{"ts": ts.isoformat(), "open": self.spot, "high": self.spot,
                                  "low": self.spot, "close": self.spot}]
        return Snapshot(ts, round(self.spot, 2), round(vix, 2), options, "Synthetic demo", self.previous_close, closed)


class Upstox:
    """Small typed wrapper around official Upstox REST endpoints."""
    # Held-position snapshots fetch only spot and the held option, so the engine may poll them quickly.
    held_quote_only = True

    def __init__(self, token: str, algo_name: str = "", api_key: str = "", pin: str = "", totp_secret: str = ""):
        self.auto_login = bool(api_key and pin and totp_secret)
        self.token = "" if self.auto_login else token
        self.algo_name = algo_name
        self.api_key = api_key
        self.pin = pin
        self.totp_secret = totp_secret
        self.token_expires_at: datetime | None = None
        self.contracts: dict[str, dict] = {}
        self.contracts_day: date | None = None
        self.candle_cache: list[dict] = []

    def intraday_candles(self, now: datetime) -> list[dict]:
        key = now.strftime("%Y-%m-%d %H:%M")
        if getattr(self, "candle_minute", None) != key or not candles_current(self.candle_cache, now):
            data = self._get("/v3/historical-candle/intraday/" + urllib.parse.quote(UNDERLYING, safe="") + "/minutes/1")
            self.candle_cache = normalize_candles(data.get("candles", []), now)
            self.candle_minute = key
        return self.candle_cache

    def prior_session_candles(self, now: datetime, days: int = 10) -> list:
        """Earlier sessions only; the historical endpoint can return nothing for today (to_date is inclusive)."""
        to_date = (now - timedelta(days=1)).date().isoformat()
        from_date = (now - timedelta(days=days)).date().isoformat()
        data = self._get("/v3/historical-candle/" + urllib.parse.quote(UNDERLYING, safe="")
                         + f"/minutes/1/{to_date}/{from_date}")
        return (data or {}).get("candles", [])

    @staticmethod
    def totp(secret: str, timestamp: int | None = None) -> str:
        clean = secret.replace(" ", "").upper()
        try:
            key = base64.b32decode(clean + "=" * ((8 - len(clean) % 8) % 8), casefold=True)
        except (ValueError, binascii.Error) as exc:
            raise MarketError("Invalid TOTP seed encoding") from exc
        counter = int(time.time() if timestamp is None else timestamp) // 30
        digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
        return f"{code % 1_000_000:06d}"

    def refresh_token(self):
        if not self.auto_login:
            raise MarketError("No automatic Upstox login credentials configured")
        payload = json.dumps({"totp": self.totp(self.totp_secret), "pin": self.pin}).encode()
        request = urllib.request.Request("https://api.upstox.com/v2/totp-login/authorization/token",
                                         data=payload, method="POST",
                                         headers={"Accept": "application/json", "Content-Type": "application/json",
                                                  "x-api-key": self.api_key})
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            error = TransientMarketError if exc.code == 429 or 500 <= exc.code <= 599 else MarketError
            raise error(f"Upstox automatic login failed (HTTP {exc.code})") from None
        except (urllib.error.URLError, OSError):
            raise TransientMarketError("Upstox automatic login connection failed") from None
        token = (data.get("data") or {}).get("access_token") or data.get("access_token")
        if data.get("status") != "success" or not token:
            raise MarketError("Upstox automatic login returned no access token")
        self.token = token
        now = datetime.now(IST)
        expiry = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if now >= expiry:
            expiry += timedelta(days=1)
        self.token_expires_at = expiry

    def _ensure_token(self):
        if self.auto_login and (not self.token or (self.token_expires_at and datetime.now(IST) >= self.token_expires_at)):
            self.refresh_token()
        if not self.token:
            raise MarketError("Upstox access token is unavailable")

    def _request(self, method: str, base: str, path: str, params=None, body=None, _allow_refresh: bool = True):
        self._ensure_token()
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.algo_name and method != "GET":
            headers["X-Algo-Name"] = self.algo_name
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers, method=method)
        retryable = method == "GET" and path.startswith(UPSTOX_MARKET_DATA)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and self.auto_login and method == "GET" and _allow_refresh:
                self.refresh_token()
                return self._request(method, base, path, params, body, _allow_refresh=False)
            if retryable and (exc.code == 429 or 500 <= exc.code <= 599):
                raise TransientMarketError(f"Upstox market data temporarily unavailable (HTTP {exc.code})") from None
            # Never include tokens or request headers in an error/event.
            try:
                detail = json.loads(exc.read().decode()).get("errors", [])
            except (ValueError, UnicodeDecodeError):
                detail = []
            raise MarketError(f"Upstox HTTP {exc.code}: {str(detail)[:260]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            # TimeoutError and connection resets are OSErrors; HTTPError is handled above.
            error = TransientMarketError if retryable else MarketError
            raise error(f"Upstox connection failed: {exc.reason if hasattr(exc, 'reason') else exc}") from exc
        except ValueError:
            raise MarketError("Upstox returned an invalid JSON response") from None
        if data.get("status") != "success":
            raise MarketError(f"Upstox returned status {data.get('status')}")
        return data.get("data")

    def _get(self, path: str, params=None):
        return self._request("GET", "https://api.upstox.com", path, params)

    def _contracts(self, today: date) -> dict[str, dict]:
        if self.contracts_day != today:
            rows = self._get("/v2/option/contract", {"instrument_key": UNDERLYING})
            self.contracts = {row["instrument_key"]: row for row in rows or []}
            self.contracts_day = today
        return self.contracts

    def _held_snapshot(self, now: datetime, held_key: str) -> Snapshot:
        """One full-quote request for NIFTY and the held option; no chain, Greeks or VIX."""
        contract = self._contracts(now.date()).get(held_key)
        if not contract:
            raise MarketError("Held Upstox option is absent from the contract list")
        quotes = self._get("/v3/market-quote/quotes", {"instrument_key": f"{UNDERLYING},{held_key}"}) or {}
        index = quotes.get("NSE_INDEX:Nifty 50")
        # Option response keys use the trading symbol; match on the pipe-style instrument token.
        raw = next((q for q in quotes.values() if isinstance(q, dict) and q.get("instrument_token") == held_key), None)
        if not index or not raw:
            raise MarketError("NIFTY or held option quote missing")
        spot = float(index.get("last_price") or 0)
        check_quote_time(index.get("last_trade_time"), now)
        if spot <= 0:
            raise MarketError("Invalid index quote")
        depth = raw.get("depth") or {}
        buys = [float(v["price"]) for v in depth.get("buy", []) if float(v.get("quantity") or 0) > 0]
        sells = [float(v["price"]) for v in depth.get("sell", []) if float(v.get("quantity") or 0) > 0]
        raw_tick = float(contract.get("tick_size") or 5)
        option = Option(
            key=held_key, symbol=contract.get("trading_symbol", held_key),
            option_type=str(contract.get("instrument_type") or contract.get("trading_symbol", "")[-2:]),
            strike=float(contract.get("strike_price") or 0), expiry=contract["expiry"],
            lot_size=int(contract["lot_size"]), tick_size=raw_tick / 100 if raw_tick >= 1 else raw_tick,
            bid=max(buys, default=0.0), ask=min(sells, default=0.0), ltp=float(raw.get("last_price") or 0),
            oi=int(raw.get("oi") or 0), volume=int(raw.get("volume") or 0), delta=0.0)
        return Snapshot(now, spot, 0.0, [option], "Upstox live data", float(index.get("prev_close_price") or spot))

    def snapshot(self, min_days_to_expiry: int, held_expiry: str | None = None,
                 held_key: str | None = None) -> Snapshot:
        now = datetime.now(IST)
        if held_key:
            # Held-position monitoring must not wait for the option chain or entry Greeks.
            return self._held_snapshot(now, held_key)
        quotes = self._get("/v3/market-quote/quotes", {"instrument_key": f"{UNDERLYING},NSE_INDEX|India VIX"})
        index = (quotes or {}).get("NSE_INDEX:Nifty 50")
        vix_quote = (quotes or {}).get("NSE_INDEX:India VIX")
        if not index or not vix_quote:
            raise MarketError("NIFTY or India VIX quote missing")
        spot = float(index["last_price"])
        vix = float(vix_quote["last_price"])
        check_quote_time(index.get("last_trade_time"), now)
        if spot <= 0 or vix <= 0:
            raise MarketError("Invalid index/VIX quote")
        contracts = self._contracts(now.date())
        expiries = sorted({c["expiry"] for c in contracts.values() if c.get("expiry")})
        eligible = [e for e in expiries if (date.fromisoformat(e) - now.date()).days >= min_days_to_expiry]
        if not eligible and not held_expiry:
            raise MarketError("No option expiry satisfies the minimum days rule")
        expiry = eligible[0] if eligible else None
        options = []
        # Entry eligibility must not remove a held contract from exit monitoring.
        for chain_expiry in dict.fromkeys((expiry, held_expiry)):
            if not chain_expiry:
                continue
            chain = self._get("/v2/option/chain", {"instrument_key": UNDERLYING, "expiry_date": chain_expiry})
            for row in chain or []:
                for field, kind in (("call_options", "CE"), ("put_options", "PE")):
                    item = row.get(field) or {}
                    key = item.get("instrument_key")
                    contract = contracts.get(key)
                    market = item.get("market_data") or {}
                    greek = item.get("option_greeks") or {}
                    if not key or not contract:
                        continue
                    raw_tick = float(contract.get("tick_size") or 5)
                    tick = raw_tick / 100 if raw_tick >= 1 else raw_tick
                    options.append(Option(
                        key=key, symbol=contract.get("trading_symbol", key),
                        option_type=kind, strike=float(row["strike_price"]),
                        expiry=chain_expiry, lot_size=int(contract["lot_size"]),
                        tick_size=tick, bid=float(market.get("bid_price") or 0),
                        ask=float(market.get("ask_price") or 0),
                        ltp=float(market.get("ltp") or 0),
                        oi=int(market.get("oi") or 0), volume=int(market.get("volume") or 0),
                        delta=float(greek.get("delta") or 0),
                    ))
        return Snapshot(now, spot, vix, options, "Upstox live data", float(index.get("prev_close_price") or spot))

    def positions(self) -> list[dict]:
        return self._get("/v2/portfolio/short-term-positions") or []

    def place_ioc(self, option: Option, side: str, qty: int, limit_price: float, tag: str) -> tuple[int, float, str]:
        payload = {
            "quantity": qty, "product": "I", "validity": "IOC", "price": limit_price,
            "tag": tag, "instrument_token": option.key, "order_type": "LIMIT",
            "transaction_type": side, "disclosed_quantity": 0, "trigger_price": 0,
            "is_amo": False, "slice": False,
        }
        data = self._request("POST", "https://api-hft.upstox.com", "/v3/order/place", body=payload)
        ids = (data or {}).get("order_ids") or []
        if len(ids) != 1:
            raise MarketError("Unexpected order response; reconcile broker order book before restarting")
        order_id = str(ids[0])
        detail = None
        for _ in range(5):
            time.sleep(0.6)
            detail = self._get("/v2/order/details", {"order_id": order_id})
            if (detail or {}).get("status") in {"complete", "cancelled", "rejected"}:
                break
        if detail and detail.get("status") not in {"complete", "cancelled", "rejected"}:
            try:
                self._request("DELETE", "https://api-hft.upstox.com", "/v3/order/cancel", {"order_id": order_id})
                time.sleep(0.6)
                detail = self._get("/v2/order/details", {"order_id": order_id})
            except MarketError as exc:
                raise MarketError(f"Order {order_id} uncertain after cancel attempt: {exc}") from exc
        if not detail or detail.get("status") not in {"complete", "cancelled", "rejected"}:
            raise MarketError(f"Order {order_id} has uncertain status; manual reconciliation required")
        filled = int(detail.get("filled_quantity") or 0)
        price = float(detail.get("average_price") or 0)
        if filled > qty or (filled and price <= 0):
            raise MarketError(f"Order {order_id} returned invalid fill; manual reconciliation required")
        return filled, price, order_id


def candles_current(candles: list[dict], now: datetime) -> bool:
    """True once the broker has published the candle that closed at the start of this minute.

    Brokers publish a minute's candle a few seconds late; a cache that waits for the next minute
    would miss a two-close confirmation entirely, so callers refetch until it appears.
    """
    expected = (now.replace(second=0, microsecond=0) - timedelta(minutes=1)).isoformat()
    return bool(candles) and candles[-1]["ts"] == expected


def normalize_candles(rows, now: datetime) -> list[dict]:
    """Only completed, valid, same-session broker OHLC; never synthesize gaps."""
    candles = {}
    cutoff = now.replace(second=0, microsecond=0)
    for row in rows or []:
        try:
            ts = datetime.fromisoformat(row[0])
            if ts.tzinfo is None:
                continue
            ts = ts.astimezone(IST)
            if ts.date() != now.date() or ts >= cutoff or (ts.hour, ts.minute) < (9, 15):
                continue
            o, h, l, c = map(float, row[1:5])
            if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
                continue
            if ts.second or ts.microsecond:
                continue
            candles[ts.isoformat()] = {"ts": ts.isoformat(), "open": o, "high": h, "low": l, "close": c}
        except (TypeError, ValueError, IndexError):
            continue
    return [candles[k] for k in sorted(candles)]
