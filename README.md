# PAPER Signal

Watches Papertrade's public stats every 30 minutes and tells you when farming
PAPER (taking losses to mint it, then staking) is actually worth it.

- **Telegram bot**: messages you when the signal changes (WAIT / WATCH / FARM),
  every 6 hours while it stays on FARM, when the LP drops below / returns to the
  $5M cap, when it crosses the $2M reset line, a 9am daily heartbeat, and a
  warning if Papertrade's stats can't be reached.
- **Live dashboard**: GitHub Pages site with the signal, payback, mint rate,
  rewards, reset distance and history charts. Refreshes itself every minute.

Read-only. It never connects a wallet, signs anything or places a trade.

## Files

| File | What it does |
|---|---|
| `paper_farm_signal.py` | The model: mint rate, cost per PAPER, reward decay, dilution, payback, signal. Also runs on its own from Terminal. |
| `notify.py` | Runs the model, updates the dashboard data, sends Telegram messages. |
| `dashboard_data.py` | Writes `docs/data.json` (30 days of checks + 7 days of protocol history). |
| `docs/index.html` | The dashboard page. |
| `.github/workflows/paper-signal.yml` | The 15-minute schedule on GitHub's servers. |

## Settings (optional)

Repo → Settings → Secrets and variables → Actions → **Variables** tab:

- `LOSS_USD`: loss to model, default `1000`
- `TARGET_DAYS`: payback you'd accept, default `30`

## Signals

- **WAIT**: payback is longer than your target. Don't farm.
- **WATCH**: not worth it yet, but a ~1% crowd-favourable price move would drain
  the LP below $2M and reset the mint rate to 100 PAPER/$1 (about 8x cheaper).
- **FARM**: modelled payback is inside your target. A model, not a promise.

## Run it locally

```
python3 paper_farm_signal.py                      # one-off check
python3 paper_farm_signal.py --loss 500 --target-days 45
```
