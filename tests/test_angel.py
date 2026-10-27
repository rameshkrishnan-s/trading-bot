import io
import json
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.angel import AngelMarket, GREEKS, LOGIN, QUOTES, RELOGIN_GUARD_SECONDS
from app.backtest import analyze
from app.config import Config
from app.engine import Engine
from app.market import DemoMarket, IST, MarketError, TransientMarketError
from app.store import Store


class AngelTests(unittest.TestCase):
    def feed(self):
        return AngelMarket("private-api", "private-client", "private-pin",
                           "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")

    def test_live_mode_is_rejected_even_with_live_gates(self):
        values = {"BOT_MODE": "live", "MARKET_DATA_PROVIDER": "angel", "ANGEL_API_KEY": "key",
                  "ANGEL_CLIENT_ID": "client", "ANGEL_PIN": "1234", "ANGEL_TOTP_SECRET": "seed",
                  "LIVE_TRADING_ENABLED": "true", "LIVE_STATIC_IP_APPROVED": "true",
                  "BOT_CONTROL_TOKEN": "control"}
        with patch.dict("os.environ", values, clear=True):
            with self.assertRaisesRegex(ValueError, "paper trading only"):
                Config.from_env()
        values["BOT_MODE"] = "paper"
        with patch.dict("os.environ", values, clear=True):
            config = Config.from_env()
        self.assertEqual(config.data_provider, "angel")
        self.assertNotIn("private-api", repr(replace(config, angel_api_key="private-api")))
        self.assertNotIn("1234", repr(config))

    def test_order_endpoint_is_blocked_before_any_network_call(self):
        with patch("app.angel.urllib.request.urlopen") as opener:
            with self.assertRaisesRegex(MarketError, "market data only"):
                self.feed()._post("/rest/secure/angelbroking/order/v1/placeOrder", {})
            opener.assert_not_called()

    def test_login_keeps_token_in_memory_and_sends_required_headers(self):
        feed = self.feed()
        feed.client_headers = {"X-ClientPublicIP": "203.0.113.1", "X-ClientLocalIP": "127.0.0.1",
                               "X-MACAddress": "00:11:22:33:44:55"}
        response = io.BytesIO(json.dumps({"status": True, "data": {"jwtToken": "session-token"}}).encode())
        with patch("app.angel.urllib.request.urlopen", return_value=response) as opener:
            feed.login()
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://apiconnect.angelone.in" + LOGIN)
        self.assertIn("X-macaddress", request.headers)
        self.assertEqual(json.loads(request.data)["clientcode"], "private-client")
        self.assertEqual(feed.token, "session-token")

    def test_broker_failure_never_echoes_payload(self):
        feed = self.feed()
        feed.client_headers = {"X-MACAddress": "test"}
        response = io.BytesIO(json.dumps({"status": False, "errorcode": "AB1012",
                                         "message": "private-api private-pin private-client"}).encode())
        with patch("app.angel.urllib.request.urlopen", return_value=response):
            with self.assertRaises(MarketError) as caught:
                feed.login()
        self.assertNotIn("private", str(caught.exception))

    def ready_feed(self):
        feed = self.feed()
        feed.token, feed.session_day = "session-token", datetime.now(IST).date()
        feed.client_headers = {"X-MACAddress": "test"}
        return feed

    def test_lost_session_relogs_once_and_a_repeated_loss_names_the_conflict(self):
        feed = self.ready_feed()
        def reply(body):
            return io.BytesIO(json.dumps(body).encode())
        lost = {"status": False, "errorcode": "AB1010", "message": "Invalid Session or Session is Expired"}
        ok = {"status": True, "data": {"fetched": []}}
        replies = [reply(lost), reply(ok), reply(lost), reply(lost), reply(ok)]
        with patch.object(feed, "login") as login, patch("app.angel.time.sleep"), \
                patch("app.angel.urllib.request.urlopen", side_effect=replies):
            self.assertEqual(feed._post(QUOTES, {}), {"fetched": []})
            self.assertEqual(login.call_count, 1)
            # One session per API key: losing it again at once means another login, not an expiry.
            with self.assertRaisesRegex(MarketError, "another client") as caught:
                feed._post(QUOTES, {})
            self.assertNotIsInstance(caught.exception, TransientMarketError)
            self.assertEqual(login.call_count, 1)
            feed.last_relogin -= RELOGIN_GUARD_SECONDS + 1  # A later, isolated expiry recovers again.
            self.assertEqual(feed._post(QUOTES, {}), {"fetched": []})
            self.assertEqual(login.call_count, 2)

    def test_gateway_throttle_403_retries_but_login_and_coded_403_do_not(self):
        feed = self.ready_feed()
        throttle = b"<html><body>Access denied because of exceeding access rate</body></html>"
        coded = json.dumps({"errorcode": "AG8004", "message": "Invalid API Key"}).encode()
        for path, body, retryable in ((QUOTES, throttle, True), (LOGIN, throttle, False), (QUOTES, coded, False)):
            error = urllib.error.HTTPError("https://apiconnect.angelone.in", 403, "Forbidden", {}, io.BytesIO(body))
            self.addCleanup(error.close)
            with self.subTest(path=path, body=body[:20]), patch("app.angel.time.sleep"), \
                    patch("app.angel.urllib.request.urlopen", side_effect=error):
                with self.assertRaises(MarketError) as caught:
                    feed._post(path, {})
                self.assertEqual(isinstance(caught.exception, TransientMarketError), retryable)
                self.assertNotIn("Access denied", str(caught.exception))

    def fake_angel(self, calls, fail_login=None):
        """Angel stand-in: logins return a numbered token; quotes succeed. Records (endpoint, token used)."""
        def reply(request, timeout):
            endpoint = request.full_url.rsplit("/", 1)[-1]
            calls.append((endpoint, request.get_header("Authorization")))
            if endpoint == "loginByPassword":
                if fail_login and fail_login.pop():
                    raise urllib.error.URLError("network down")
                return io.BytesIO(json.dumps({"status": True, "data": {"jwtToken": f"token-{len(calls)}"}}).encode())
            return io.BytesIO(json.dumps({"status": True, "data": {"fetched": []}}).encode())
        return patch("app.angel.urllib.request.urlopen", side_effect=reply)

    def requests_at(self, feed, minutes, fail_login=None):
        """Send one quote request at each elapsed minute mark; return what reached Angel."""
        calls, clock = [], {"now": 1000.0}
        feed.client_headers = {"X-MACAddress": "test"}
        with self.fake_angel(calls, fail_login), patch("app.angel.time.sleep"), \
                patch("app.angel.time.monotonic", side_effect=lambda: clock["now"]):
            for minute in minutes:
                clock["now"] = 1000.0 + minute * 60
                feed._post(QUOTES, {})
        return calls

    def test_session_refresh_is_off_by_default(self):
        calls = self.requests_at(self.feed(), (0, 60, 360))
        self.assertEqual([endpoint for endpoint, _ in calls], ["loginByPassword", "quote", "quote", "quote"])

    def test_optional_session_refresh_logs_in_again_every_interval(self):
        feed = AngelMarket("private-api", "private-client", "private-pin", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
                           refresh_minutes=14)
        calls = self.requests_at(feed, (0, 13, 14, 15, 28))
        self.assertEqual([endpoint for endpoint, _ in calls],
                         ["loginByPassword", "quote", "quote", "loginByPassword", "quote", "quote", "loginByPassword", "quote"])
        # Requests after each refresh carry the new session token.
        self.assertEqual([token for endpoint, token in calls if endpoint == "quote"],
                         ["Bearer token-1", "Bearer token-1", "Bearer token-4", "Bearer token-4", "Bearer token-7"])
        self.assertIsNone(feed.last_relogin)  # A scheduled refresh is not a lost session.

    def test_failed_refresh_keeps_the_current_session_and_retries_a_minute_later(self):
        feed = AngelMarket("private-api", "private-client", "private-pin", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
                           refresh_minutes=14)
        # Login outcomes in pop order: first succeeds, the 14-minute refresh fails, the retry succeeds.
        calls = self.requests_at(feed, (0, 14, 14.5, 15.05), fail_login=[False, True, False])
        self.assertEqual(calls, [("loginByPassword", None), ("quote", "Bearer token-1"),
                                 ("loginByPassword", None), ("quote", "Bearer token-1"),
                                 ("quote", "Bearer token-1"),
                                 ("loginByPassword", None), ("quote", "Bearer token-6")])

    def test_refresh_setting_is_validated_and_reaches_the_adapter(self):
        base = {"BOT_MODE": "paper", "MARKET_DATA_PROVIDER": "angel", "ANGEL_API_KEY": "k",
                "ANGEL_CLIENT_ID": "c", "ANGEL_PIN": "1234", "ANGEL_TOTP_SECRET": "s"}
        for value, expected in ((None, 0), ("", 0), ("0", 0), ("14", 14)):
            env = base if value is None else {**base, "ANGEL_SESSION_REFRESH_MINUTES": value}
            with self.subTest(value=value), patch.dict("os.environ", env, clear=True):
                self.assertEqual(Config.from_env().angel_session_refresh_minutes, expected)
        for value in ("4", "-1", "1441"):
            with self.subTest(value=value), patch.dict("os.environ", {**base, "ANGEL_SESSION_REFRESH_MINUTES": value}, clear=True):
                with self.assertRaisesRegex(ValueError, "ANGEL_SESSION_REFRESH_MINUTES"):
                    Config.from_env()
        store = Store(Path(":memory:"))
        self.addCleanup(store.db.close)
        engine = Engine(Config(mode="paper", data_provider="angel", angel_session_refresh_minutes=14), store)
        self.assertEqual(engine.market.refresh_seconds, 14 * 60)

    def test_stale_quotes_rejected(self):
        now = datetime.now(IST)
        raw = {"exchFeedTime": (now - timedelta(minutes=5)).strftime("%d-%b-%Y %H:%M:%S")}
        with self.assertRaisesRegex(MarketError, "stale"):
            AngelMarket._fresh(raw, now)

    def test_snapshot_normalizes_depth_and_keeps_distant_held_contract(self):
        feed = self.feed()
        now = datetime.now(IST)
        expiry = (now.date() + timedelta(days=3)).isoformat()
        held_expiry = (now.date() + timedelta(days=1)).isoformat()
        feed.contracts_day = now.date()
        feed.indices = {"spot": "99926000", "vix": "99926017"}
        feed.contracts = {
            "1": {"expiry": expiry, "strike": 25000, "lot": 75, "tick": .05, "symbol": "TESTCE", "kind": "CE"},
            "2": {"expiry": held_expiry, "strike": 20000, "lot": 75, "tick": .05, "symbol": "HELDPE", "kind": "PE"},
        }
        requests = []

        def fake_post(path, payload):
            requests.append((path, payload))
            if path == GREEKS:
                return [{"name": "NIFTY", "expiry": (now.date() + timedelta(days=3)).strftime("%d%b%Y").upper(),
                         "strikePrice": "25000", "optionType": "CE", "delta": "0.5"}]
            self.assertEqual(path, QUOTES)
            return {"fetched": [{"exchange": exchange, "symbolToken": token,
                                  "ltp": 14 if token == "99926017" else 25000 if token == "99926000" else 101,
                                  "close": 24900, "exchFeedTime": now.strftime("%d-%b-%Y %H:%M:%S"),
                                  "opnInterest": 100000, "tradeVolume": 25000,
                                  "depth": {"buy": [{"price": 100, "quantity": 75}],
                                            "sell": [{"price": 101, "quantity": 75}]}}
                                for exchange, tokens in payload["exchangeTokens"].items() for token in tokens]}

        feed._post = fake_post
        snapshot = feed.snapshot(2, held_expiry, "ANGEL_NFO|2")
        self.assertEqual(snapshot.source, "Angel One live data")
        self.assertEqual(snapshot.spot, 25000)
        self.assertEqual(snapshot.vix, 0)  # Entry-only VIX is not fetched while holding.
        self.assertEqual({o.key for o in snapshot.options}, {"ANGEL_NFO|2"})
        self.assertEqual(snapshot.options[0].bid, 100)
        self.assertEqual(snapshot.options[0].ask, 101)
        self.assertEqual(snapshot.options[0].delta, 0)
        self.assertTrue(all(path != GREEKS for path, _ in requests))
        # Spot and the held option share one quote request.
        self.assertEqual([r[1]["exchangeTokens"] for r in requests], [{"NSE": ["99926000"], "NFO": ["2"]}])
        self.assertTrue(all(path in {QUOTES, GREEKS} for path, _ in requests))
        # Entry Greeks failure must not prevent risk management of a held option.
        def unavailable_greeks(path, payload):
            if path == GREEKS:
                raise MarketError("Greeks unavailable")
            return fake_post(path, payload)
        feed._post = unavailable_greeks
        self.assertEqual(len(feed.snapshot(2, held_expiry, "ANGEL_NFO|2").options), 1)
        with self.assertRaisesRegex(MarketError, "Greeks unavailable"):
            feed.snapshot(2)
        def temporary_greeks_failure(path, payload):
            if path == GREEKS:
                raise TransientMarketError("AB9019")
            return fake_post(path, payload)
        feed._post = temporary_greeks_failure
        degraded = feed.snapshot(2)
        self.assertEqual(len(degraded.options), 1)
        self.assertEqual(degraded.options[0].delta, 0)

    def test_angel_paper_orders_are_local_only(self):
        store = Store(Path(":memory:"))
        self.addCleanup(store.db.close)
        feed = self.feed()
        engine = Engine(Config(mode="paper", data_provider="angel"), store, market=feed)
        option = DemoMarket().next().options[0]
        with patch.object(feed, "_post") as broker_request:
            filled, price, fee, broker_id = engine._execute(option, "BUY", option.lot_size)
        broker_request.assert_not_called()
        self.assertEqual(filled, option.lot_size)
        self.assertEqual(broker_id, "paper")

    def test_replay_accepts_angel_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            store = Store(path)
            feed = DemoMarket()
            for _ in range(35):
                store.add_snapshot(replace(feed.next(), source="Angel One live data"))
            store.db.close()
            report = analyze(path)
            self.assertIn("Angel One", report["source"])
            self.assertEqual(len(report["scenarios"]), 3)


if __name__ == "__main__":
    unittest.main()
