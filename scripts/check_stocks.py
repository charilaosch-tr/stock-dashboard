#!/usr/bin/env python3
"""Fetch watchlist prices, market overview, and US stock screener lists.

Stdlib only. Data source: Yahoo Finance public endpoints (no API key).

Outputs under docs/data/ (served by GitHub Pages):
  latest.json       watchlist snapshot (+ enrichment)
  history.json      rolling intraday history per watchlist ticker
  alerts.json       over-threshold alerts; notify=true opens/comments an issue
  alert_state.json  what was already notified today (anti-spam)
  market.json       S&P 500 / Nasdaq / Dow / Russell 2000 / VIX + market state
  actives.json      top US stocks by share volume
  movers.json       biggest absolute % movers (filtered), plus gainers/losers

Watchlist alerts are independent of screener/market fetches: those failures
are logged and written as empty/error payloads, but never fail the run.
"""
from __future__ import annotations

import http.cookiejar
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
MARKET_FILE = DATA_DIR / "market.json"
ACTIVES_FILE = DATA_DIR / "actives.json"
MOVERS_FILE = DATA_DIR / "movers.json"

MAX_POINTS_PER_TICKER = 400
HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
TIMEOUT = 20
RETRIES = 3

# US equity exchange codes commonly returned by Yahoo screeners
US_EXCHANGES = frozenset({"NYQ", "NYS", "NMS", "NGM", "NCM", "ASE", "PCX", "BTS", "YHD"})
INDEX_SYMBOLS = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq Composite"),
    ("^DJI", "Dow Jones"),
    ("^RUT", "Russell 2000"),
    ("^VIX", "VIX"),
]
MOVER_MIN_MCAP = 2_000_000_000  # $2B
MOVER_MIN_PRICE = 5.0
SCREENER_COUNT = 100
TABLE_TOP_N = 20


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
    if isinstance(raw, list):
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


class YahooClient:
    """HTTP client with browser UA + optional crumb/cookie for quote API."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.crumb: str | None = None
        self._crumb_tried = False

    def _request(self, url: str, data: bytes | None = None, method: str | None = None) -> dict:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=TIMEOUT) as resp:
            return json.load(resp)

    def ensure_crumb(self) -> str | None:
        if self.crumb or self._crumb_tried:
            return self.crumb
        self._crumb_tried = True
        try:
            # Seed cookies, then fetch crumb
            try:
                self._request("https://fc.yahoo.com/")
            except Exception:
                pass
            for host in HOSTS:
                try:
                    # getcrumb returns plain text, not JSON
                    url = f"https://{host}/v1/test/getcrumb"
                    req = urllib.request.Request(
                        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
                    )
                    with self.opener.open(req, timeout=TIMEOUT) as resp:
                        crumb = resp.read().decode("utf-8", errors="replace").strip()
                    if crumb and "<" not in crumb and len(crumb) < 80:
                        self.crumb = crumb
                        log(f"Yahoo crumb acquired via {host}")
                        return self.crumb
                except Exception as e:
                    log(f"crumb via {host} failed: {e}")
        except Exception as e:
            log(f"crumb setup failed: {e}")
        return None

    def get_json(self, path: str, query: dict | None = None, post: dict | None = None) -> dict:
        q = dict(query or {})
        last_err: Exception | None = None
        for attempt in range(RETRIES):
            crumb = self.ensure_crumb() if ("/v7/finance/quote" in path or post is not None) else self.crumb
            if crumb and "crumb" not in q:
                q = {**q, "crumb": crumb}
            qs = urllib.parse.urlencode(q) if q else ""
            body = None if post is None else json.dumps(post).encode("utf-8")
            method = "POST" if body is not None else None
            for host in HOSTS:
                url = f"https://{host}{path}"
                if qs:
                    url = f"{url}?{qs}"
                try:
                    return self._request(url, data=body, method=method)
                except urllib.error.HTTPError as e:
                    last_err = e
                    if e.code in (401, 403) and not self._crumb_tried:
                        self.ensure_crumb()
                    if e.code == 404:
                        raise
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                    last_err = e
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"request failed for {path}: {last_err}")


def fetch_chart(client: YahooClient, ticker: str) -> dict:
    query = {"interval": "5m", "range": "1d"}
    path = f"/v8/finance/chart/{urllib.parse.quote(ticker, safe='')}"
    # Chart works without crumb; use opener for consistent UA/cookies
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        for host in HOSTS:
            url = f"https://{host}{path}?{urllib.parse.urlencode(query)}"
            try:
                payload = client._request(url)
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
    raise RuntimeError(f"chart fetch failed after {RETRIES} rounds: {last_err}")


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
        "volume": meta.get("regularMarketVolume"),
        "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
        "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
        "session_start": session.get("start"),
        "session_end": session.get("end"),
        "bars": bars,
    }


def merge_history(history: dict, ticker: str, bars: list, price: float, ts: int) -> list:
    points = {int(t): p for t, p in history.get(ticker, [])}
    for t, p in bars:
        points[t] = p
    points[ts] = round(price, 4)
    merged = sorted(points.items())[-MAX_POINTS_PER_TICKER:]
    return [[t, p] for t, p in merged]


def write_github_output(values: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")


def _num(v):
    if v is None:
        return None
    if isinstance(v, dict) and "raw" in v:
        v = v["raw"]
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def quote_row(q: dict) -> dict:
    price = _num(q.get("regularMarketPrice"))
    prev = _num(q.get("regularMarketPreviousClose"))
    chg = _num(q.get("regularMarketChange"))
    pct = _num(q.get("regularMarketChangePercent"))
    if chg is None and price is not None and prev:
        chg = price - prev
    if pct is None and price is not None and prev:
        pct = (price - prev) / prev * 100.0
    vol = _num(q.get("regularMarketVolume"))
    avg = _num(q.get("averageDailyVolume3Month")) or _num(q.get("averageDailyVolume10Day"))
    rel = round(vol / avg, 2) if vol and avg else None
    hi = _num(q.get("fiftyTwoWeekHigh"))
    lo = _num(q.get("fiftyTwoWeekLow"))
    pos = None
    if price is not None and hi is not None and lo is not None and hi > lo:
        pos = round((price - lo) / (hi - lo) * 100.0, 1)
    return {
        "ticker": q.get("symbol"),
        "name": q.get("shortName") or q.get("longName") or q.get("displayName") or q.get("symbol"),
        "long_name": q.get("longName") or q.get("shortName"),
        "price": round(price, 4) if price is not None else None,
        "previous_close": round(prev, 4) if prev is not None else None,
        "change": round(chg, 4) if chg is not None else None,
        "change_pct": round(pct, 4) if pct is not None else None,
        "volume": int(vol) if vol is not None else None,
        "avg_volume": int(avg) if avg is not None else None,
        "rel_volume": rel,
        "market_cap": int(_num(q.get("marketCap"))) if _num(q.get("marketCap")) is not None else None,
        "fifty_two_week_high": hi,
        "fifty_two_week_low": lo,
        "fifty_two_week_pos": pos,
        "exchange": q.get("exchange") or q.get("fullExchangeName"),
        "currency": q.get("currency") or "USD",
        "market_state": (q.get("marketState") or "").lower().replace("pre", "pre-market")
            .replace("regular", "open").replace("post", "post-market")
            .replace("closed", "closed") or None,
        "quote_time": q.get("regularMarketTime"),
    }


def is_us_equity(q: dict) -> bool:
    if (q.get("quoteType") or "EQUITY") != "EQUITY":
        return False
    ex = q.get("exchange") or ""
    if ex in US_EXCHANGES:
        return True
    # Fallback: USD + US region
    if q.get("region") == "US" and (q.get("currency") or "USD") == "USD":
        return True
    return False


def fetch_quotes(client: YahooClient, symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    # Batch to stay under URL limits
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 40):
        batch = symbols[i:i + 40]
        payload = client.get_json(
            "/v7/finance/quote",
            {"symbols": ",".join(batch), "fields": ",".join([
                "symbol", "shortName", "longName", "displayName",
                "regularMarketPrice", "regularMarketChange", "regularMarketChangePercent",
                "regularMarketPreviousClose", "regularMarketVolume",
                "averageDailyVolume3Month", "averageDailyVolume10Day",
                "marketCap", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
                "exchange", "fullExchangeName", "currency", "marketState",
                "regularMarketTime", "quoteType", "region",
            ])},
        )
        results = (payload.get("quoteResponse") or {}).get("result") or []
        for q in results:
            if q.get("symbol"):
                out[q["symbol"]] = q
    return out


def fetch_predefined_screener(client: YahooClient, scr_id: str, count: int = SCREENER_COUNT) -> list[dict]:
    payload = client.get_json(
        "/v1/finance/screener/predefined/saved",
        {"scrIds": scr_id, "count": str(count)},
    )
    finance = payload.get("finance") or {}
    if finance.get("error"):
        raise RuntimeError(finance["error"])
    results = finance.get("result") or []
    if not results:
        return []
    return results[0].get("quotes") or []


def market_state_from_quotes(quotes: list[dict], now_ts: int) -> str:
    # Prefer S&P 500 marketState
    for q in quotes:
        ms = (q.get("marketState") or "").upper()
        if ms == "REGULAR":
            return "open"
        if ms in ("PRE", "PREPRE"):
            return "pre-market"
        if ms in ("POST", "POSTPOST"):
            return "post-market"
        if ms == "CLOSED":
            return "closed"
    # Fallback by New York clock on weekdays
    try:
        ny = datetime.now(ZoneInfo("America/New_York"))
        if ny.weekday() >= 5:
            return "closed"
        mins = ny.hour * 60 + ny.minute
        if 4 * 60 <= mins < 9 * 60 + 30:
            return "pre-market"
        if 9 * 60 + 30 <= mins < 16 * 60:
            return "open"
        if 16 * 60 <= mins < 20 * 60:
            return "post-market"
    except Exception:
        pass
    return "closed"


def build_market(client: YahooClient, generated: str) -> dict:
    symbols = [s for s, _ in INDEX_SYMBOLS]
    names = {s: n for s, n in INDEX_SYMBOLS}
    try:
        raw = fetch_quotes(client, symbols)
        indices = []
        for sym in symbols:
            q = raw.get(sym)
            if not q:
                # Chart fallback for a missing index
                try:
                    ch = parse_chart(fetch_chart(client, sym), int(time.time()))
                    indices.append({
                        "ticker": sym,
                        "name": names[sym],
                        "price": ch["price"],
                        "change": ch["change"],
                        "change_pct": ch["change_pct"],
                        "previous_close": ch["previous_close"],
                        "market_state": ch["market_state"],
                        "quote_time": ch["market_time"],
                    })
                except Exception as e:
                    log(f"[market] {sym} failed: {e}")
                    indices.append({"ticker": sym, "name": names[sym], "error": str(e)})
                continue
            row = quote_row(q)
            row["name"] = names[sym]
            # Normalize market_state from Yahoo's PRE/REGULAR/CLOSED
            ms = (q.get("marketState") or "").upper()
            row["market_state"] = {
                "REGULAR": "open", "PRE": "pre-market", "PREPRE": "pre-market",
                "POST": "post-market", "POSTPOST": "post-market", "CLOSED": "closed",
            }.get(ms, row.get("market_state") or "closed")
            indices.append(row)
        state = market_state_from_quotes(list(raw.values()), int(time.time()))
        return {
            "generated_at": generated,
            "source": "Yahoo Finance quote",
            "market_state": state,
            "indices": indices,
            "error": None,
        }
    except Exception as e:
        log(f"[market] ERROR: {e}")
        return {
            "generated_at": generated,
            "source": "Yahoo Finance quote",
            "market_state": "unknown",
            "indices": [],
            "error": str(e),
        }


def build_actives(client: YahooClient, generated: str) -> dict:
    try:
        quotes = fetch_predefined_screener(client, "most_actives", SCREENER_COUNT)
        rows = [quote_row(q) for q in quotes if is_us_equity(q)]
        rows = [r for r in rows if r.get("ticker")]
        # Already sorted by volume from screener; re-sort defensively
        rows.sort(key=lambda r: r.get("volume") or 0, reverse=True)
        top = rows[:TABLE_TOP_N]
        return {
            "generated_at": generated,
            "source": "Yahoo Finance screener:most_actives",
            "count": len(top),
            "stocks": top,
            "error": None,
        }
    except Exception as e:
        log(f"[actives] ERROR: {e}")
        return {
            "generated_at": generated,
            "source": "Yahoo Finance screener:most_actives",
            "count": 0,
            "stocks": [],
            "error": str(e),
        }


def build_movers(client: YahooClient, generated: str) -> dict:
    try:
        gainers_raw = fetch_predefined_screener(client, "day_gainers", SCREENER_COUNT)
        losers_raw = fetch_predefined_screener(client, "day_losers", SCREENER_COUNT)
        seen = set()
        merged = []
        for q in gainers_raw + losers_raw:
            if not is_us_equity(q):
                continue
            sym = q.get("symbol")
            if not sym or sym in seen:
                continue
            price = _num(q.get("regularMarketPrice"))
            mcap = _num(q.get("marketCap"))
            if price is None or price < MOVER_MIN_PRICE:
                continue
            if mcap is None or mcap < MOVER_MIN_MCAP:
                continue
            seen.add(sym)
            merged.append(quote_row(q))

        by_abs = sorted(merged, key=lambda r: abs(r.get("change_pct") or 0), reverse=True)
        gainers = sorted(
            [r for r in merged if (r.get("change_pct") or 0) > 0],
            key=lambda r: r.get("change_pct") or 0,
            reverse=True,
        )
        losers = sorted(
            [r for r in merged if (r.get("change_pct") or 0) < 0],
            key=lambda r: r.get("change_pct") or 0,
        )
        return {
            "generated_at": generated,
            "source": "Yahoo Finance screener:day_gainers+day_losers",
            "filters": {
                "min_market_cap": MOVER_MIN_MCAP,
                "min_price": MOVER_MIN_PRICE,
                "exchanges": sorted(US_EXCHANGES),
            },
            "all": by_abs[:TABLE_TOP_N],
            "gainers": gainers[:TABLE_TOP_N],
            "losers": losers[:TABLE_TOP_N],
            "error": None,
        }
    except Exception as e:
        log(f"[movers] ERROR: {e}")
        return {
            "generated_at": generated,
            "source": "Yahoo Finance screener:day_gainers+day_losers",
            "filters": {
                "min_market_cap": MOVER_MIN_MCAP,
                "min_price": MOVER_MIN_PRICE,
            },
            "all": [],
            "gainers": [],
            "losers": [],
            "error": str(e),
        }


def enrich_watchlist(client: YahooClient, out_stocks: list[dict]) -> None:
    tickers = [s["ticker"] for s in out_stocks if not s.get("error")]
    if not tickers:
        return
    try:
        quotes = fetch_quotes(client, tickers)
    except Exception as e:
        log(f"[enrich] quote batch failed: {e}")
        return
    for s in out_stocks:
        q = quotes.get(s["ticker"])
        if not q:
            # Derive 52w position from chart fields already present
            hi = s.get("fifty_two_week_high")
            lo = s.get("fifty_two_week_low")
            price = s.get("price")
            if price is not None and hi and lo and hi > lo:
                s["fifty_two_week_pos"] = round((price - lo) / (hi - lo) * 100.0, 1)
            continue
        row = quote_row(q)
        for k in (
            "volume", "avg_volume", "rel_volume", "market_cap",
            "fifty_two_week_high", "fifty_two_week_low", "fifty_two_week_pos",
        ):
            if row.get(k) is not None:
                s[k] = row[k]
        if not s.get("long_name") and row.get("long_name"):
            s["long_name"] = row["long_name"]


def main() -> int:
    stocks, settings = load_watchlist()
    try:
        renotify_step = abs(float(settings.get("renotify_step", 0.5)))
    except (TypeError, ValueError):
        renotify_step = 0.5

    client = YahooClient()
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
            q = parse_chart(fetch_chart(client, t), now_ts)
        except Exception as e:
            failures += 1
            log(f"[{t}] ERROR: {e}")
            prev = previous_latest.get(t, {})
            row.update({k: prev.get(k) for k in (
                "price", "previous_close", "change", "change_pct", "currency",
                "market_time", "market_time_iso", "market_date", "market_state",
                "session_start", "session_end", "volume", "fifty_two_week_high",
                "fifty_two_week_low", "market_cap", "avg_volume", "rel_volume",
                "fifty_two_week_pos")})
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

    # Enrichment + market extras — never fail the watchlist run
    try:
        enrich_watchlist(client, out_stocks)
    except Exception as e:
        log(f"[enrich] ERROR: {e}")

    generated = now.isoformat(timespec="seconds")
    save_json(LATEST_FILE, {"generated_at": generated, "source": "Yahoo Finance",
                            "stocks": out_stocks})
    save_json(HISTORY_FILE, {"generated_at": generated,
                             "max_points_per_ticker": MAX_POINTS_PER_TICKER,
                             "tickers": history})
    save_json(ALERTS_FILE, {"generated_at": generated, "alerts": alerts})
    save_json(STATE_FILE, state)

    market = build_market(client, generated)
    actives = build_actives(client, generated)
    movers = build_movers(client, generated)
    save_json(MARKET_FILE, market)
    save_json(ACTIVES_FILE, actives)
    save_json(MOVERS_FILE, movers)
    log(f"[market] state={market.get('market_state')} indices={len(market.get('indices') or [])}"
        f"{' err=' + market['error'] if market.get('error') else ''}")
    log(f"[actives] {actives.get('count', 0)} stocks"
        f"{' err=' + actives['error'] if actives.get('error') else ''}")
    log(f"[movers] all={len(movers.get('all') or [])} "
        f"gainers={len(movers.get('gainers') or [])} "
        f"losers={len(movers.get('losers') or [])}"
        f"{' err=' + movers['error'] if movers.get('error') else ''}")

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
