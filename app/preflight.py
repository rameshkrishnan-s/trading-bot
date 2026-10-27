"""Read-only deployment checks; never starts an engine or logs credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

from app.config import Config, policy
from app.costs import ITEMIZED
from app.market import IST
from app.server import ROOT, load_env
from app.session_calendar import session
from datetime import datetime


def check(config, cloud=False):
    errors = []
    if config.mode != 'paper' or config.strategy not in {'orb', 'noise'} or config.data_provider not in {'angel', 'upstox'}:
        errors.append('Deployment requires Angel or Upstox ORB or noise paper mode')
    if cloud and not config.control_token:
        errors.append('Cloud dashboard controls require BOT_CONTROL_TOKEN')
    if cloud and config.reviewer_enabled:
        errors.append('Disable the optional desktop AI reviewer for this cloud profile')
    if cloud and ITEMIZED.get(config.paper_fee_model) != config.data_provider:
        errors.append('Cloud trial requires the itemized fee profile for ' + config.data_provider)
    if cloud and config.data_provider == 'upstox' and (config.upstox_pin or config.upstox_totp_secret):
        # Paper needs only the read-only Analytics Token; a TOTP seed grants trading access.
        errors.append('Use the read-only UPSTOX_ACCESS_TOKEN for cloud paper; remove UPSTOX_PIN and UPSTOX_TOTP_SECRET')
    if not session(datetime.now(IST))['known']:
        errors.append('Session calendar needs updating')
    if sys.version_info < (3, 12):
        errors.append('Python 3.12 or newer is required')
    existing_parent = config.db_path.resolve().parent
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    if shutil.disk_usage(existing_parent).free < 1024**3:
        errors.append('Less than 1 GiB of free database storage')
    open_positions = None
    if config.db_path.exists():
        db = sqlite3.connect(config.db_path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                errors.append('Database integrity check failed')
            states = dict(db.execute('SELECT key,value FROM state'))
            recorded = json.loads(states.get('policy', 'null'))
            if recorded and {'cooldown_minutes': 30, 'paper_fee_model': 'legacy', **recorded} != policy(config):
                errors.append('Database policy differs from configuration')
            for field, expected in (('mode', config.mode), ('data_provider', config.data_provider)):
                if field in states and json.loads(states[field]) != expected:
                    errors.append('Database ' + field + ' differs from configuration')
            open_positions = db.execute("SELECT count(*) FROM positions WHERE status='OPEN'").fetchone()[0]
        finally:
            db.close()
    digest = hashlib.sha256()
    for path in sorted((ROOT / 'app').glob('*.py')):
        digest.update(path.name.encode() + path.read_bytes())
    return {'ok': not errors, 'errors': errors, 'mode': config.mode,
            'policy': policy(config), 'open_positions': open_positions,
            'release_sha256': digest.hexdigest(), 'python': sys.version.split()[0],
            'network_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cloud', action='store_true')
    args = parser.parse_args()
    path = Path(os.getenv('BOT_ENV_FILE', str(ROOT / '.env')))
    if path.exists() and path.stat().st_mode & 0o077:
        raise SystemExit('Environment file must be readable only by its owner (chmod 600)')
    load_env(path)
    try:
        result = check(Config.from_env(), args.cloud)
    except (ValueError, sqlite3.Error):
        raise SystemExit('Preflight failed: invalid configuration or unreadable database; inspect locally') from None
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['ok'] else 1)


if __name__ == '__main__':
    main()
