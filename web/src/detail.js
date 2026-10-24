// The spot card: why it scored, what's there, can you fly it, the light, the wind, and a 3D look.
import { el, metres, compass } from './util.js';
import { sunBox, weatherBox } from './conditions.js';
import { Preview3D } from './preview3d.js';

const NICE = {
  gaps: 'Gaps', lone: 'Lone trees', edges: 'Tree lines / edges', space: 'Open space', mix: 'Obstacle mix',
  vertical: 'Tall stuff', relief: 'Slopes / drops', bando: 'Bando nearby', water: 'Water', bridge: 'Bridge / viaduct',
  architecture: 'Architecture', trees: 'Trees', under: 'Under-canopy space', landmark: 'Landmark', plaza: 'Plazas',
  busy: 'Busy (minus)', hazard: 'Masts / cranes (minus)', crowds: 'Crowds (minus)', wet: 'Mostly water (minus)',
  permission: 'Owner permission (minus)',
};

function statChips(s, profile) {
  const chips = [];
  const add = (v, label) => chips.push(el('span', { class: 'chip' }, el('b', {}, v), ` ${label}`));
  if (profile === 'cine85') add(s.gaps_whoop, s.gaps_whoop === 1 ? 'whoop-size gap' : 'whoop-size gaps');
  else add(s.gaps, s.gaps === 1 ? 'gap' : 'gaps');
  add(s.lone_trees, s.lone_trees === 1 ? 'lone tree' : 'lone trees');
  add(metres(s.edge_m), 'of edges');
  add(`${s.open_pct}%`, 'open');
  add(`${s.tree_pct}%`, 'trees');
  if (s.bld_pct) add(`${s.bld_pct}%`, 'buildings');
  add(`${Math.round(s.max_height_m)} m`, 'tallest');
  if (s.relief_m >= 8) add(`${Math.round(s.relief_m)} m`, 'of drop');
  if (s.water_m < 300) add(metres(s.water_m), 'to water');
  if (s.bando_m < 400) add(metres(s.bando_m), 'to a bando');
  if (s.bridge_m < 400) add(metres(s.bridge_m), 'to a bridge');
  if (s.hazard_m < 150) add(metres(s.hazard_m), 'to a mast/crane');
  return el('div', { class: 'chips' }, chips);
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

function legalBox(p, profile, meta) {
  const L = p.legal;
  const cat = meta.profiles[profile].category;
  const flags = L.flags || [];
  const status = profile === 'freestyle5'
    ? (L.a3_clear_pct >= 90 ? 'ok' : 'warn')
    : 'ok';
  return el('div', { class: 'box' },
    el('h4', {}, `Can you fly it? (${cat})`),
    el('div', { class: status }, L.summary),
    flags.length ? el('ul', { class: 'legal-flags' }, flags.map((f) => el('li', {}, f))) : null,
    el('div', { class: 'muted', style: 'margin-top:6px;font-size:12.5px' },
      'In goggles you need a spotter beside you who can see the quad. Rules and land owners change: check the ',
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
    el('div', {}, `Suggested take-off ${where} (green on the map).`),
    el('div', { class: 'muted' }, `Nearest path ${metres(t.path_m)} · nearest car park ${metres(t.parking_m)}.`),
    el('div', { class: 'links', style: 'margin-top:8px' },
      el('a', { href: `https://www.google.com/maps/dir/?api=1&destination=${t.lat},${t.lon}`, target: '_blank', rel: 'noopener' }, 'Directions')));
}

export function renderDetail(host, { spot, meta, profile, ratings, base, onRate, onLike, isSeed = false }) {
  const p = spot.properties;
  const [lon, lat] = spot.geometry.coordinates;
  const rating = ratings.get(profile, p.id);
  const kids = [];
  kids.push(el('div', { class: 'd-title' }, p.title));
  kids.push(el('div', { class: 'd-sub' },
    isSeed ? 'Your spot · ' : `#${p.rank} for ${meta.profiles[profile].label} · `,
    p.locality ? `${p.locality} · ` : '', el('code', {}, p.gridref || '')));

  kids.push(el('div', { class: 'scorebar' },
    el('div', { class: 'big-score' }, p.score ?? '–'),
    el('div', { class: 'score-split' },
      p.rules != null ? `Rules ${p.rules} · ` : '',
      p.taste != null ? `like your spots ${p.taste}` : 'no seed spots for this drone yet',
      el('br'), 'out of 100, compared with everywhere else in the region')));

  if (p.drivers?.length) {
    kids.push(el('div', { class: 'muted', style: 'font-size:12.5px' }, 'Why it stands out'));
    kids.push(el('ul', { class: 'why' }, p.drivers.map((d) => el('li', {}, d))));
  }
  if (p.stats) kids.push(statChips(p.stats, profile));

  if (!isSeed) {
    kids.push(el('div', { class: 'rate' },
      el('button', { class: `up${rating > 0 ? ' on' : ''}`, onclick: () => onRate(rating > 0 ? 0 : 1) }, '👍 Good spot'),
      el('button', { class: `down${rating < 0 ? ' on' : ''}`, onclick: () => onRate(rating < 0 ? 0 : -1) }, '👎 Not for me'),
      el('button', { class: 'like', onclick: onLike }, 'More like this')));
    const url = `${location.origin}${location.pathname}${location.search}#${profile}/${p.id}`;
    const share = el('a', {
      href: url,
      onclick: async (e) => {
        e.preventDefault();
        try {
          if (navigator.share) await navigator.share({ title: `${p.title} (FPV spot)`, url });
          else {
            await navigator.clipboard.writeText(url);
            share.textContent = 'Link copied';
          }
        } catch { /* share sheet closed */ }
      },
    }, 'Share this spot');
    kids.push(el('div', { style: 'font-size:13px;margin:2px 0 4px' }, share));
  }

  const p3dHost = el('div');
  kids.push(p3dHost);
  if (p.legal) kids.push(legalBox(p, profile, meta));
  kids.push(takeoffBox(p));
  kids.push(weatherBox(lon, lat, profile));
  kids.push(sunBox(lon, lat));

  if (p.components) {
    kids.push(el('div', { class: 'box' }, el('h4', {}, 'Score breakdown'), compBars(p.components, meta, profile)));
  }
  if (p.links) {
    kids.push(el('div', { class: 'box' },
      el('h4', {}, 'See it for real'),
      el('div', { class: 'muted', style: 'font-size:12.5px;margin-bottom:6px' },
        'Geograph has people\'s ground-level photos of this 1 km square: often the quickest way to judge a spot.'),
      el('div', { class: 'links' },
        el('a', { href: p.links.geograph, target: '_blank', rel: 'noopener' }, 'Geograph photos'),
        el('a', { href: p.links.streetview, target: '_blank', rel: 'noopener' }, 'Street View'),
        el('a', { href: p.links.satellite, target: '_blank', rel: 'noopener' }, 'Satellite'),
        el('a', { href: p.links.osmaps, target: '_blank', rel: 'noopener' }, 'OS map'))));
  }
  const src = p.stats?.lidar_pct >= 50 ? 'EA LiDAR (1 m)' : 'a 1 m tree-height model + building heights (LiDAR not loaded here yet)';
  kids.push(el('div', { class: 'muted', style: 'font-size:12px' }, `Heights from ${src}.`));

  host.replaceChildren(...kids.filter(Boolean));

  let preview = null;
  if (p.preview) {
    preview = new Preview3D(p3dHost, {
      spot: p, preview: p.preview, center: p.preview.center, profile,
      radiusM: meta.profiles[profile].radius_m,
    });
    preview.load(`${base}/${p.preview.file}`).catch((e) => {
      p3dHost.replaceChildren(el('div', { class: 'muted' }, `3D view unavailable (${e.message || e}).`));
    });
  } else {
    p3dHost.append(el('div', { class: 'muted', style: 'font-size:12.5px;margin:10px 0' },
      '3D view is made for the top spots only. Re-run the pipeline with more --previews to get one here.'));
  }
  return { dispose: () => preview?.dispose() };
}

export function nice(k) {
  return NICE[k] || k;
}
