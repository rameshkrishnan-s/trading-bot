"""Read-only audit summary: python3 -m app.session_report path/to/database.sqlite3."""
import argparse
import json
import sqlite3
from pathlib import Path


def report(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                  for name in ('snapshots', 'session_candles', 'market_requests', 'decisions', 'orders') if name in tables}
        closed = [dict(r) for r in db.execute("SELECT opened_at,closed_at,realized FROM positions WHERE status='CLOSED' ORDER BY closed_at")]
        days = [r[0] for r in db.execute('SELECT DISTINCT substr(ts,1,10) FROM snapshots ORDER BY ts')]
        active_days = {r[0] for r in db.execute('SELECT DISTINCT substr(opened_at,1,10) FROM positions')}
        errors = [dict(r) for r in db.execute("SELECT ts,title,detail FROM events WHERE level='error' ORDER BY id DESC LIMIT 30")]
        requests = [dict(r) for r in db.execute('''SELECT endpoint,outcome,count(*) AS attempts,
                    round(avg(duration_ms),2) AS mean_ms,round(max(duration_ms),2) AS max_ms
                    FROM market_requests GROUP BY endpoint,outcome''')] if 'market_requests' in tables else []
        decisions = [dict(r) for r in db.execute('''SELECT json_extract(decision_json,'$.reason') AS reason,
                     count(*) AS polls FROM decisions GROUP BY reason ORDER BY polls DESC''')] if 'decisions' in tables else []
        policy_row = db.execute("SELECT value FROM state WHERE key='policy'").fetchone()
        profits = [r['realized'] for r in closed]
        wins = [p for p in profits if p > 0]
        losses = [p for p in profits if p < 0]
        cumulative = peak = max_drawdown = 0.0
        streak = worst_streak = 0
        for pnl in profits:
            cumulative += pnl
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
            streak = streak + 1 if pnl < 0 else 0
            worst_streak = max(worst_streak, streak)
        performance = {'win_rate_pct': round(100 * len(wins) / len(profits), 2) if profits else None,
                       'net_expectancy_inr': round(sum(profits) / len(profits), 2) if profits else None,
                       'average_win_inr': round(sum(wins) / len(wins), 2) if wins else None,
                       'average_loss_inr': round(sum(losses) / len(losses), 2) if losses else None,
                       'profit_factor': round(sum(wins) / -sum(losses), 3) if losses else None,
                       'closed_trade_drawdown_inr': round(max_drawdown, 2),
                       'longest_losing_streak': worst_streak}
        return {'database': str(path), 'policy': json.loads(policy_row[0]) if policy_row else None,
                'performance': performance, 'recording_counts': counts, 'observed_days': days, 'completed_trades': len(closed),
                'days_without_entries': [d for d in days if d not in active_days],
                'net_closed_pnl_inr': round(sum(r['realized'] for r in closed), 2),
                'recorded_order_fees_inr': db.execute('SELECT round(coalesce(sum(fee),0),2) FROM orders').fetchone()[0],
                'requests': requests, 'decision_reasons': decisions, 'recent_halts': errors,
                'limitations': 'Drawdown here uses closed trades, not intratrade equity. No-trade days can be valid. Old sessions lack request/decision telemetry. '
                                'Request duration is observed; execution delay and partial fills are not simulated. '
                                'Recorded fees follow the recorded policy, not necessarily the new itemized model.'}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = json.dumps(report(args.database), indent=2)
    if args.output:
        args.output.write_text(result + '\n')
    else:
        print(result)


if __name__ == '__main__':
    main()
