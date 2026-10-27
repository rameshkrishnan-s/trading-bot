"""Regular NSE F&O sessions only. Unknown years and special sessions fail closed.

2026 source: https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf
Checked 2026-10-15. Review subsequent exchange amendments before deployment.
Muhurat (2026-11-08) is deliberately unsupported by the regular-session ORB.
"""
from datetime import time, timedelta
from app.market import IST

HOLIDAYS = {2026: frozenset({
    '2026-01-26', '2026-03-03', '2026-03-26', '2026-03-31',
    '2026-04-03', '2026-04-14', '2026-05-01', '2026-05-28',
    '2026-06-26', '2026-09-14', '2026-10-02', '2026-10-20',
    '2026-11-10', '2026-11-24', '2026-12-25',
})}


def session(now):
    now = now.astimezone(IST)
    if now.year not in HOLIDAYS:
        return {'known': False, 'trading_day': False, 'open': False,
                'reason': 'NSE calendar needs updating for this year'}
    trading_day = now.weekday() < 5 and now.date().isoformat() not in HOLIDAYS[now.year]
    opened = trading_day and time(9, 15) <= now.time() <= time(15, 30)
    return {'known': True, 'trading_day': trading_day, 'open': opened,
            'reason': 'NSE regular session open' if opened else 'NSE regular session closed'}


def previous_trading_day(day):
    """Latest regular session before `day`; None when the calendar does not cover it."""
    for back in range(1, 15):
        candidate = day - timedelta(days=back)
        if candidate.year not in HOLIDAYS:
            return None
        if candidate.weekday() < 5 and candidate.isoformat() not in HOLIDAYS[candidate.year]:
            return candidate
    return None
