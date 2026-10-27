"""Angel One authentication and market data for paper trading only."""
from __future__ import annotations

import json
import ipaddress
import math
import re
import socket
import time
import uuid
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from app.market import IST, MarketError, TransientMarketError, Option, Snapshot, Upstox, candles_current, normalize_candles


LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword"
QUOTES = "/rest/secure/angelbroking/market/v1/quote"
GREEKS = "/rest/secure/angelbroking/marketData/v1/optionGreek"
CANDLES = "/rest/secure/angelbroking/historical/v1/getCandleData"
MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
# Invalid, expired or missing token; AB1010 is a session that expired or was replaced by a newer login.
SESSION_ERRORS = frozenset({"AG8001", "AG8002", "AG8003", "AB1010"})
# Angel allows one session per API key and client: losing it twice this quickly means another client logs in.
RELOGIN_GUARD_SECONDS = 300
# A scheduled refresh that fails on the network keeps the current session and is retried this much later.
REFRESH_RETRY_SECONDS = 60


class AngelMarket:
    # No order, portfolio or transfer endpoint is accessible through this adapter.
    ALLOWED_PATHS = frozenset({LOGIN, QUOTES, GREEKS, CANDLES})
    # Held-position snapshots fetch only spot and the held option, so the engine may poll them quickly.
    held_quote_only = True

    def __init__(self, api_key: str, client_id: str, pin: str, totp_secret: str, refresh_minutes: int = 0):
        self.api_key = api_key
        self.client_id = client_id
        self.pin = pin
        self.totp_secret = totp_secret
        self.token = ""
        self.session_day: date | None = None
        self.contracts_day: date | None = None
        self.contracts: dict[str, dict] = {}
        self.indices: dict[str, str] = {}
        self.last_request = 0.0
        self.client_headers: dict[str, str] = {}
        self.observer = None
        self.candle_cache: list[dict] = []
        self.last_relogin = None
        # Optional scheduled re-login (0 is off). Each login ends any other session on this API key,
        # logging out any other program that uses it.
        self.refresh_seconds = refresh_minutes * 60
        self.logged_in_at = None

    def _client_headers(self) -> dict[str, str]:
        if not self.client_headers:
            try:
                # This public-IP service receives no broker credentials or tokens.
                with urllib.request.urlopen("https://api.ipify.org", timeout=10) as response:
                    public_ip = str(ipaddress.ip_address(response.read(128).decode().strip()))
                local_ip = socket.gethostbyname(socket.gethostname())
            except (OSError, ValueError):
                raise MarketError("Could not determine client IP headers required by Angel") from None
            mac = f"{uuid.getnode():012x}"
            self.client_headers = {"X-ClientPublicIP": public_ip, "X-ClientLocalIP": local_ip,
                                   "X-MACAddress": ":".join(mac[i:i + 2] for i in range(0, 12, 2))}
        return self.client_headers

    def _post(self, path: str, payload: dict, retry_auth: bool = True):
        started = time.monotonic()
        data, error, outcome = None, "", "ok"
        try:
            data = self._post_once(path, payload, retry_auth)
            return data
        except MarketError as exc:
            error, outcome = str(exc), "retryable" if isinstance(exc, TransientMarketError) else "error"
            raise
        except Exception:
            error, outcome = "Unexpected market response failure", "error"
            raise
        finally:
            # Record only public market responses. Never record login payloads,
            # headers, tokens, client identifiers or arbitrary broker messages.
            if self.observer and path in {QUOTES, GREEKS, CANDLES}:
                self.observer({'ts': datetime.now(IST).isoformat(), 'endpoint': path,
                               'duration_ms': round((time.monotonic() - started) * 1000, 2),
                               'outcome': outcome, 'error': error, 'data': data})

    def _post_once(self, path: str, payload: dict, retry_auth: bool = True):
        if path not in self.ALLOWED_PATHS:
            raise MarketError("Angel adapter permits authentication and market data only")
        if path != LOGIN and (not self.token or self.session_day != datetime.now(IST).date()):
            self.login()
        elif (path != LOGIN and self.refresh_seconds and self.logged_in_at is not None
              and time.monotonic() - self.logged_in_at >= self.refresh_seconds):
            try:
                self.login()
            except TransientMarketError:
                # The current session is still valid: an optional refresh must never block market data.
                self.logged_in_at = time.monotonic() - self.refresh_seconds + REFRESH_RETRY_SECONDS
        remaining = 1.05 - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        self.last_request = time.monotonic()
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "X-PrivateKey": self.api_key, "X-UserType": "USER", "X-SourceID": "WEB"}
        headers.update(self._client_headers())
        if path != LOGIN:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request("https://apiconnect.angelone.in" + path,
                                         data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code <= 599:
                raise TransientMarketError(f"Angel market request temporarily unavailable (HTTP {exc.code})") from None
            if exc.code == 401 and path != LOGIN and retry_auth:
                self._relogin()
                return self._post(path, payload, retry_auth=False)
            try:
                failure = json.loads(exc.read())
                code = str(failure.get("errorcode") or "")
                message = str(failure.get("message") or "").lower()
                safe_code = code if re.fullmatch(r"[A-Z]{2}\d{4}", code) else "UNKNOWN"
                labels = [label for label in ("totp", "password", "api key", "clientcode", "clientlocalip",
                                               "clientpublicip", "macaddress", "invalid", "missing", "expired")
                          if label in message]
                hint = ", ".join(labels) or "request rejected"
            except (ValueError, AttributeError):
                if exc.code == 403 and path != LOGIN:
                    # Angel's gateway answers its own throttle ("exceeding access rate") with an HTML 403;
                    # it clears within seconds to minutes, so market data is retried rather than halted.
                    raise TransientMarketError("Angel rate limit: access denied (HTTP 403); retrying") from None
                safe_code, hint = "UNKNOWN", "non-JSON response"
            raise MarketError(f"Angel request failed (HTTP {exc.code}, {safe_code}, {hint})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransientMarketError("Angel connection failed; check network availability") from None
        except (ValueError, UnicodeError):
            raise MarketError("Angel returned an invalid JSON response") from None
        if not isinstance(result, dict):
            raise MarketError("Angel returned an invalid response")
        if result.get("status") is not True:
            code = str(result.get("errorcode") or "UNKNOWN")
            if code in SESSION_ERRORS and path != LOGIN and retry_auth:
                self._relogin()
                return self._post(path, payload, retry_auth=False)
            # Never log raw responses: broker errors can contain request fields.
            safe_code = code if re.fullmatch(r"[A-Z]{2}\d{4}", code) else "UNKNOWN"
            if safe_code == "AB9019":
                raise TransientMarketError("Angel option data temporarily unavailable (AB9019)")
            raise MarketError(f"Angel request rejected ({safe_code})")
        return result.get("data")

    def _relogin(self):
        """Replace a lost session once; repeated losses mean a session conflict that needs a person."""
        now = time.monotonic()
        if self.last_relogin is not None and now - self.last_relogin < RELOGIN_GUARD_SECONDS:
            raise MarketError("Angel session lost again within 5 minutes: another client may be logging in "
                              "with this API key (Angel allows one session per API key); use a separate key")
        self.last_relogin = now
        self.login()

    def login(self):
        data = self._post(LOGIN, {"clientcode": self.client_id, "password": self.pin,
                                  "totp": Upstox.totp(self.totp_secret)})
        token = data.get("jwtToken") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise MarketError("Angel login returned no session token")
        self.token = token.removeprefix("Bearer ")
        self.session_day = datetime.now(IST).date()
        self.logged_in_at = time.monotonic()

    def _load_contracts(self, today: date):
        if self.contracts_day == today:
            return
        try:
            with urllib.request.urlopen(MASTER, timeout=60) as response:
                rows = json.load(response)
        except (urllib.error.URLError, OSError, ValueError):
            raise MarketError("Could not load Angel instrument master") from None
        if not isinstance(rows, list):
            raise MarketError("Invalid Angel instrument master")
        contracts, indices = {}, {}
        for row in rows:
            segment = row.get("exch_seg")
            token = str(row.get("token") or "")
            if not token.isdigit():
                continue
            if segment == "NSE":
                names = {str(row.get(k) or "").upper().replace(" ", "") for k in ("name", "symbol")}
                if "NIFTY50" in names:
                    indices["spot"] = token
                if "INDIAVIX" in names:
                    indices["vix"] = token
            if segment != "NFO" or row.get("instrumenttype") != "OPTIDX" or row.get("name") != "NIFTY":
                continue
            try:
                expiry = datetime.strptime(row["expiry"], "%d%b%Y").date().isoformat()
                strike = float(row["strike"]) / 100
                lot = int(row["lotsize"])
                tick = float(row["tick_size"]) / 100
                symbol = str(row["symbol"])
                kind = symbol[-2:]
                if kind not in {"CE", "PE"} or not all(math.isfinite(v) and v > 0 for v in (strike, lot, tick)):
                    continue
                contracts[token] = {"expiry": expiry, "strike": strike, "lot": lot,
                                    "tick": tick, "symbol": symbol, "kind": kind}
            except (KeyError, TypeError, ValueError):
                continue
        if not contracts or set(indices) != {"spot", "vix"}:
            raise MarketError("Angel master is missing NIFTY options or NIFTY/VIX indices")
        self.contracts, self.indices, self.contracts_day = contracts, indices, today

    @staticmethod
    def _fresh(raw: dict, now: datetime):
        try:
            stamp = datetime.strptime(raw["exchFeedTime"], "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        except (KeyError, TypeError, ValueError):
            raise MarketError("Angel quote has no valid exchange timestamp") from None
        age = (now - stamp).total_seconds()
        if age < -10:
            raise MarketError("Angel quote is ahead of the local clock; check time synchronization")
        if age > 90:
            # Usually a feed delay (for example India VIX at the open): paper mode retries; live still halts.
            raise TransientMarketError("Angel quote is stale (over 90 seconds)")

    def _quotes(self, exchange: str, tokens: list[str]) -> dict[str, dict]:
        results = {}
        for start in range(0, len(tokens), 50):
            batch = tokens[start:start + 50]
            results.update(self._quote_request({exchange: batch})[exchange])
        return results

    def _quote_request(self, tokens: dict[str, list[str]]) -> dict[str, dict[str, dict]]:
        """One FULL quote request (at most 50 tokens), keyed by exchange then token."""
        data = self._post(QUOTES, {"mode": "FULL", "exchangeTokens": tokens})
        if not isinstance(data, dict) or not isinstance(data.get("fetched"), list):
            raise MarketError("Angel FULL quote response is malformed")
        results = {exchange: {} for exchange in tokens}
        for raw in data["fetched"]:
            token, exchange = str(raw.get("symbolToken") or ""), raw.get("exchange")
            if exchange in tokens and token in tokens[exchange]:
                results[exchange][token] = raw
        return results

    def intraday_candles(self, now: datetime) -> list[dict]:
        key = now.strftime("%Y-%m-%d %H:%M")
        if getattr(self, "candle_minute", None) != key or not candles_current(self.candle_cache, now):
            self._load_contracts(now.date())
            rows = self._post(CANDLES, {"exchange": "NSE", "symboltoken": self.indices["spot"],
                                      "interval": "ONE_MINUTE", "fromdate": now.strftime("%Y-%m-%d 09:15"),
                                      "todate": key})
            self.candle_cache = normalize_candles(rows, now)
            self.candle_minute = key
        return self.candle_cache

    def prior_session_candles(self, now: datetime, days: int = 10):
        """Read recent completed days; validation and session coverage live in context.py/history.py."""
        self._load_contracts(now.date())
        return self._post(CANDLES, {"exchange": "NSE", "symboltoken": self.indices["spot"],
                                   "interval": "ONE_MINUTE",
                                   "fromdate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15"),
                                   "todate": (now - timedelta(days=1)).strftime("%Y-%m-%d 15:30")})

    def snapshot(self, min_days_to_expiry: int, held_expiry: str | None = None,
                 held_key: str | None = None) -> Snapshot:
        now = datetime.now(IST)
        self._load_contracts(now.date())
        held_token = held_key.removeprefix("ANGEL_NFO|") if held_key else None
        if held_token and held_token not in self.contracts:
            raise MarketError("Held Angel option is absent from the instrument master")
        held_quotes = None
        if held_token:
            # A single request keeps the exit-check cycle to one broker round trip.
            fetched = self._quote_request({"NSE": [self.indices["spot"]], "NFO": [held_token]})
            indices, held_quotes = fetched["NSE"], fetched["NFO"]
        else:
            indices = self._quotes("NSE", list(self.indices.values()))
        spot_quote = indices.get(self.indices["spot"])
        vix_quote = indices.get(self.indices["vix"])
        if not spot_quote or (not held_key and not vix_quote):
            raise MarketError("Angel NIFTY or India VIX quote missing")
        for quote in ([spot_quote] if held_key else [spot_quote, vix_quote]):
            self._fresh(quote, datetime.now(IST))
        spot, vix = float(spot_quote["ltp"]), float(vix_quote["ltp"]) if vix_quote else 0.0
        if not all(math.isfinite(v) and v > 0 for v in ([spot] if held_key else [spot, vix])):
            raise MarketError("Invalid Angel index prices")
        eligible = sorted({c["expiry"] for c in self.contracts.values()
                           if (date.fromisoformat(c["expiry"]) - now.date()).days >= min_days_to_expiry})
        if not eligible and not held_expiry:
            raise MarketError("No Angel option expiry satisfies the minimum days rule")
        expiry = eligible[0] if eligible else held_expiry
        near = sorted(((token, c) for token, c in self.contracts.items() if c["expiry"] == expiry),
                      key=lambda pair: abs(pair[1]["strike"] - spot))[:30]
        selected = dict(near)
        if held_token:
            selected = {held_token: self.contracts[held_token]}
        deltas = {}
        # Held-position monitoring must not wait for entry Greeks or unrelated quotes.
        if not held_key:
            try:
                rows = self._post(GREEKS, {"name": "NIFTY", "expirydate": date.fromisoformat(expiry).strftime("%d%b%Y").upper()})
            except TransientMarketError:
                # Still collect quotes/candles; zero deltas fail entry selection.
                # The failed endpoint remains in request telemetry.
                rows = []
            for row in rows if isinstance(rows, list) else []:
                try:
                    greek_expiry = datetime.strptime(row["expiry"], "%d%b%Y").date().isoformat()
                    if row.get("name") != "NIFTY" or greek_expiry != expiry:
                        continue
                    strike, delta = float(row["strikePrice"]), float(row["delta"])
                    if math.isfinite(delta) and -1 <= delta <= 1:
                        deltas[(strike, row["optionType"])] = delta
                except (KeyError, ValueError, TypeError):
                    continue
        quotes = held_quotes if held_quotes is not None else self._quotes("NFO", list(selected))
        options = []
        for token, c in selected.items():
            raw = quotes.get(token)
            if not raw:
                continue
            try:
                self._fresh(raw, datetime.now(IST))
                depth = raw.get("depth") or {}
                buys = [float(v["price"]) for v in depth.get("buy", []) if float(v.get("quantity") or 0) > 0]
                sells = [float(v["price"]) for v in depth.get("sell", []) if float(v.get("quantity") or 0) > 0]
                bid, ask = max(buys, default=0), min(sells, default=0)
                ltp = float(raw.get("ltp") or 0)
                if not all(math.isfinite(v) and v >= 0 for v in (bid, ask, ltp)):
                    continue
                options.append(Option("ANGEL_NFO|" + token, c["symbol"], c["kind"], c["strike"],
                                      c["expiry"], c["lot"], c["tick"], bid, ask, ltp,
                                      int(raw.get("opnInterest") or 0), int(raw.get("tradeVolume") or 0),
                                      deltas.get((c["strike"], c["kind"]), 0.0) if c["expiry"] == expiry else 0.0))
            except (KeyError, ValueError, TypeError, MarketError):
                continue
        if not options:
            # Malformed responses fail earlier; an empty set here means stale or missing quotes.
            raise TransientMarketError("Angel returned no fresh option quotes with usable depth")
        return Snapshot(now, spot, vix, options, "Angel One live data", float(spot_quote.get("close") or spot))
