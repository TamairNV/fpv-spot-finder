// The spot card: a sticky header (back, name, score), then can you fly it, what's there, a 3D look,
// the wind and the light, and the score breakdown.
import { el, metres, compass, scoreColour, SANDBOXED } from './util.js';
import { sunBox, weatherBox } from './conditions.js';
import { flightBox } from './server.js';

const NICE = {
  gaps: 'Gaps', lone: 'Lone trees', edges: 'Tree lines / edges', space: 'Open space', mix: 'Obstacle mix',
  vertical: 'Tall stuff', relief: 'Slopes / drops', bando: 'Bando nearby', water: 'Water', bridge: 'Bridge / viaduct',
  architecture: 'Architecture', trees: 'Trees', under: 'Under-canopy space', landmark: 'Landmark', plaza: 'Plazas',
  busy: 'Busy (minus)', hazard: 'Masts / cranes (minus)', crowds: 'Crowds (minus)', wet: 'Mostly water (minus)',
  permission: 'Owner permission (minus)',
};

const BACK_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11H7.8l5.6-5.6L12 4l-8 8 8 8 1.4-1.4L7.8 13H20z"/></svg>';
const STEP_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.6 16.6 13.2 12 8.6 7.4 10 6l6 6-6 6z"/></svg>';

// How the spot sits with the rules, as a short badge: the 5" needs Open A3, the whoop is A1 anywhere outside FRZs.
export function legalBadge(p, a3, big = false) {
  const L = p.legal;
  if (!L) return null;
  const cls = big ? ' big' : '';
  if (!a3) {
    return (L.flags || []).length
      ? el('span', { class: `badge part${cls}`, title: L.flags.join(' · ') }, 'A1 · check owner')
      : el('span', { class: `badge ok${cls}`, title: L.summary }, 'A1 ok');
  }
  const pct = L.a3_clear_pct ?? 0;
  if (pct >= 90) return el('span', { class: `badge ok${cls}`, title: L.summary }, big ? 'A3-clear for the 5"' : 'A3 clear');
  if (pct >= 30) return el('span', { class: `badge part${cls}`, title: L.summary }, big ? `Partly A3-clear (${pct}%)` : `A3 ${pct}%`);
  return el('span', { class: `badge no${cls}`, title: L.summary }, big ? 'Not A3: sub-250 g only' : 'Sub-250');
}

function tiles(s, a3) {
  const out = [];
  const add = (v, label) => out.push(el('div', { class: 'tile' }, el('b', {}, v), el('span', {}, label)));
  if (!a3) add(s.gaps_whoop, s.gaps_whoop === 1 ? 'whoop gap' : 'whoop gaps');
  else add(s.gaps, s.gaps === 1 ? 'gap' : 'gaps');
  add(s.lone_trees, s.lone_trees === 1 ? 'lone tree' : 'lone trees');
  add(metres(s.edge_m), 'of edges');
  add(`${s.open_pct}%`, 'open');
  add(`${s.tree_pct}%`, 'trees');
  add(`${Math.round(s.max_height_m)} m`, 'tallest');
  if (s.bld_pct) add(`${s.bld_pct}%`, 'buildings');
  if (s.relief_m >= 8) add(`${Math.round(s.relief_m)} m`, 'of drop');
  if (s.water_m < 300) add(metres(s.water_m), 'to water');
  if (s.bando_m < 400) add(metres(s.bando_m), 'to a bando');
  if (s.bridge_m < 400) add(metres(s.bridge_m), 'to a bridge');
  if (s.hazard_m < 150) add(metres(s.hazard_m), 'to a mast/crane');
  return el('div', { class: 'tiles' }, out);
}

function compBars(comps, meta, profile) {
  const w = meta.profiles[profile].weights;
  const rows = Object.keys(w)
    .filter((k) => comps[k] != null)
    .sort((a, b) => Math.abs(w[b] * comps[b]) - Math.abs(w[a] * comps[a]));
  return el('div', { class: 'comp-grid' },
    rows.flatMap((k) => [
      el('span', {}, NICE[k] || k),
      el('span', { class: `comp-bar${w[k] < 0 ? ' neg' : ''}`, title: `${Math.round(comps[k] * 100)}% · weight ${w[k]}` },
        el('i', { style: `width:${Math.round(comps[k] * 100)}%` })),
    ]));
}

function whyList(p) {
  // Newer pipelines explain the score in points; older ones just list what stands out.
  if (Array.isArray(p.why) && p.why.length) {
    return el('ul', { class: 'why' }, p.why.slice(0, 6).map((w) => (typeof w === 'string'
      ? el('li', {}, w, el('span'))
      : el('li', {}, w.text, el('span', { class: 'pts' }, w.pts != null ? `${w.pts > 0 ? '+' : ''}${Math.round(w.pts)}` : '')))));
  }
  if (p.drivers?.length) return el('ul', { class: 'why' }, p.drivers.map((d) => el('li', {}, d, el('span'))));
  return null;
}

function legalBox(p, a3, cat) {
  const L = p.legal;
  const flags = L.flags || [];
  const pct = L.a3_clear_pct ?? 0;
  const tone = a3 ? (pct >= 90 ? 'ok' : pct >= 30 ? 'warn' : 'bad') : 'ok';
  return el('div', { class: 'box' },
    el('h4', {}, `Can you fly it? · ${cat}`),
    el('div', { class: 'legal-status' }, el('span', { class: tone }, tone === 'ok' ? '✓' : '!'), el('span', {}, L.summary)),
    flags.length ? el('ul', { class: 'legal-flags' }, flags.map((f) => el('li', {}, f))) : null,
    el('p', { class: 'muted small', style: 'margin-top:8px' },
      'This is a place worth a look, not a permission. In goggles you need a spotter. Check the ',
      el('a', { href: 'https://dronesafetymap.co.uk/', target: '_blank', rel: 'noopener' }, 'drone safety map'),
      ' and the ', el('a', { href: 'https://register-drones.caa.co.uk/drone-code', target: '_blank', rel: 'noopener' }, 'Drone Code'),
      ' before you go.'));
}

function takeoffBox(p) {
  const t = p.takeoff;
  if (!t) return null;
  const d = Math.hypot(t.dx, t.dy);
  const where = d < 30 ? 'right by the pin' : `${metres(d)} ${compass(t.dx, t.dy)} of the pin`;
  return el('div', { class: 'box' },
    el('h4', {}, 'Getting there'),
    el('p', {}, `Suggested take-off ${where} (the green dot).`),
    el('p', { class: 'muted small' }, `Nearest path ${metres(t.path_m)} · nearest car park ${metres(t.parking_m)}.`),
    el('div', { class: 'links' },
      el('a', { href: `https://www.google.com/maps/dir/?api=1&destination=${t.lat},${t.lon}`, target: '_blank', rel: 'noopener' }, 'Directions to the take-off')));
}

export function renderSpot({ head, body, spot, meta, profile, ratings, base, isSeed = false, total, nav, onRate, onLike, onBack, onNote }) {
  const p = spot.properties;
  const [lon, lat] = spot.geometry.coordinates;
  let rating = ratings.get(profile, p.id);
  const { label, category } = meta.profiles[profile];
  const a3 = /A3/.test(category || '');

  // ---- header
  const back = el('button', { class: 'icon-btn', 'aria-label': 'Back to the list', title: 'Back to the list', onclick: onBack });
  back.innerHTML = BACK_ICON;
  const score = p.score ?? null;
  // previous / next spot in the list as it's sorted and filtered now
  const step = (fn, prev) => {
    const b = el('button', { class: `icon-btn step${prev ? ' prev' : ''}`, disabled: !fn, onclick: fn,
      'aria-label': prev ? 'Previous spot' : 'Next spot', title: prev ? 'Previous spot (←)' : 'Next spot (→)' });
    b.innerHTML = STEP_ICON;
    return b;
  };
  head.replaceChildren(back,
    el('div', { class: 'sh-main' },
      el('div', { class: 'sh-title', title: p.title }, p.title),
      el('div', { class: 'sh-sub' },
        isSeed ? 'Your spot' : `#${p.rank}${total ? ` of ${total}` : ''} for the ${label}`,
        p.locality && !isSeed ? ` · ${p.locality}` : '', p.gridref ? ` · ${p.gridref}` : '')),
    nav ? el('div', { class: 'sh-nav' }, step(nav.prev, true), step(nav.next, false)) : el('span'),
    el('div', { class: 'sh-score', style: `--c:${scoreColour(score ?? 0)};--p:${score ?? 0}`, title: score != null ? `Score ${score} out of 100` : '' },
      el('span', {}, score ?? '–')));

  // ---- body
  const kids = [];
  const badges = [legalBadge(p, a3, true)];
  if (p.rules != null) badges.push(el('span', { class: 'badge plain big', title: 'How it scores on the rules alone' }, `Rules ${p.rules}`));
  if (p.taste != null) badges.push(el('span', { class: 'badge plain big', title: 'How much it looks like your favourite spots' }, `Like your spots ${p.taste}`));
  kids.push(el('div', { class: 'd-badges' }, badges));

  if (!isSeed) {
    const url = `${location.origin}${location.pathname}${location.search}#${profile}.${p.id}`;
    const share = el('button', {
      class: 'act',
      onclick: async () => {
        try {
          if (navigator.share) await navigator.share({ title: `${p.title} (FPV spot)`, url });
          else {
            await navigator.clipboard.writeText(url);
            share.lastChild.textContent = 'Copied';
          }
        } catch { /* share sheet closed */ }
      },
    }, el('span', { class: 'ico' }, '↗'), el('span', {}, 'Share'));
    const dest = p.takeoff ? `${p.takeoff.lat},${p.takeoff.lon}` : `${lat},${lon}`;
    const rate = (v) => {
      rating = rating === v ? 0 : v;
      up.classList.toggle('on', rating > 0);
      down.classList.toggle('on', rating < 0);
      up.setAttribute('aria-pressed', String(rating > 0));
      down.setAttribute('aria-pressed', String(rating < 0));
      onRate(rating);
    };
    const up = el('button', { class: `act up${rating > 0 ? ' on' : ''}`, 'aria-pressed': String(rating > 0), onclick: () => rate(1) },
      el('span', { class: 'ico' }, '👍'), el('span', {}, 'Good'));
    const down = el('button', { class: `act down${rating < 0 ? ' on' : ''}`, 'aria-pressed': String(rating < 0), onclick: () => rate(-1) },
      el('span', { class: 'ico' }, '👎'), el('span', {}, 'Not for me'));
    kids.push(el('div', { class: 'actions' }, up, down,
      el('button', { class: 'act', onclick: onLike, title: 'List the spots most like this one' },
        el('span', { class: 'ico' }, '≈'), el('span', {}, 'Similar')),
      el('a', { class: 'act', href: `https://www.google.com/maps/dir/?api=1&destination=${dest}`, target: '_blank', rel: 'noopener' },
        el('span', { class: 'ico' }, '➜'), el('span', {}, 'Directions')),
      SANDBOXED ? null : share));
    kids.push(notesBox(p, ratings, onNote), flightBox(spot, profile, onNote));
  }

  const p3dHost = el('div');
  kids.push(p3dHost, lineBox(p));

  const why = whyList(p);
  if (why) kids.push(el('div', { class: 'muted small' }, 'Why it stands out'), why);
  if (p.stats) kids.push(tiles(p.stats, a3));

  if (p.legal) kids.push(legalBox(p, a3, category));
  kids.push(takeoffBox(p));
  kids.push(weatherBox(lon, lat, profile));
  kids.push(sunBox(lon, lat));

  if (p.components) {
    kids.push(el('details', { class: 'box' }, el('summary', {}, 'Score breakdown'), compBars(p.components, meta, profile)));
  }
  if (p.links) {
    kids.push(el('div', { class: 'box' },
      el('h4', {}, 'See it for real'),
      el('p', { class: 'muted small' }, 'Geograph has ground-level photos of this 1 km square: often the quickest way to judge a spot.'),
      el('div', { class: 'links' },
        el('a', { href: p.links.geograph, target: '_blank', rel: 'noopener' }, 'Geograph photos'),
        el('a', { href: p.links.streetview, target: '_blank', rel: 'noopener' }, 'Street View'),
        el('a', { href: p.links.satellite, target: '_blank', rel: 'noopener' }, 'Satellite'),
        el('a', { href: p.links.osmaps, target: '_blank', rel: 'noopener' }, 'OS map'))));
  }
  const src = p.stats?.lidar_pct >= 50 ? 'EA LiDAR (1 m)' : 'a 1 m tree-height model and building heights (no LiDAR here yet)';
  kids.push(el('div', { class: 'd-foot' }, `Heights from ${src}.`));

  body.replaceChildren(...kids.filter(Boolean));

  // The 3D view (and three.js with it) loads only when a spot is opened.
  let preview = null;
  let gone = false;
  if (p.preview) {
    p3dHost.append(el('div', { class: 'p3d-wrap' }, el('div', { class: 'p3d' }), el('div', { class: 'p3d-tag' }, 'Loading 3D…')));
    import('./preview3d.js').then(({ Preview3D }) => {
      if (gone) return;
      p3dHost.replaceChildren();
      preview = new Preview3D(p3dHost, {
        spot: p, preview: p.preview, center: p.preview.center, profile,
        radiusM: meta.profiles[profile].radius_m,
      });
      return preview.load(`${base}/${p.preview.file}`);
    }).catch((e) => {
      p3dHost.replaceChildren(el('div', { class: 'muted small', style: 'margin:0 0 12px' }, `3D view unavailable (${e.message || e}).`));
    });
  } else {
    p3dHost.append(el('div', { class: 'muted small', style: 'margin:0 0 12px' },
      'No 3D model for this spot yet: re-run the scorer to make one.'));
  }
  return { dispose: () => { gone = true; preview?.dispose(); } };
}

// Your own notes on the place, kept on this device and in the ratings download.
function notesBox(p, ratings, onNote) {
  const text = ratings.note(p.id);
  const area = el('textarea', { class: 'note-text', rows: 3, placeholder: 'Parking, access, who to ask, when it is quiet…', 'aria-label': 'Your notes' }, text);
  let timer;
  const keep = () => {
    clearTimeout(timer);
    ratings.setNote(p.id, area.value, { title: p.title });
    onNote?.();
  };
  area.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => ratings.setNote(p.id, area.value, { title: p.title }), 400);
  });
  area.addEventListener('change', keep);
  return el('details', { class: 'box notes', open: !!text }, el('summary', {}, text ? '📝 Your notes' : '📝 Add a note'), area);
}

// The pipeline's suggested run through the spot, numbered like the markers in the 3D view.
const MOVE_TEXT = { gap: 'Through the gap', orbit: 'Orbit the lone tree', dive: 'Dive over the tall stuff', skim: 'Skim low along the edge' };
function lineBox(p) {
  const moves = p.line?.moves;
  if (!moves?.length) return null;
  const head = [p.stats?.line_mix, p.line.length_m ? `about ${Math.round(p.line.length_m / 10) * 10} m` : null].filter(Boolean).join(' · ');
  return el('div', { class: 'box line-box' },
    el('h4', {}, 'A line to fly'),
    head ? el('p', { class: 'muted small' }, `${head}. From the take-off, in this order; the 3D view numbers them and “Watch the line” rides it.`) : null,
    el('ol', { class: 'moves' }, moves.map(([kind], i) => el('li', {},
      el('span', { class: `mv mv-${kind}` }, i + 1), MOVE_TEXT[kind] || kind))));
}

export function nice(k) {
  return NICE[k] || k;
}
