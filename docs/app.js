(() => {
  'use strict';
  const REFRESH_MS = 60_000;
  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n, d = 2) => (n == null || isNaN(n) ? '—' : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const dir = (n) => (n > 0 ? 'up' : n < 0 ? 'down' : 'flat');
  const signed = (n) => (n == null ? '—' : `${n > 0 ? '+' : ''}${fmt(n)}`);
  const timeStr = (ts) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—');

  async function getJSON(url) {
    const r = await fetch(`${url}?t=${Date.now()}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

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
        <div><div class="ticker">${esc(s.ticker)}</div><div class="name" title="${esc(s.long_name || s.name)}">${esc(s.name)}</div></div>
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
      </div>
      ${chart(sessionPoints(hist, s), s.previous_close, color)}
      <div class="updated">Quote: ${timeStr(s.market_time)}${state} · ${esc(s.currency || '')}</div>
      ${s.error ? `<div class="error">${esc(s.error)}</div>` : ''}
    </article>`;
  }

  async function load() {
    try {
      const [latest, history] = await Promise.all([
        getJSON('data/latest.json'),
        getJSON('data/history.json').catch(() => ({ tickers: {} })),
      ]);
      const stocks = latest.stocks || [];
      const hist = history.tickers || {};
      $('#grid').innerHTML = stocks.length ? stocks.map((s) => card(s, hist[s.ticker])).join('') : '<p>No stocks yet. Add one to <code>stocks.json</code>.</p>';
      const hot = stocks.filter((s) => s.over_threshold && !s.stale);
      const al = $('#alerts');
      al.hidden = !hot.length;
      al.innerHTML = hot.length ? `⚠️ Over threshold now: ${hot.map((s) => `<b>${esc(s.ticker)}</b> ${signed(s.change_pct)}%`).join(' · ')}` : '';
      const gen = latest.generated_at ? new Date(latest.generated_at) : null;
      $('#meta').textContent = gen ? `Last updated ${gen.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}` : '';
    } catch (e) {
      $('#meta').textContent = `Could not load data: ${e.message}`;
    }
  }

  load();
  setInterval(load, REFRESH_MS);
})();
