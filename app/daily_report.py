"""Save a daily paper audit and optionally deliver its summary to Telegram."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3

from app.config import Config
from app.market import IST
from app.monitor import notify_change, telegram
from app.server import ROOT, load_env
from app.session_calendar import session
from app.session_report import report


def daily(path, day):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        closed, pnl = db.execute("SELECT count(*),coalesce(sum(realized),0) FROM positions WHERE status='CLOSED' AND substr(closed_at,1,10)=?", (day,)).fetchone()
        snapshots = db.execute('SELECT count(*) FROM snapshots WHERE substr(ts,1,10)=?', (day,)).fetchone()[0]
        opened = db.execute("SELECT count(*) FROM positions WHERE status='OPEN'").fetchone()[0]
        fees = db.execute('SELECT coalesce(sum(fee),0) FROM orders WHERE substr(ts,1,10)=?', (day,)).fetchone()[0]
        return {'date': day, 'completed_trades': closed, 'net_closed_pnl_inr': round(pnl, 2),
                'fees_booked_today_inr': round(fees, 2), 'snapshots': snapshots, 'open_positions': opened}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--notify', action='store_true')
    args = parser.parse_args()
    load_env(Path(os.getenv('BOT_ENV_FILE', str(ROOT / '.env'))))
    config = Config.from_env()
    if config.mode != 'paper':
        raise SystemExit('Daily reporting requires paper mode')
    now = datetime.now(IST)
    if not session(now)['trading_day']:
        print('No regular trading session; report skipped')
        return
    day = now.date().isoformat()
    summary = daily(config.db_path, day)
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = args.directory / (day + '.json')
    path.write_text(json.dumps({'daily': summary, 'experiment': report(config.db_path)}, indent=2))
    path.chmod(0o600)
    if args.notify:
        text = (f"Session {day}: {summary['completed_trades']} completed trades; "
                f"net closed P&L INR {summary['net_closed_pnl_inr']:.2f}; "
                f"fees booked today INR {summary['fees_booked_today_inr']:.2f}; "
                f"{summary['snapshots']} snapshots; {summary['open_positions']} open positions. "
                'Snapshot count alone does not establish complete session coverage.')
        if not summary['snapshots']:
            text += ' No market observations recorded: investigate the data gap.'
        try:
            notify_change(day, text, args.directory/'notification.json',
                lambda message: telegram(message, os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('TELEGRAM_CHAT_ID', '')))
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
