#!/usr/bin/env python3
"""Fetch prices for every ticker in stocks.json and flag moves over threshold.

Stdlib only. Data source: Yahoo Finance public chart endpoint (no API key).

Outputs (all under docs/data/ so the GitHub Pages dashboard can read them):
  latest.json       current snapshot for every stock
  history.json      rolling intraday price history per ticker (capped)
  alerts.json       tickers over threshold; `notify: true` = open/comment issue
  alert_state.json  what has already been notified today (avoids spam)

If GITHUB_OUTPUT is set, also writes `alert_count`, `notify_count`, `tickers`.
Exit code is 0 even when some tickers fail (errors are recorded per stock);
it is 1 only if every ticker failed, so the workflow surfaces total outages.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STOCKS_FILE = ROOT / "stocks.json"
DATA_DIR = ROOT / "docs" / "data"
LATEST_FILE = DATA_DIR / "latest.json"
HISTORY_FILE = DATA_DIR / "history.json"
ALERTS_FILE = DATA_DIR / "alerts.json"
STATE_FILE = DATA_DIR / "alert_state.json"

MAX_POINTS_PER_TICKER = 400  # ~5 trading sessions of 5-minute bars
HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
TIMEOUT = 15
RETRIES = 3


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        log(f"warning: could not read {path.name} ({e}); starting fresh")
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")
    tmp.replace(path)


def load_watchlist() -> tuple[list[dict], dict]:
    raw = load_json(STOCKS_FILE, None)
    if raw is None:
        sys.exit(f"error: {STOCKS_FILE} is missing or invalid JSON")
    if isinstance(raw, list):  # allow a bare list too
        raw = {"stocks": raw}
    settings = raw.get("settings", {}) or {}
    stocks, seen = [], set()
    for entry in raw.get("stocks", []):
        ticker = str(entry.get("ticker", "")).strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        try:
            threshold = abs(float(entry.get("threshold", 1.0)))
        except (TypeError, ValueError):
            threshold = 1.0
        stocks.append({
            "ticker": ticker,
            "name": str(entry.get("name") or ticker),
            "threshold": threshold,
        })
    if not stocks:
        sys.exit("error: stocks.json has no stocks")
    return stocks, settings


def fetch_chart(ticker: str) -> dict:
    """Return the Yahoo chart `result[0]` object, trying hosts with retries."""
    query = urllib.parse.urlencode({"interval": "5m", "range": "1d"})
    path = f"/v8/finance/chart/{urllib.parse.quote(ticker)}?{query}"
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        for host in HOSTS:
            req = urllib.request.Request(
                f"https://{host}{path}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    payload = json.load(resp)
                chart = payload.get("chart") or {}
                if chart.get("error"):
                    err = chart["error"]
                    raise ValueError(f"{err.get('code')}: {err.get('description')}")
                results = chart.get("result") or []
                if not results:
                    raise ValueError("empty result")
                return results[0]
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 404:
                    raise ValueError("unknown ticker (HTTP 404)") from e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as e:
                last_err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed after {RETRIES} rounds: {last_err}")


def parse_chart(result: dict, now_ts: int) -> dict:
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    if price is None or not prev:
        raise ValueError("price or previous close missing in response")
    price, prev = float(price), float(prev)
    pct = (price - prev) / prev * 100.0

    tz_name = meta.get("exchangeTimezoneName") or "America/New_York"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/New_York")
    market_ts = int(meta.get("regularMarketTime") or now_ts)
    market_date = datetime.fromtimestamp(market_ts, tz).date().isoformat()

    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    reg_start, reg_end = regular.get("start"), regular.get("end")
    # The quote belongs to the current session only once that session opened.
    # Before the open, Yahoo still reports yesterday's close vs. the day before,
    # which we must not alert on again.
    fresh = reg_start is None or market_ts >= int(reg_start)
    if reg_start and now_ts < int(reg_start):
        state = "pre-market"
    elif reg_end and now_ts >= int(reg_end):
        state = "closed"
    elif fresh:
        state = "open"
    else:
        state = "closed"

    session = ((meta.get("tradingPeriods") or [[{}]])[0] or [{}])[0]
    timestamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    bars = [
        [int(t), round(float(c), 4)]
        for t, c in zip(timestamps, closes)
        if t is not None and c is not None
    ]

    return {
        "price": round(price, 4),
        "previous_close": round(prev, 4),
        "change": round(price - prev, 4),
        "change_pct": round(pct, 4),
        "currency": meta.get("currency", "USD"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "long_name": meta.get("longName") or meta.get("shortName"),
        "market_time": market_ts,
        "market_time_iso": datetime.fromtimestamp(market_ts, timezone.utc).isoformat(),
        "market_date": market_date,
        "market_state": state,
        "fresh": fresh,
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "session_start": session.get("start"),
        "session_end": session.get("end"),
        "bars": bars,
    }


def merge_history(history: dict, ticker: str, bars: list, price: float, ts: int) -> list:
    points = {int(t): p for t, p in history.get(ticker, [])}
    for t, p in bars:
        points[t] = p
    points[ts] = round(price, 4)  # latest quote (may be newer than last bar)
    merged = sorted(points.items())[-MAX_POINTS_PER_TICKER:]
    return [[t, p] for t, p in merged]


def write_github_output(values: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")


def main() -> int:
    stocks, settings = load_watchlist()
    try:
        renotify_step = abs(float(settings.get("renotify_step", 0.5)))
    except (TypeError, ValueError):
        renotify_step = 0.5

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    history = load_json(HISTORY_FILE, {})
    history = history.get("tickers", history) if isinstance(history, dict) else {}
    state = load_json(STATE_FILE, {})
    previous_latest = {s["ticker"]: s for s in load_json(LATEST_FILE, {}).get("stocks", [])}

    out_stocks, alerts, failures = [], [], 0
    for s in stocks:
        t = s["ticker"]
        row = dict(s)
        try:
            q = parse_chart(fetch_chart(t), now_ts)
        except Exception as e:  # never let one ticker kill the run
            failures += 1
            log(f"[{t}] ERROR: {e}")
            prev = previous_latest.get(t, {})
            row.update({k: prev.get(k) for k in (
                "price", "previous_close", "change", "change_pct", "currency",
                "market_time", "market_time_iso", "market_date", "market_state",
                "session_start", "session_end")})
            row.update({"over_threshold": False, "error": str(e), "stale": True})
            out_stocks.append(row)
            continue

        history[t] = merge_history(history, t, q.pop("bars"), q["price"], q["market_time"])
        over = abs(q["change_pct"]) >= s["threshold"]
        row.update(q)
        row.update({"over_threshold": over, "error": None, "stale": not q["fresh"]})
        out_stocks.append(row)

        sign = "+" if q["change_pct"] >= 0 else ""
        flag = "OVER THRESHOLD" if over else "ok"
        log(f"[{t}] {q['price']:.2f} vs prev close {q['previous_close']:.2f} "
            f"= {sign}{q['change_pct']:.2f}% (threshold {s['threshold']}%, "
            f"{q['market_state']}{', stale' if not q['fresh'] else ''}) -> {flag}")

        if over and q["fresh"]:
            st = state.get(t) or {}
            if st.get("date") != q["market_date"]:
                notify, reason = True, "first move over threshold today"
            elif abs(q["change_pct"] - float(st.get("last_pct", 0))) >= renotify_step:
                notify, reason = True, f"moved another {renotify_step}% since last alert"
            else:
                notify, reason = False, "already notified; no significant change"
            if notify:
                state[t] = {"date": q["market_date"], "last_pct": q["change_pct"],
                            "notified_at": now.isoformat()}
            alerts.append({
                "ticker": t, "name": s["name"], "threshold": s["threshold"],
                "price": q["price"], "previous_close": q["previous_close"],
                "change_pct": q["change_pct"], "currency": q["currency"],
                "date": q["market_date"], "market_time_iso": q["market_time_iso"],
                "notify": notify, "reason": reason,
                "title": f"{t} moved {sign}{q['change_pct']:.2f}% on {q['market_date']}",
            })

    generated = now.isoformat(timespec="seconds")
    save_json(LATEST_FILE, {"generated_at": generated, "source": "Yahoo Finance",
                            "stocks": out_stocks})
    save_json(HISTORY_FILE, {"generated_at": generated,
                             "max_points_per_ticker": MAX_POINTS_PER_TICKER,
                             "tickers": history})
    save_json(ALERTS_FILE, {"generated_at": generated, "alerts": alerts})
    save_json(STATE_FILE, state)

    to_notify = [a for a in alerts if a["notify"]]
    for a in alerts:
        log(f"ALERT {a['title']} (notify={a['notify']}: {a['reason']})")
    if not alerts:
        log("No alerts: no fresh quote is over its threshold "
            "(stale/pre-market quotes are never alerted).")
    write_github_output({
        "alert_count": len(alerts),
        "notify_count": len(to_notify),
        "tickers": ",".join(a["ticker"] for a in to_notify),
    })
    return 1 if failures == len(stocks) else 0


if __name__ == "__main__":
    sys.exit(main())
