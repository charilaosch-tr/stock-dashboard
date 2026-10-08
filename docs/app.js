(() => {
  'use strict';
  const REFRESH_MS = 60_000;
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n, d = 2) => (n == null || isNaN(n) ? '—' : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const dir = (n) => (n > 0 ? 'up' : n < 0 ? 'down' : 'flat');
  const signed = (n, d = 2) => (n == null || isNaN(n) ? '—' : `${n > 0 ? '+' : ''}${fmt(n, d)}`);
  const timeStr = (ts) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—');
  const quoteUrl = (t) => `https://finance.yahoo.com/quote/${encodeURIComponent(t)}`;

  function compact(n) {
    if (n == null || isNaN(n)) return '—';
    const a = Math.abs(n);
    if (a >= 1e12) return `${(n / 1e12).toFixed(2)}T`;
    if (a >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
    if (a >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
    if (a >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
    return String(Math.round(n));
  }
  const money = (n) => (n == null ? '—' : `$${compact(n)}`);
  const pctPill = (p) => `<span class="pct-pill ${dir(p)}">${p == null ? '—' : `${p > 0 ? '▲ ' : p < 0 ? '▼ ' : ''}${signed(p)}%`}</span>`;
  const relVol = (r) => (r == null ? '—' : `${fmt(r, 2)}×`);

  async function getJSON(url) {
    const r = await fetch(`${url}?t=${Date.now()}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  /* ---------- market overview ---------- */
  function renderMarket(m) {
    const el = $('#market');
    const pill = $('#mkt-state');
    const st = (m && m.market_state) || 'unknown';
    pill.textContent = st === 'open' ? 'Market open' : st === 'pre-market' ? 'Pre-market' : st === 'post-market' ? 'After hours' : st === 'closed' ? 'Market closed' : 'Market —';
    pill.className = `pill ${st}`;
    const idx = (m && m.indices) || [];
    if (!idx.length) {
      el.innerHTML = `<div class="market-empty">Market overview unavailable${m && m.error ? ` (${esc(m.error)})` : ''}.</div>`;
      return;
    }
    el.innerHTML = idx.map((i) => {
      const d = i.ticker === '^VIX' ? dir(-(i.change_pct || 0)) : dir(i.change_pct);
      // For VIX: rising = fear, show in red
      return `<a class="idx" href="${quoteUrl(i.ticker)}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">
        <div class="iname">${esc(i.name)}</div>
        <div class="iprice">${i.error ? '—' : fmt(i.price)}</div>
        <div class="ichg ${d}">${i.error ? esc(i.error) : `${signed(i.change)} (${signed(i.change_pct)}%)`}</div>
      </a>`;
    }).join('');
  }

  /* ---------- watchlist ---------- */
  function chart(points, prevClose, color) {
    if (!points || points.length < 2) return '<div class="updated">No intraday history yet.</div>';
    const W = 300, H = 90, P = 4;
    const ys = points.map((p) => p[1]).concat(prevClose ? [prevClose] : []);
    const t0 = points[0][0], t1 = points[points.length - 1][0];
    let lo = Math.min(...ys), hi = Math.max(...ys);
    if (hi === lo) { hi += 1; lo -= 1; }
    const x = (t) => P + ((t - t0) / Math.max(1, t1 - t0)) * (W - 2 * P);
    const y = (v) => H - P - ((v - lo) / (hi - lo)) * (H - 2 * P);
    const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('');
    const area = `${line}L${x(t1).toFixed(1)},${H - P}L${x(t0).toFixed(1)},${H - P}Z`;
    const base = prevClose
      ? `<line x1="${P}" x2="${W - P}" y1="${y(prevClose).toFixed(1)}" y2="${y(prevClose).toFixed(1)}" stroke="currentColor" stroke-opacity=".35" stroke-dasharray="3 3"/>`
      : '';
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Intraday price chart">
      ${base}
      <path d="${area}" fill="${color}" fill-opacity=".12"/>
      <path d="${line}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
    </svg>`;
  }

  function sessionPoints(all, s) {
    if (!all || !all.length) return [];
    if (s.session_start) {
      const pts = all.filter((p) => p[0] >= s.session_start && (!s.session_end || p[0] <= s.session_end));
      if (pts.length >= 2) return pts;
    }
    return all.slice(-80);
  }

  function rangeBlock(s) {
    const lo = s.fifty_two_week_low, hi = s.fifty_two_week_high;
    let pos = s.fifty_two_week_pos;
    if (pos == null && lo != null && hi != null && hi > lo && s.price != null) pos = ((s.price - lo) / (hi - lo)) * 100;
    if (pos == null) return '<div>52-wk range<b>—</b></div>';
    const p = Math.max(0, Math.min(100, pos));
    return `<div style="grid-column: 1 / -1">52-wk range · ${fmt(p, 0)}% of range
      <div class="range-bar" title="Low ${fmt(lo)} · High ${fmt(hi)}"><i style="left:${p}%"></i></div>
      <div style="display:flex;justify-content:space-between"><b style="display:inline">${fmt(lo)}</b><b style="display:inline">${fmt(hi)}</b></div>
    </div>`;
  }

  function card(s, hist) {
    const d = dir(s.change_pct);
    const color = getComputedStyle(document.documentElement).getPropertyValue(d === 'down' ? '--down' : d === 'up' ? '--up' : '--flat').trim();
    let badge = '';
    if (s.error) badge = '<span class="badge err">Fetch error</span>';
    else if (s.over_threshold && !s.stale) badge = '<span class="badge">Over threshold</span>';
    else if (s.over_threshold) badge = '<span class="badge muted">Over (last session)</span>';
    const state = s.market_state ? ` · ${esc(s.market_state)}` : '';
    return `<article class="card ${s.over_threshold && !s.stale ? 'hot' : ''}">
      <div class="head">
        <div><div class="ticker"><a href="${quoteUrl(s.ticker)}" target="_blank" rel="noopener">${esc(s.ticker)}</a></div><div class="name" title="${esc(s.long_name || s.name)}">${esc(s.name)}</div></div>
        ${badge}
      </div>
      <div class="price-row">
        <span class="price">${fmt(s.price)}</span>
        <span class="chg ${d}">${s.change_pct == null ? '—' : `${signed(s.change_pct)}%`}</span>
        <span class="${d}">${signed(s.change)}</span>
      </div>
      <div class="details">
        <div>Prev close<b>${fmt(s.previous_close)}</b></div>
        <div>Threshold<b>±${fmt(s.threshold, s.threshold < 1 ? 2 : 1)}%</b></div>
        <div>Day range<b>${s.day_low != null ? `${fmt(s.day_low)}–${fmt(s.day_high)}` : '—'}</b></div>
        <div>Volume<b>${compact(s.volume)}</b></div>
        <div>Vol vs avg<b>${relVol(s.rel_volume)}</b></div>
        <div>Market cap<b>${money(s.market_cap)}</b></div>
        ${rangeBlock(s)}
      </div>
      ${chart(sessionPoints(hist, s), s.previous_close, color)}
      <div class="updated">Quote: ${timeStr(s.market_time)}${state} · ${esc(s.currency || '')}${s.avg_volume ? ` · avg vol ${compact(s.avg_volume)}` : ''}</div>
      ${s.error ? `<div class="error">${esc(s.error)}</div>` : ''}
    </article>`;
  }

  /* ---------- sortable tables ---------- */
  const COLS = [
    { key: 'rank', label: '#', num: true, render: (r) => r.rank },
    { key: 'ticker', label: 'Ticker', render: (r) => `<a href="${quoteUrl(r.ticker)}" target="_blank" rel="noopener">${esc(r.ticker)}</a>` },
    { key: 'name', label: 'Name', render: (r) => `<span class="name-cell" title="${esc(r.long_name || r.name)}">${esc(r.name)}</span>` },
    { key: 'price', label: 'Price', num: true, render: (r) => fmt(r.price) },
    { key: 'change_pct', label: '% Chg', num: true, render: (r) => pctPill(r.change_pct) },
    { key: 'volume', label: 'Volume', num: true, render: (r) => compact(r.volume) },
    { key: 'rel_volume', label: 'Rel vol', num: true, render: (r) => relVol(r.rel_volume) },
    { key: 'market_cap', label: 'Mkt cap', num: true, render: (r) => money(r.market_cap) },
  ];

  const tables = {};
  function makeTable(id, defaultSort) {
    const t = { id, rows: [], sort: defaultSort };
    const el = document.getElementById(id);
    el.querySelector('thead').innerHTML = `<tr>${COLS.map((c) => `<th data-key="${c.key}" scope="col">${c.label}</th>`).join('')}</tr>`;
    el.querySelectorAll('th').forEach((th) => th.addEventListener('click', () => {
      const key = th.dataset.key;
      if (t.sort.key === key) t.sort.dir = t.sort.dir === 'asc' ? 'desc' : 'asc';
      else t.sort = { key, dir: COLS.find((c) => c.key === key).num ? 'desc' : 'asc' };
      renderTable(t);
    }));
    tables[id] = t;
    return t;
  }

  function sortRows(rows, { key, dir: d }) {
    const mul = d === 'asc' ? 1 : -1;
    return rows.slice().sort((a, b) => {
      let va = a[key], vb = b[key];
      if (key === 'change_pct_abs') { va = Math.abs(a.change_pct || 0); vb = Math.abs(b.change_pct || 0); }
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'string') return va.localeCompare(vb) * mul;
      return (va - vb) * mul;
    });
  }

  function renderTable(t) {
    const el = document.getElementById(t.id);
    el.querySelectorAll('th').forEach((th) => {
      const on = th.dataset.key === t.sort.key;
      th.classList.toggle('sorted', on);
      const c = COLS.find((x) => x.key === th.dataset.key);
      th.textContent = c.label + (on ? (t.sort.dir === 'asc' ? ' ▲' : ' ▼') : '');
    });
    const rows = sortRows(t.rows, t.sort);
    el.querySelector('tbody').innerHTML = rows.length
      ? rows.map((r) => `<tr>${COLS.map((c) => `<td>${c.render(r)}</td>`).join('')}</tr>`).join('')
      : `<tr><td colspan="${COLS.length}" style="text-align:center;color:var(--muted)">No data yet.</td></tr>`;
  }

  const activesT = makeTable('actives-table', { key: 'rank', dir: 'asc' });
  const moversT = makeTable('movers-table', { key: 'rank', dir: 'asc' });
  let moversData = null;
  let moversMode = 'all';

  function withRank(rows) { return (rows || []).map((r, i) => ({ ...r, rank: i + 1 })); }

  function renderMovers() {
    if (!moversData) return;
    moversT.rows = withRank(moversData[moversMode]);
    renderTable(moversT);
    const f = moversData.filters || {};
    $('#movers-note').textContent = moversData.error
      ? `Movers unavailable: ${moversData.error}`
      : `${moversT.rows.length} stocks · ${(moversData.source || '')} · as of ${asOf(moversData)}`;
  }

  function asOf(d) {
    const rows = (d.stocks || d.all || []);
    const qt = rows.map((r) => r.quote_time).filter(Boolean).sort().pop();
    return qt ? timeStr(qt) : (d.generated_at ? new Date(d.generated_at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—');
  }

  $$('.tog').forEach((b) => b.addEventListener('click', () => {
    moversMode = b.dataset.movers;
    $$('.tog').forEach((x) => x.classList.toggle('active', x === b));
    moversT.sort = { key: 'rank', dir: 'asc' };
    renderMovers();
  }));

  async function load() {
    try {
      const [latest, history, market, actives, movers] = await Promise.all([
        getJSON('data/latest.json'),
        getJSON('data/history.json').catch(() => ({ tickers: {} })),
        getJSON('data/market.json').catch((e) => ({ error: e.message, indices: [] })),
        getJSON('data/actives.json').catch((e) => ({ error: e.message, stocks: [] })),
        getJSON('data/movers.json').catch((e) => ({ error: e.message, all: [], gainers: [], losers: [] })),
      ]);

      renderMarket(market);

      const stocks = latest.stocks || [];
      const hist = history.tickers || {};
      $('#grid').innerHTML = stocks.length ? stocks.map((s) => card(s, hist[s.ticker])).join('') : '<p>No stocks yet. Add one to <code>stocks.json</code>.</p>';
      const hot = stocks.filter((s) => s.over_threshold && !s.stale);
      const al = $('#alerts');
      al.hidden = !hot.length;
      al.innerHTML = hot.length ? `⚠️ Over threshold now: ${hot.map((s) => `<b>${esc(s.ticker)}</b> ${signed(s.change_pct)}%`).join(' · ')}` : '';

      activesT.rows = withRank(actives.stocks);
      renderTable(activesT);
      $('#actives-note').textContent = actives.error
        ? `Volume leaders unavailable: ${actives.error}`
        : `${activesT.rows.length} stocks · ${actives.source || ''} · as of ${asOf(actives)}`;

      moversData = movers;
      renderMovers();

      const gen = latest.generated_at ? new Date(latest.generated_at) : null;
      $('#meta').textContent = gen ? `Last updated ${gen.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}` : '';
    } catch (e) {
      $('#meta').textContent = `Could not load data: ${e.message}`;
    }
  }

  load();
  setInterval(load, REFRESH_MS);
})();
