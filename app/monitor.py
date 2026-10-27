"""Independent local health monitor with deduplicated Telegram notifications."""
import argparse
from datetime import datetime, time
import json
import os
from pathlib import Path
import shutil
import urllib.error
import urllib.request

from app.config import Config
from app.market import IST
from app.runtime import file_lock
from app.server import ROOT, load_env


def telegram(text, token, chat_id):
    if not token or not chat_id:
        raise ValueError('Telegram is not configured')
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage',
        data=json.dumps({'chat_id': chat_id, 'text': text, 'disable_notification': False}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        if result.get('ok') is not True:
            raise ValueError('Telegram rejected the notification')
    except Exception:
        # URLs contain bot tokens, and server responses may contain private data.
        raise ValueError('Telegram delivery failed; check credentials and connectivity') from None


def assess(health, now):
    if not health or not health.get('ok'):
        return 'engine_unavailable', 'Bot process is unavailable or its engine loop has stalled.'
    if health.get('exit_overdue'):
        return 'exit_overdue', 'An open paper position remains past the exit deadline; inspect the bot.'
    if health.get('status') in {'HALTED', 'PAUSED'}:
        return 'attention_' + health['status'], 'Bot is ' + health['status'] + '; inspect before resuming.'
    if not health.get('session', {}).get('known'):
        return 'calendar_unknown', 'Update the NSE session calendar; automatic entries are blocked.'
    if health['session']['open'] and now.time() >= time(9, 17):
        if not health.get('feed_fresh'):
            return 'feed_stale', 'Fresh market data is unavailable; check feed recovery.'
        if not health.get('open_position'):
            if not health.get('candles_ok'):
                return 'candles_missing', 'Completed market candles are unavailable; entries are blocked.'
            if not health.get('greeks_ok'):
                return 'greeks_missing', 'Usable option deltas are unavailable; entries are blocked.'
            if not health.get('history_ok', True):
                return 'history_missing', 'Prior-session candles are unavailable; noise-band entries are blocked.'
    return 'healthy', 'Bot process recovered; inspect the dashboard for session and feed status.'


def read_health(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=4) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def notify_change(key, message, state_path, send):
    state_path = Path(state_path)
    with file_lock(str(state_path) + '.lock'):
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
        if previous.get('key') == key:
            return False
        # First healthy check is quiet; failures and recovery transitions notify.
        if key != 'healthy' or previous:
            send('Options Desk PAPER: ' + message)
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'key': key, 'time': datetime.now(IST).isoformat()}))
        temporary.chmod(0o600)
        temporary.replace(state_path)
        return key != 'healthy' or bool(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--backups', type=Path)
    args = parser.parse_args()
    load_env(Path(os.getenv('BOT_ENV_FILE', str(ROOT / '.env'))))
    config = Config.from_env()
    if config.mode != 'paper':
        raise SystemExit('This monitor is for the paper trial only')
    key, message = assess(read_health(config.port), datetime.now(IST))
    if key == 'healthy' and config.db_path.parent.exists():
        if shutil.disk_usage(config.db_path.parent).free < 1024**3:
            key, message = 'storage_low', 'Less than 1 GiB of database storage remains.'
        elif args.backups:
            files = list(args.backups.glob('*.sqlite3'))
            newest = max((p.stat().st_mtime for p in files), default=0)
            if datetime.now(IST).timestamp() - newest > 96 * 3600:
                key, message = 'backup_stale', 'No verified local SQLite backup in the last 96 hours.'
    if args.dry_run:
        print(json.dumps({'status': key, 'message': message, 'delivered': False}))
        return
    try:
        notified = notify_change(key, message, args.state,
            lambda text: telegram(text, os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('TELEGRAM_CHAT_ID', '')))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps({'status': key, 'notified': notified}))


if __name__ == '__main__':
    main()
