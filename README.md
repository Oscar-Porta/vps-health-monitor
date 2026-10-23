# VPS Health Monitor

Lightweight monitoring for the `btc-ml-signal-engine` production server: a **daily infrastructure health check** and a **weekly model-drift monitor**, both reporting to a Telegram alerts channel via cron.

> **Status: archived.** Ran on the production VPS until the system was decommissioned. Published as a portfolio piece.

## `src/health_check.py` — daily (cron)

Nine checks, one Telegram report:

1. systemd services active (engine, position monitor, dashboard)
2. errors in the service logs over the last 24h
3. ownership/permissions of critical files (a recurring production bug: files created as `root` but read by the service user)
4. time since the engine's last hourly check
5. open positions
6. disk usage
7. memory usage
8. Binance API reachable
9. **Supabase reachable + keep-alive**: free-tier Supabase projects pause after 7 days without activity. The daily read-only query both verifies the database and resets the inactivity timer, so it doubles as the keep-alive.

Checks without credentials configured are skipped instead of raising false alarms.

## `src/model_drift.py` — weekly (cron)

Detects whether the deployed model still behaves like it did in validation:

- **Probability drift:** median predicted probability and share of signals above the 0.60 threshold, compared with the training-set reference.
- **Performance drift:** rolling win rate of the last 30 closed trades above the threshold, against the walk-forward expectation (42.7%) with a ±2σ binomial band.
- **Execution drift:** limit-order fill rate against the 98.4% measured in the backtest. If fills drop, real trading costs rise and the strategy loses its edge.

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env

# crontab
0 5 * * *  cd /path/to/vps-health-monitor && .venv/bin/python src/health_check.py >> health_check.log 2>&1
0 8 * * 1  cd /path/to/vps-health-monitor && .venv/bin/python src/model_drift.py  >> model_drift.log  2>&1
```

Python · requests · pandas · cron · systemd · Telegram Bot API. Code comments and reports are in Spanish.
