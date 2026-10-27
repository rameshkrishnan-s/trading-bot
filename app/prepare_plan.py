"""Prepare historical context and an advisory plan without starting trading."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from app.angel import AngelMarket
from app.config import Config
from app.context import MarketContext
from app.market import IST
from app.reviewer import Reviewer
from app.runtime import engine_locks
from app.server import load_env
from app.store import Store


def main():
    load_env(Path(__file__).resolve().parent.parent / '.env')
    config = Config.from_env()
    if config.mode != 'paper' or config.data_provider != 'angel':
        raise SystemExit('Historical planning requires Angel paper mode')
    # The server's database and account locks: never write history a running engine owns.
    with engine_locks(config):
        store = Store(config.db_path)
        reviewer = Reviewer(config, store, enabled=config.reviewer_enabled)
        history = MarketContext(store, reviewer, enabled=True)
        market = AngelMarket(config.angel_api_key, config.angel_client_id, config.angel_pin, config.angel_totp_secret)
        now = datetime.now(IST)
        try:
            history.prepare(market, now)
            deadline = time.monotonic() + 65
            while reviewer.worker and time.monotonic() < deadline:
                if reviewer.plan_status not in {"Plan queued", "Preparing AI plan"}:
                    break
                time.sleep(.2)
            data, plan = history.view(), reviewer.view()
            result = {"history_status": data['status'], "context": data['context'],
                      "plan_status": plan['plan_status'], "plan": plan['last_plan'],
                      "trading_started": False}
            print(json.dumps(result, indent=2))
        finally:
            reviewer.shutdown()
            if not reviewer.worker or not reviewer.worker.is_alive():
                store.db.close()


if __name__ == '__main__':
    main()
