# Stock Move Tracker

Watches a list of stocks every 5 minutes during US market hours. When a stock
moves more than its threshold vs. the previous close, it opens a GitHub issue
so you get notified. A dashboard on GitHub Pages shows:

- **Market overview**: S&P 500, Nasdaq Composite, Dow Jones, Russell 2000, VIX, market state
- **Watchlist** cards: price, move, threshold, intraday chart, 52-week range position,
  volume vs. 3-month average, market cap
- **Top 20 by volume**: most actively traded US stocks today (Yahoo "most actives")
- **Top 20 movers**: biggest % moves today among US stocks with market cap ≥ $2B and
  price ≥ $5, with All / Gainers / Losers toggle

Tables are sortable (click a column header) and tickers link to Yahoo Finance.

Runs entirely on GitHub Actions + GitHub Pages. No servers, no API keys, no
secrets (it only uses the built-in `GITHUB_TOKEN`).

```
stocks.json                       <- your watchlist (edit this)
scripts/check_stocks.py           <- fetches prices, finds moves over threshold (Python stdlib only)
.github/workflows/stock-check.yml <- runs every 5 min, commits data, opens/comments on issues
docs/                             <- dashboard (served by GitHub Pages)
  index.html, app.js, style.css
  data/alert_state.json           <- what was already notified today (committed; prevents spam)
  data/*.json (generated)         <- latest, history, alerts, market, actives, movers:
                                     built each run, deployed with the Pages artifact,
                                     history kept in the Actions cache (not committed)
```

## Setup (one time)

1. Push this folder to a new GitHub repository (branch `main`).
2. **Actions permissions:** Settings → Actions → General → Workflow permissions →
   pick **Read and write permissions** (lets the workflow commit data and open issues).
3. **Pages:** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
   The workflow uploads `docs/` (plus fresh data) and deploys it every run, which avoids
   the ~10 builds/hour limit of branch-based Pages. Your dashboard will be at
   `https://<your-user>.github.io/<repo>/`.
4. **Run it once now:** Actions tab → **Stock check** → **Run workflow**.
   After that it runs on its own every 5 minutes, Mon–Fri, 13:00–21:55 UTC.
5. **Get notified:** click **Watch** on the repo (All Activity, or Custom → Issues).
   Make sure email and/or GitHub Mobile notifications are on in
   https://github.com/settings/notifications.

## Add a stock

Edit `stocks.json` (on github.com, click the file → pencil icon) and add an entry:

```json
{
  "settings": { "renotify_step": 0.5 },
  "stocks": [
    { "ticker": "SPCX", "name": "SpaceX", "threshold": 0.1 },
    { "ticker": "AAPL", "name": "Apple",  "threshold": 1.5 }
  ]
}
```

- `ticker`: the Yahoo Finance symbol (US stocks: the plain ticker, e.g. `NVDA`;
  other markets use a suffix, e.g. `VOD.L`, `SAP.DE`; crypto e.g. `BTC-USD`).
- `name`: label shown on the dashboard.
- `threshold`: percent move vs. previous close that triggers an alert, in either
  direction. `0.1` means ±0.1%.

The new stock shows up on the dashboard after the next run (or run the workflow manually).

## Change a threshold

Change the `threshold` number for that stock in `stocks.json` and commit.
Note: 0.1% is a very small move, so expect an alert most trading days.

## How alerts work

- Each run compares the latest price with the previous close.
- The first time a stock is over its threshold on a trading day, the workflow
  opens an issue titled like **`SPCX moved +0.35% on 2026-10-08`** (label `stock-alert`).
- Later that day, it **comments on the same issue** instead of opening a new one,
  but only when the move has changed by at least `renotify_step` percentage points
  (default `0.5`) since the last alert, so you aren't pinged every 5 minutes.
  Set `"renotify_step": 0.1` (or any number) in `stocks.json` to change this.
- New issues and comments trigger GitHub notifications: email and the GitHub
  mobile app, if you **Watch** the repo. Close issues whenever you like. A closed
  issue for the same day is reopened if a new comment is added.
- Before the market opens, Yahoo still reports yesterday's move. The script marks
  those quotes as stale and never alerts on them.

## Run locally

```bash
python3 scripts/check_stocks.py        # fetch prices, update docs/data/*.json
cd docs && python3 -m http.server 8000 # open http://localhost:8000
```

## Notes

- Data comes from Yahoo Finance's public chart endpoint (free, no key, roughly
  15 min delayed for some exchanges). It's unofficial, so it could change or rate-limit.
  Fetch errors are logged and shown on the dashboard card; one bad ticker doesn't stop the others.
- GitHub's scheduler is best-effort: runs can be delayed several minutes at busy times.
- The workflow commits `docs/data/` on each run that changes data (about 100
  small commits per trading day). This also keeps the repo "active", because GitHub
  pauses scheduled workflows in repos with no activity for 60 days.
- Free-tier Actions minutes: public repos are unlimited. Private repos get
  2,000 min/month, and this uses roughly 2,000–2,500 min/month, so make the repo
  **public** or widen the cron interval (e.g. `*/10`).
