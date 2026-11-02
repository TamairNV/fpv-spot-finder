// site-stats dashboard: renderStats(element, { fetchJson, title }) draws the admin stats page into
// element. fetchJson(url) must return the parsed JSON of GET <base>/data?days=N with your app's auth.
// No dependencies; styles are scoped under .sst and follow light/dark from the page.
const CSS = `
.sst{--sst-ink:var(--text,#e8eef2);--sst-muted:var(--muted,#9aa7b0);--sst-line:var(--line,rgba(255,255,255,.1));--sst-panel:var(--panel-2,rgba(255,255,255,.04));
  --sst-bar:#5cc8ff;--sst-bar2:#a78bfa;--sst-bad:#f87171;font:13px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--sst-ink)}
@media (prefers-color-scheme:light){.sst.sst-auto{--sst-ink:#14191d;--sst-muted:#5b6770;--sst-line:rgba(0,0,0,.1);--sst-panel:rgba(0,0,0,.03);--sst-bar:#0b7cc1;--sst-bar2:#6d4fd8;--sst-bad:#c62828}}
.sst h2{margin:0;font-size:18px}.sst h3{margin:18px 0 8px;font-size:13px;font-weight:650;color:var(--sst-ink)}
.sst-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:space-between}
.sst-top select,.sst-top button{background:var(--sst-panel);color:var(--sst-ink);border:1px solid var(--sst-line);border-radius:8px;padding:4px 8px;font:inherit}
.sst-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-top:12px}
.sst-tile{background:var(--sst-panel);border:1px solid var(--sst-line);border-radius:10px;padding:8px 10px}
.sst-tile b{display:block;font-size:20px;font-weight:650;font-variant-numeric:tabular-nums}.sst-tile span{color:var(--sst-muted);font-size:12px}
.sst-tile.bad b{color:var(--sst-bad)}
.sst-chart{position:relative;background:var(--sst-panel);border:1px solid var(--sst-line);border-radius:10px;padding:8px 8px 4px}
.sst-chart svg{display:block;width:100%;height:120px;overflow:visible}
.sst-chart .ax{font-size:10px;fill:var(--sst-muted)}.sst-chart .grid{stroke:var(--sst-line)}
.sst-chart rect.hit{fill:transparent}.sst-chart rect.hit:hover+rect,.sst-chart g:hover rect.m{opacity:.75}
.sst-tip{position:absolute;pointer-events:none;background:#0d1114;color:#fff;border-radius:6px;padding:4px 7px;font-size:12px;white-space:nowrap;transform:translate(-50%,-100%);display:none;z-index:2}
.sst table{width:100%;border-collapse:collapse;font-size:12.5px}.sst td,.sst th{padding:4px 6px;border-bottom:1px solid var(--sst-line);text-align:left;vertical-align:top}
.sst th{color:var(--sst-muted);font-weight:500}.sst td.n{text-align:right;font-variant-numeric:tabular-nums}
.sst .pbar{height:4px;border-radius:2px;background:var(--sst-bar);margin-top:3px}
.sst .muted{color:var(--sst-muted)}.sst .err{color:var(--sst-bad)}
.sst-scroll{max-height:260px;overflow:auto}
`;

const fmt = (n) => (n == null ? '–' : typeof n === 'number' ? (Math.abs(n) >= 10000 ? `${(n / 1000).toFixed(n >= 1e5 ? 0 : 1)}k` : n.toLocaleString('en-GB')) : String(n));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 129600 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`; };
const short = (d) => new Date(`${d}T12:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });

// One series of bars with a baseline, two gridlines, end labels and a hover tooltip per bar.
function bars(points, { colour = 'var(--sst-bar)', label = (p) => '', xLabel = (p) => '' } = {}) {
  const W = 600, H = 120, pad = { l: 30, r: 4, t: 6, b: 16 };
  const max = Math.max(1, ...points.map((p) => p.v));
  const nice = (() => { const e = 10 ** Math.floor(Math.log10(max)); return Math.ceil(max / e) * e; })();
  const bw = (W - pad.l - pad.r) / points.length, gap = Math.min(2, bw * 0.25);
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - v / nice);
  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">`;
  for (const g of [0.5, 1]) svg += `<line class="grid" x1="${pad.l}" x2="${W - pad.r}" y1="${y(nice * g)}" y2="${y(nice * g)}"/><text class="ax" x="${pad.l - 4}" y="${y(nice * g) + 3}" text-anchor="end">${fmt(nice * g)}</text>`;
  svg += `<line class="grid" x1="${pad.l}" x2="${W - pad.r}" y1="${y(0)}" y2="${y(0)}"/>`;
  points.forEach((p, i) => {
    const x = pad.l + i * bw, h = y(0) - y(p.v), r = Math.min(4, (bw - gap) / 2, h);
    const top = y(p.v), w = bw - gap;
    const path = h > 0 ? `M${x},${y(0)}V${top + r}Q${x},${top} ${x + r},${top}H${x + w - r}Q${x + w},${top} ${x + w},${top + r}V${y(0)}Z` : '';
    svg += `<g data-i="${i}"><rect class="hit" x="${x}" y="${pad.t}" width="${bw}" height="${H - pad.t - pad.b}"/>${path ? `<path class="m" d="${path}" fill="${p.colour || colour}"/>` : ''}</g>`;
  });
  const first = points[0], last = points[points.length - 1];
  if (first) svg += `<text class="ax" x="${pad.l}" y="${H - 3}">${esc(xLabel(first))}</text><text class="ax" x="${W - pad.r}" y="${H - 3}" text-anchor="end">${esc(xLabel(last))}</text>`;
  svg += '</svg>';
  const box = document.createElement('div');
  box.className = 'sst-chart';
  box.innerHTML = `${svg}<div class="sst-tip"></div>`;
  const tip = box.querySelector('.sst-tip');
  box.querySelectorAll('g[data-i]').forEach((g) => {
    g.addEventListener('mouseenter', () => {
      const p = points[+g.dataset.i], r = g.getBoundingClientRect(), b = box.getBoundingClientRect();
      tip.innerHTML = label(p);
      tip.style.display = 'block';
      tip.style.left = `${r.left - b.left + r.width / 2}px`;
      tip.style.top = `${Math.max(14, g.querySelector('path')?.getBoundingClientRect().top - b.top || r.bottom - b.top - 20)}px`;
    });
    g.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  });
  return box;
}

export async function renderStats(root, { fetchJson, title = 'Site stats', days = 30 } = {}) {
  if (!document.getElementById('sst-css')) {
    const st = document.createElement('style');
    st.id = 'sst-css';
    st.textContent = CSS;
    document.head.append(st);
  }
  root.classList.add('sst');
  root.innerHTML = `<div class="sst-top"><h2>${esc(title)}</h2><div><select aria-label="Range">${[7, 30, 90].map((d) => `<option value="${d}"${d === days ? ' selected' : ''}>Last ${d} days</option>`).join('')}</select> <button type="button">Refresh</button></div></div><div class="sst-body"><p class="muted">Loading…</p></div>`;
  const body = root.querySelector('.sst-body');
  const load = async () => {
    const n = +root.querySelector('select').value;
    let d;
    try {
      d = await fetchJson(`data?days=${n}`);
    } catch (e) {
      body.innerHTML = `<p class="err">Couldn’t load the stats (${esc(e.message || e)}).</p>`;
      return;
    }
    draw(body, d, n);
  };
  root.querySelector('select').onchange = load;
  root.querySelector('button').onclick = load;
  await load();
  return { refresh: load };
}

function draw(body, d, n) {
  const today = d.days[d.days.length - 1] || {};
  const sum = (k) => d.days.reduce((a, x) => a + (x[k] || 0), 0);
  const hourReq = d.last_hour.reduce((a, x) => a + x.requests, 0);
  const tile = (label, value, cls = '') => `<div class="sst-tile ${cls}"><b>${esc(fmt(value))}</b><span>${esc(label)}</span></div>`;
  const up = (Date.now() / 1000 - d.up_since) / 3600;
  let h = `<div class="sst-tiles">${[
    tile('Requests today', today.requests), tile('Visitors today', today.visitors), tile('Requests, last hour', hourReq),
    tile(`Visitors, ${n} days`, sum('visitors')), tile('Errors today (4xx/5xx)', today.errors, today.server_errors ? 'bad' : ''),
    tile('Server errors today', today.server_errors, today.server_errors ? 'bad' : ''), tile('p95 response (ms)', d.latency_ms.p95),
    tile('Up for', up < 48 ? `${up.toFixed(1)} h` : `${(up / 24).toFixed(1)} d`)].join('')}</div>`;
  const groups = {};
  for (const g of d.gauges) (groups[g.group] ||= []).push(g);
  for (const [name, gs] of Object.entries(groups)) h += `<h3>${esc(name)}</h3><div class="sst-tiles">${gs.map((g) => tile(g.label, g.value)).join('')}</div>`;
  body.innerHTML = h;
  const section = (t, node) => { const hh = document.createElement('h3'); hh.textContent = t; body.append(hh, node); };
  const dayPts = (k) => d.days.map((x) => ({ v: x[k] || 0, x }));
  section(`Requests per day`, bars(dayPts('requests'), { label: (p) => `${short(p.x.day)}: <b>${fmt(p.v)}</b> requests${p.x.errors ? `, ${fmt(p.x.errors)} errors` : ''}`, xLabel: (p) => short(p.x.day) }));
  section(`Unique visitors per day`, bars(dayPts('visitors'), { colour: 'var(--sst-bar2)', label: (p) => `${short(p.x.day)}: <b>${fmt(p.v)}</b> visitors`, xLabel: (p) => short(p.x.day) }));
  const hm = (t) => new Date(t * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  section('Requests per minute, last hour', bars(d.last_hour.map((x) => ({ v: x.requests, x })), {
    label: (p) => `${hm(p.x.t)}: <b>${p.v}</b> requests${p.x.errors ? `, <span class="err">${p.x.errors} errors</span>` : ''}`, xLabel: (p) => hm(p.x.t) }));

  // security / app events: totals over the range, then the latest ones
  const evRows = d.event_kinds.map((k) => {
    const total = d.days.reduce((a, x) => a + (x.counts[`event:${k.kind}`] || 0), 0);
    return `<tr><td>${esc(k.label)}</td><td class="n">${fmt(today.counts?.[`event:${k.kind}`] || 0)}</td><td class="n">${fmt(total)}</td></tr>`;
  }).join('');
  const t1 = document.createElement('div');
  t1.innerHTML = evRows ? `<table><tr><th>Event</th><th class="n">Today</th><th class="n">${n} days</th></tr>${evRows}</table>` : '<p class="muted">Nothing yet.</p>';
  section('Events', t1);
  const t2 = document.createElement('div');
  t2.className = 'sst-scroll';
  t2.innerHTML = d.events.length ? `<table><tr><th>When</th><th>What</th><th>From</th><th>Detail</th></tr>${d.events.map((e) => `<tr><td title="${esc(new Date(e.t * 1000).toLocaleString('en-GB'))}">${ago(e.t)}</td><td>${esc(e.label)}</td><td class="muted">${esc(e.ip || '')}</td><td class="muted">${esc(e.detail || '')}</td></tr>`).join('')}</table>` : '<p class="muted">No events logged.</p>';
  section('Latest events', t2);
  const top = d.paths_today[0]?.[1] || 1;
  const t3 = document.createElement('div');
  t3.innerHTML = d.paths_today.length ? `<table><tr><th>Path</th><th class="n">Requests today</th></tr>${d.paths_today.map(([p, c]) => `<tr><td><code>${esc(p)}</code><div class="pbar" style="width:${(100 * c) / top}%"></div></td><td class="n">${fmt(c)}</td></tr>`).join('')}</table>` : '<p class="muted">No requests today.</p>';
  section('Busiest paths today', t3);
  const foot = document.createElement('p');
  foot.className = 'muted';
  foot.style.fontSize = '11.5px';
  foot.textContent = `site-stats ${d.version} · p50 ${fmt(d.latency_ms.p50)} ms, p99 ${fmt(d.latency_ms.p99)} ms (recent requests) · stats file ${(d.db_bytes / 1048576).toFixed(1)} MB · addresses are cut to /24 and visitors are a daily-salted hash.`;
  body.append(foot);
}
