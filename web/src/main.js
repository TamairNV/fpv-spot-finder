import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import './style.css';
import { $, $$, el, esc, circlePolygon, scoreColour, haversine, metres, store, save, SANDBOXED } from './util.js';
import { LegalLookup, verdicts } from './legal.js';
import { Ratings, learnTaste, likeThis } from './ratings.js';
import { renderDetail, nice } from './detail.js';

const params = new URLSearchParams(location.search);
const REGION = params.get('region') || 'newcastle';
const BASE = `${import.meta.env.BASE_URL}data/${REGION}`;
const PREFS_KEY = 'fpvspots:prefs';

const abs = (path) => new URL(path, location.href).href;

const BASEMAP_URL = 'https://tiles.openfreemap.org/styles/liberty';
// The bundled basemap: water, woods, built-up land, main roads and place names from the pipeline.
// Used offline, when the tile server can't be reached, and in sandboxed hosts.
function liteStyle() {
  const k = (kind) => ['==', ['get', 'k'], kind];
  const w = (lo, hi) => ['interpolate', ['linear'], ['zoom'], 9, lo, 16, hi];
  return {
    version: 8,
    sources: {
      lite: {
        type: 'geojson', data: abs(`${BASE}/basemap.json`), tolerance: 0.5,
        attribution: '© OpenStreetMap contributors, Overture Maps',
      },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#eef0e8' } },
      { id: 'lite-built', type: 'fill', source: 'lite', filter: k('built'), paint: { 'fill-color': '#e1dcd3' } },
      { id: 'lite-green', type: 'fill', source: 'lite', filter: k('green'), paint: { 'fill-color': '#d5e6c4' } },
      { id: 'lite-wood', type: 'fill', source: 'lite', filter: k('wood'), paint: { 'fill-color': '#b5d09c' } },
      { id: 'lite-water', type: 'fill', source: 'lite', filter: k('water'), paint: { 'fill-color': '#a6c8e2' } },
      { id: 'lite-waterline', type: 'line', source: 'lite', filter: k('waterline'), paint: { 'line-color': '#a6c8e2', 'line-width': w(1, 5) } },
      { id: 'lite-rail', type: 'line', source: 'lite', filter: k('rail'), paint: { 'line-color': '#8f8f8f', 'line-width': w(0.8, 2), 'line-dasharray': [3, 2] } },
      {
        id: 'lite-road-case', type: 'line', source: 'lite', filter: k('road'), layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': '#bdb6aa', 'line-width': w(['match', ['get', 'r'], 1, 2.6, 2, 1.8, 1.2], ['match', ['get', 'r'], 1, 10, 2, 8, 6]) },
      },
      {
        id: 'lite-road', type: 'line', source: 'lite', filter: k('road'), layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: {
          'line-color': ['match', ['get', 'r'], 1, '#f3bd5d', 2, '#fbe19a', '#ffffff'],
          'line-width': w(['match', ['get', 'r'], 1, 1.6, 2, 1, 0.6], ['match', ['get', 'r'], 1, 8, 2, 6, 4.5]),
        },
      },
    ],
  };
}
let liteActive = false;
let placeLabels = [];
const SATELLITE = {
  type: 'raster', tileSize: 256, maxzoom: 19,
  tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
  attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
};

const state = {
  meta: null,
  profile: 'freestyle5',
  spots: {},
  sort: 'score',
  like: null,
  query: '',
  selected: null,
  detail: null,
  pins: [],
  seedMarkers: [],
  me: null,
  base: 'map',
  ...store(PREFS_KEY, {}),
};
let map, lookup, ratings;

// ---------------------------------------------------------------- boot

async function boot() {
  const metaRes = await fetch(`${BASE}/meta.json`);
  if (!metaRes.ok) {
    document.body.innerHTML = `<div style="padding:40px;color:#eee;font:16px system-ui">No data for “${esc(REGION)}” yet.
      Run <code>python -m spotfinder run --region ${esc(REGION)}</code> first.</div>`;
    return;
  }
  state.meta = await metaRes.json();
  ratings = new Ratings(REGION);
  $('#region-name').textContent = `· ${state.meta.region.name}`;
  const [a, b] = await Promise.all(['freestyle5', 'cine85'].map((k) => fetch(`${BASE}/spots_${k}.geojson`).then((r) => r.json())));
  state.spots = { freestyle5: a, cine85: b };
  if (!state.spots[state.profile]) state.profile = 'freestyle5';

  lookup = new LegalLookup(state.meta);
  lookup.load(`${BASE}/legal_lookup.png`).catch(() => {});

  initMap();
  wireUI();
  renderList();
  renderSeeds();
  syncSegs();
}

// ---------------------------------------------------------------- map

function initMap() {
  const [lon, lat] = state.meta.region.center;
  const offline = SANDBOXED || navigator.onLine === false;
  liteActive = offline;
  map = new maplibregl.Map({
    container: 'map',
    style: offline ? liteStyle() : BASEMAP_URL,
    center: [lon, lat],
    zoom: 10.2,
    maxZoom: 18.5,
    attributionControl: { compact: true, customAttribution: 'Heights © Environment Agency (OGL)' },
    hash: false,
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
  if (!SANDBOXED) {
    const geo = new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: true }, trackUserLocation: false });
    map.addControl(geo, 'top-right');
    geo.on('geolocate', (e) => {
      state.me = [e.coords.longitude, e.coords.latitude];
      if (state.sort === 'near') renderList();
    });
  }

  // If the online basemap can't be reached, switch to the bundled one; overlays work either way.
  let loaded = offline, fellBack = offline;
  const fallback = () => {
    if (loaded || fellBack) return;
    fellBack = true;
    liteActive = true;
    map.setStyle(liteStyle());
  };
  const timer = offline ? null : setTimeout(fallback, 6000);
  map.on('error', () => fallback());
  map.on('style.load', () => {
    loaded = true;
    clearTimeout(timer);
    addOverlays();
    updatePlaceLabels();
  });
  map.on('zoom', updatePlaceLabels);
  map.on('click', onMapClick);
  loadPlaceLabels();
}

// Place names for the bundled basemap, as plain HTML labels (no font files needed).
async function loadPlaceLabels() {
  try {
    const rows = await fetch(`${BASE}/places.json`).then((r) => r.json());
    placeLabels = rows.map(([name, lon, lat, rank]) => {
      const node = el('div', { class: `place r${rank}` }, name);
      return { rank, marker: new maplibregl.Marker({ element: node }).setLngLat([lon, lat]).addTo(map) };
    });
    updatePlaceLabels();
  } catch {
    /* no labels, the map still works */
  }
}

function updatePlaceLabels() {
  const z = map.getZoom();
  const minZoom = [0, 8.5, 11.5, 13.5];
  placeLabels.forEach(({ rank, marker }) => {
    marker.getElement().style.display = liteActive && z >= minZoom[rank] ? '' : 'none';
  });
}

function firstSymbolId() {
  return map.getStyle().layers.find((l) => l.type === 'symbol')?.id;
}

function addOverlays() {
  const m = state.meta.region;
  const before = firstSymbolId();
  if (!SANDBOXED) {
    if (!map.getSource('sat')) map.addSource('sat', SATELLITE);
    map.addLayer({ id: 'sat', type: 'raster', source: 'sat', layout: { visibility: state.base === 'satellite' ? 'visible' : 'none' } }, before);
  }
  // Zoomed out: a coarse heatmap (100 m, max of the cells) so good areas stay visible.
  // Zoomed in: the 25 m heatmap plus the legal overlay, which is too busy to read from far out.
  map.addSource('heatlo', { type: 'image', url: `${BASE}/heatlo_${state.profile}.png`, coordinates: m.heat_lo_corners || m.heat_corners });
  map.addLayer({ id: 'heatlo', type: 'raster', source: 'heatlo', maxzoom: 12, paint: { 'raster-opacity': 0.8, 'raster-fade-duration': 0 } }, before);
  map.addSource('heat', { type: 'image', url: `${BASE}/heat_${state.profile}.png`, coordinates: m.heat_corners });
  map.addLayer({ id: 'heat', type: 'raster', source: 'heat', minzoom: 12, paint: { 'raster-opacity': ['interpolate', ['linear'], ['zoom'], 12, 0.75, 16, 0.35], 'raster-fade-duration': 0 } }, before);
  map.addSource('legal', { type: 'image', url: `${BASE}/legal_${state.profile}.png`, coordinates: m.legal_corners });
  map.addLayer({ id: 'legal', type: 'raster', source: 'legal', minzoom: 11.5, paint: { 'raster-opacity': ['interpolate', ['linear'], ['zoom'], 11.5, 0, 12.5, 0.8], 'raster-resampling': 'nearest', 'raster-fade-duration': 0 } }, before);
  map.addSource('zones', { type: 'geojson', data: `${BASE}/zones.geojson` });
  map.addLayer({
    id: 'zones-fill', type: 'fill', source: 'zones',
    paint: { 'fill-color': ['match', ['get', 'kind'], 'Danger area', '#ff9f1c', '#e5383b'], 'fill-opacity': 0.08 },
  });
  map.addLayer({
    id: 'zones-line', type: 'line', source: 'zones',
    paint: { 'line-color': ['match', ['get', 'kind'], 'Danger area', '#ff9f1c', '#e5383b'], 'line-width': 1.6, 'line-dasharray': [3, 2] },
  });
  map.addSource('sel', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addLayer({ id: 'sel-area', type: 'fill', source: 'sel', filter: ['==', ['get', 'k'], 'area'], paint: { 'fill-color': '#ffffff', 'fill-opacity': 0.08 } });
  map.addLayer({ id: 'sel-line', type: 'line', source: 'sel', filter: ['==', ['get', 'k'], 'area'], paint: { 'line-color': '#ffffff', 'line-width': 2, 'line-dasharray': [2, 1.5] } });
  map.addLayer({
    id: 'sel-pts', type: 'circle', source: 'sel', filter: ['!=', ['get', 'k'], 'area'],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 13, 2.5, 17, 7],
      'circle-color': ['match', ['get', 'k'], 'gap', '#ff3d7f', 'lone', '#ffc23d', 'takeoff', '#3ddc84', '#fff'],
      'circle-stroke-color': '#111', 'circle-stroke-width': 1,
    },
  });
  map.addSource('spots', { type: 'geojson', data: state.spots[state.profile] });
  map.addLayer({
    id: 'spots-dots', type: 'circle', source: 'spots',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 3, 14, 7],
      'circle-color': ['interpolate', ['linear'], ['get', 'score'], 55, '#2bb3c0', 77, '#ffc23d', 100, '#ff3d7f'],
      'circle-stroke-color': '#111', 'circle-stroke-width': 1,
    },
  });
  map.on('mouseenter', 'spots-dots', () => (map.getCanvas().style.cursor = 'pointer'));
  map.on('mouseleave', 'spots-dots', () => (map.getCanvas().style.cursor = ''));
  applyLayerToggles();
  drawPins();
  drawSeeds();
  addLegend();
  if (location.hash.length > 1) openFromHash();
}

function addLegend() {
  if ($('.legend')) return;
  const box = el('div', { class: 'legend' },
    el('div', {}, el('i', { style: 'background:linear-gradient(90deg,#2bb3c0,#ffc23d,#ff3d7f)' }), 'Better spots (heatmap)'),
    el('div', { id: 'legend-a3' }, el('i', { style: 'background:rgba(245,158,11,.55)' }), 'A3 no-go for the 5" (zoom in to see)'),
    el('div', {}, el('i', { style: 'background:rgba(220,38,38,.6)' }), 'No-fly zone (airport, prison…)'));
  $('#map').append(box);
}

function applyLayerToggles() {
  const vis = (id, on) => map.getLayer(id) && map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
  vis('heat', $('#lyr-heat').checked);
  vis('heatlo', $('#lyr-heat').checked);
  vis('legal', $('#lyr-legal').checked);
  vis('zones-fill', $('#lyr-zones').checked);
  vis('zones-line', $('#lyr-zones').checked);
  vis('spots-dots', $('#lyr-spots').checked);
  vis('sat', state.base === 'satellite');
  state.pins.forEach((p) => (p.getElement().style.display = $('#lyr-spots').checked ? '' : 'none'));
  const a3 = $('#legend-a3');
  if (a3) a3.style.display = state.profile === 'freestyle5' ? '' : 'none';
}

function setProfile(key) {
  if (key === state.profile) return;
  state.profile = key;
  state.like = null;
  savePrefs();
  closeDetail();
  if (map.getSource('heat')) {
    map.getSource('heat').updateImage({ url: `${BASE}/heat_${key}.png`, coordinates: state.meta.region.heat_corners });
    map.getSource('heatlo').updateImage({ url: `${BASE}/heatlo_${key}.png`, coordinates: state.meta.region.heat_lo_corners || state.meta.region.heat_corners });
    map.getSource('legal').updateImage({ url: `${BASE}/legal_${key}.png`, coordinates: state.meta.region.legal_corners });
    map.getSource('spots').setData(state.spots[key]);
  }
  applyLayerToggles();
  syncSegs();
  renderList();
  renderSeeds();
  drawSeeds();
}

function savePrefs() {
  save(PREFS_KEY, { profile: state.profile, sort: state.sort === 'near' ? 'score' : state.sort, base: state.base });
}

// ---------------------------------------------------------------- list

function orderedSpots() {
  const feats = [...state.spots[state.profile].features];
  let note = '';
  let key = (f) => f.properties.rank;
  let desc = false;
  if (state.like) {
    const target = feats.find((f) => f.properties.id === state.like);
    if (target) {
      const sim = likeThis(feats, target);
      key = (f) => sim(f);
      desc = true;
      note = `Spots most like “${target.properties.title}”. <a href="#" data-clear-like>Back to best</a>`;
    }
  } else if (state.sort === 'taste') {
    const model = learnTaste(feats, ratings, state.profile);
    if (model.ready) {
      key = (f) => model.predict(f) * 0.7 + (f.properties.score / 100) * 0.3;
      desc = true;
      const top = Object.entries(model.weights).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([k]) => k);
      note = `Learned from your ${model.pos} 👍 and ${model.neg} 👎. You seem to like: ${top.map(nice).join(', ').toLowerCase()}. `
        + (SANDBOXED
          ? 'To teach the whole-region ranking, copy your ratings (bottom left) and paste them back to Claude.'
          : 'To teach the whole-region ranking, download your ratings (bottom left) into config/ratings/ and re-run the scorer.');
    } else {
      note = `Rate a few spots 👍 / 👎 (at least 2 good, 1 not) and this list sorts itself by your taste. So far: ${model.pos} 👍, ${model.neg} 👎.`;
    }
  } else if (state.sort === 'near') {
    if (state.me) {
      key = (f) => haversine(state.me, f.geometry.coordinates);
      note = 'Closest first.';
    } else {
      note = 'Finding you… (allow location, or tap the locate button on the map)';
      if (!state.locating && navigator.geolocation) {
        state.locating = true;
        navigator.geolocation.getCurrentPosition((pos) => {
          state.locating = false;
          state.me = [pos.coords.longitude, pos.coords.latitude];
          renderList();
        }, () => {
          state.locating = false;
          state.sort = 'score';
          syncSegs();
          renderList();
        });
      }
    }
  }
  const q = state.query.trim().toLowerCase();
  let list = feats.filter((f) => !q || `${f.properties.title} ${f.properties.locality} ${f.properties.gridref}`.toLowerCase().includes(q));
  list.sort((a, b) => (desc ? key(b) - key(a) : key(a) - key(b)));
  // spots you've binned sink to the bottom
  list = [...list.filter((f) => ratings.get(state.profile, f.properties.id) >= 0), ...list.filter((f) => ratings.get(state.profile, f.properties.id) < 0)];
  return { list, note };
}

function renderList() {
  const { list, note } = orderedSpots();
  const ol = $('#spot-list');
  const noteBox = $('#taste-note');
  noteBox.hidden = !note;
  noteBox.innerHTML = note;
  noteBox.querySelector('[data-clear-like]')?.addEventListener('click', (e) => {
    e.preventDefault();
    state.like = null;
    renderList();
  });
  ol.replaceChildren(...list.map((f, i) => {
    const p = f.properties;
    const r = ratings.get(state.profile, p.id);
    const sub = [p.locality, state.me ? metres(haversine(state.me, f.geometry.coordinates)) : null, subline(p)].filter(Boolean).join(' · ');
    const li = el('li', { class: `${state.selected === p.id ? 'on' : ''} ${r < 0 ? 'rated-down' : ''}`, 'data-id': p.id },
      el('span', { class: 'rank' }, i + 1),
      el('div', {}, el('div', { class: `spot-name${r > 0 ? ' rated-up' : ''}` }, p.title), el('div', { class: 'spot-sub' }, sub)),
      el('span', { class: 'score-pill', style: `color:${scoreColour(p.score)}` }, p.score));
    li.addEventListener('click', () => select(p.id, true));
    return li;
  }));
  state.order = list;
  drawPins();
}

function subline(p) {
  const s = p.stats;
  if (!s) return '';
  if (state.profile === 'cine85') return `${s.gaps_whoop} gaps · ${s.bld_pct}% buildings`;
  return `${s.gaps} gaps · ${s.lone_trees} lone trees`;
}

function drawPins() {
  if (!map || !map.getSource?.('spots')) return;
  state.pins.forEach((m) => m.remove());
  state.pins = [];
  const top = (state.order || []).slice(0, 40);
  top.forEach((f, i) => {
    const p = f.properties;
    const node = el('div', { class: `pin${state.selected === p.id ? ' on' : ''}`, style: `background:${scoreColour(p.score)}`, title: p.title, 'data-id': p.id }, i + 1);
    node.addEventListener('click', (e) => {
      e.stopPropagation();
      select(p.id, false);
    });
    const mk = new maplibregl.Marker({ element: node }).setLngLat(f.geometry.coordinates).addTo(map);
    if (!$('#lyr-spots').checked) node.style.display = 'none';
    state.pins.push(mk);
  });
}

// ---------------------------------------------------------------- seeds ("your spots")

function seedsFor(profile) {
  return (state.meta.seeds || []).filter((s) => s.profile === profile);
}

function renderSeeds() {
  const box = $('#seeds-box');
  const seeds = seedsFor(state.profile);
  const tools = el('div', { style: 'margin-top:8px;display:flex;gap:12px;flex-wrap:wrap' },
    SANDBOXED ? null : el('a', { href: `${BASE}/spots_${state.profile}.gpx`, download: `fpv-spots-${REGION}-${state.profile}.gpx` }, 'Spots as GPX'),
    el('a', { href: '#', onclick: exportRatings }, SANDBOXED ? 'Copy my ratings' : 'Download my ratings'));
  if (!seeds.length) {
    box.replaceChildren(el('div', {}, 'No seed spots for this drone. Add your favourite spots to config/seeds.toml and the ranking learns their style.'), tools);
    return;
  }
  box.replaceChildren(el('div', {}, el('b', {}, 'Your spots'), ' (the style the ranking looks for)'),
    ...seeds.map((s) => el('div', { style: 'margin-top:4px' },
      el('button', { onclick: () => openSeed(s) }, `★ ${s.name}`),
      ` scores ${s.score ?? '–'}`,
      state.profile === 'freestyle5' && s.a3_frac < 0.6 ? el('span', { class: 'warn' }, ' · not A3-legal for the 5"') : null)),
    tools);
}

function drawSeeds() {
  state.seedMarkers.forEach((m) => m.remove());
  state.seedMarkers = seedsFor(state.profile).map((s) => {
    const node = el('div', { class: 'seed-pin', title: s.name });
    node.addEventListener('click', (e) => {
      e.stopPropagation();
      openSeed(s);
    });
    return new maplibregl.Marker({ element: node }).setLngLat([s.lon, s.lat]).addTo(map);
  });
}

function openSeed(s) {
  const fake = {
    type: 'Feature', geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
    properties: {
      id: `seed-${s.name}`, title: s.name, locality: 'your spot', score: s.score, rules: null, taste: null,
      components: s.components, drivers: s.note ? [s.note] : [],
      legal: {
        summary: state.profile === 'freestyle5'
          ? (s.a3_frac >= 0.6 ? 'Clear of the A3 distances.' : 'Not A3-legal for an 800 g quad: it\'s within 150 m of a park or houses. Fine for a sub-250 g whoop.')
          : 'Open A1: fine for a sub-250 g whoop outside restriction zones; never over crowds.',
        flags: s.flags, a3_clear_pct: Math.round(100 * s.a3_frac),
      },
      preview: s.preview, gaps_list: [], lone_list: [],
    },
  };
  showDetail(fake, true);
  map.flyTo({ center: [s.lon, s.lat], zoom: 16, essential: true });
}

// ---------------------------------------------------------------- selection + detail

function select(id, fly) {
  const f = state.spots[state.profile].features.find((x) => x.properties.id === id);
  if (!f) return;
  state.selected = id;
  history.replaceState(null, '', `#${state.profile}.${id}`);
  $$('#spot-list li').forEach((li) => li.classList.toggle('on', li.dataset.id === id));
  $(`#spot-list li[data-id="${id}"]`)?.scrollIntoView({ block: 'nearest' });
  state.pins.forEach((m) => m.getElement().classList.toggle('on', m.getElement().dataset.id === id));
  showSelection(f);
  showDetail(f, false);
  if (fly) {
    const narrow = window.innerWidth <= 820;
    map.flyTo({
      center: f.geometry.coordinates, zoom: 15.5, essential: true,
      padding: narrow ? { bottom: window.innerHeight * 0.45 } : { right: 440 },
    });
  }
}

function showSelection(f) {
  const p = f.properties;
  const feats = [{ type: 'Feature', geometry: circlePolygon(f.geometry.coordinates, state.meta.profiles[state.profile].radius_m), properties: { k: 'area' } }];
  (p.gaps_list || []).forEach((g) => feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [g[0], g[1]] }, properties: { k: 'gap' } }));
  (p.lone_list || []).forEach((t) => feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [t[0], t[1]] }, properties: { k: 'lone' } }));
  if (p.takeoff) feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [p.takeoff.lon, p.takeoff.lat] }, properties: { k: 'takeoff' } });
  map.getSource('sel')?.setData({ type: 'FeatureCollection', features: feats });
}

function showDetail(f, isSeed) {
  state.detail?.dispose();
  const host = $('#detail-body');
  $('#detail').hidden = false;
  $('#detail .detail-scroll').scrollTop = 0;
  state.detail = renderDetail(host, {
    spot: f, meta: state.meta, profile: state.profile, ratings, base: BASE, isSeed,
    onRate: (v) => {
      ratings.set(state.profile, f.properties.id, v, { title: f.properties.title });
      showDetail(f, isSeed);
      renderList();
    },
    onLike: () => {
      state.like = f.properties.id;
      renderList();
      $('#spot-list').scrollTop = 0;
    },
  });
}

function closeDetail() {
  state.detail?.dispose();
  state.detail = null;
  state.selected = null;
  $('#detail').hidden = true;
  map?.getSource('sel')?.setData({ type: 'FeatureCollection', features: [] });
  $$('#spot-list li.on').forEach((li) => li.classList.remove('on'));
  state.pins.forEach((m) => m.getElement().classList.remove('on'));
  history.replaceState(null, '', location.pathname + location.search);
}

function openFromHash() {
  const [profile, id] = location.hash.slice(1).split(/[./]/);
  if (state.spots[profile]) {
    if (profile !== state.profile) setProfile(profile);
    if (id) select(id, true);
  }
}

// ---------------------------------------------------------------- can I fly here?

function onMapClick(e) {
  const hitSpot = map.queryRenderedFeatures(e.point, { layers: ['spots-dots'] })[0];
  if (hitSpot) {
    select(hitSpot.properties.id, false);
    return;
  }
  const { lng, lat } = e.lngLat;
  const toast = $('#fly-check');
  const zones = map.queryRenderedFeatures(e.point, { layers: ['zones-fill'] }).map((z) => z.properties.name);
  const hit = lookup?.at(lng, lat);
  const v = verdicts(state.meta, hit, [...new Set(zones)]);
  if (!v) {
    toast.hidden = false;
    toast.replaceChildren(el('div', {}, 'Outside the area this region covers.'));
    return;
  }
  toast.hidden = false;
  toast.replaceChildren(...[
    el('h4', {}, 'Can I fly here?'),
    ...v.verdicts.map((x) => el('div', { class: 'row' }, el('span', { class: x.ok ? 'ok' : 'bad' }, x.ok ? '✓' : '✗'),
      el('span', {}, el('b', {}, x.who), ': ', x.text))),
    v.flags.length ? el('ul', { class: 'legal-flags' }, v.flags.map((t) => el('li', {}, t))) : null,
    el('div', { class: 'muted', style: 'font-size:12px;margin-top:6px' },
      'From OpenStreetMap land use and the NATS restriction zones, at 20 m. Always double-check on the ground.'),
  ].filter(Boolean));
}

// ---------------------------------------------------------------- UI wiring

function syncSegs() {
  $$('#profile-switch button').forEach((b) => b.classList.toggle('on', b.dataset.profile === state.profile));
  $$('#list-sort button').forEach((b) => b.classList.toggle('on', b.dataset.sort === state.sort && !state.like));
  $$('#basemap-switch button').forEach((b) => b.classList.toggle('on', b.dataset.base === state.base));
}

function wireUI() {
  if (SANDBOXED) {
    $('#list-sort button[data-sort="near"]')?.remove();
    $('#basemap-switch')?.previousElementSibling?.remove();
    $('#basemap-switch')?.remove();
  }
  $$('#profile-switch button').forEach((b) => b.addEventListener('click', () => setProfile(b.dataset.profile)));
  $$('#list-sort button').forEach((b) => b.addEventListener('click', () => {
    state.sort = b.dataset.sort;
    state.like = null;
    savePrefs();
    syncSegs();
    renderList();
  }));
  $$('#basemap-switch button').forEach((b) => b.addEventListener('click', () => {
    state.base = b.dataset.base;
    savePrefs();
    syncSegs();
    applyLayerToggles();
  }));
  $('#search').addEventListener('input', (e) => {
    state.query = e.target.value;
    renderList();
  });
  $('#btn-layers').addEventListener('click', (e) => {
    e.stopPropagation();
    $('#layers-pop').hidden = !$('#layers-pop').hidden;
  });
  document.addEventListener('click', (e) => {
    if (!$('#layers-pop').contains(e.target)) $('#layers-pop').hidden = true;
    if (!$('#fly-check').contains(e.target) && !$('#map').contains(e.target)) $('#fly-check').hidden = true;
  });
  $$('#layers-pop input').forEach((i) => i.addEventListener('change', applyLayerToggles));
  $('#detail-close').addEventListener('click', closeDetail);
  $('#btn-about').addEventListener('click', () => {
    $('#about-body').innerHTML = aboutHtml();
    $('#about-body [data-export]')?.addEventListener('click', exportRatings);
    $('#about-body [data-import]')?.addEventListener('change', importRatings);
    $('#about').showModal();
  });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#detail').hidden && !document.querySelector('.p3d-wrap.full')) closeDetail();
  });
}

function exportRatings(e) {
  e.preventDefault();
  if (SANDBOXED) {
    const link = e.currentTarget;
    const json = ratings.export();
    const showText = () => {  // no clipboard here: put the text where it can be selected and copied by hand
      link.textContent = 'Copy this text into config/ratings/<region>.json:';
      const box = el('textarea', { class: 'ratings-json', readonly: true, rows: 4 }, json);
      link.after(box);
      box.select();
    };
    (navigator.clipboard ? navigator.clipboard.writeText(json) : Promise.reject(new Error('no clipboard'))).then(
      () => { link.textContent = 'Copied: paste into config/ratings/<region>.json'; }, showText);
    return;
  }
  const blob = new Blob([ratings.export()], { type: 'application/json' });
  const a = el('a', { href: URL.createObjectURL(blob), download: `fpv-ratings-${REGION}.json` });
  a.click();
  URL.revokeObjectURL(a.href);
}

async function importRatings(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  ratings.import(await file.text());
  renderList();
}

function aboutHtml() {
  const s = state.meta.sources || {};
  return `
  <h2>How it works</h2>
  <p>No satellite photos and no AI image guessing. Every 50 m square of the region gets measured from <b>1 m height data</b>
  and map data, then scored on what FPV pilots actually look for:</p>
  <ul>
    <li><b>Gaps</b>: openings 2–12 m wide between trees or buildings at least 5 m tall (whoop gaps up to 6 m).</li>
    <li><b>Lone trees</b>: single trees with open ground all round, for orbits and power loops.</li>
    <li><b>Edges, open space, tall stuff, slopes, water, bridges, bandos</b> nearby, and for the whoop: architecture, plazas, landmarks and under-canopy space.</li>
    <li><b>Your taste</b>: each square is compared with your favourite spots (the ★ seeds), and with your 👍 / 👎 ratings in “My taste”.</li>
  </ul>
  <p><b>Legal first.</b> A spot can't be picked unless it's legal for that drone: the 800 g 5" is Open <b>A3</b>
  (150 m from houses, businesses, schools and parks; 50 m from buildings and busy roads), the 85 mm is <b>A1</b>.
  Airport and prison flight restriction zones block both. MOD land is excluded. Land-owner rules (National Trust,
  Forestry England, Woodland Trust, nature reserves) show up as warnings. Tap anywhere on the map for “can I fly here?”.</p>
  <h3>Be real about it</h3>
  <ul>
    <li>Heights: ${esc(s.heights || '')}. ${s.lidar_coverage ? `LiDAR covers ${Math.round(s.lidar_coverage * 100)}% of the region.` : 'Without LiDAR, small trees and gaps under 4 m get missed and building heights are estimates.'}</li>
    <li>Land use and owners come from OpenStreetMap: if a park or a National Trust boundary isn't mapped, it isn't known here.</li>
    <li>It can't see fences, “no drones” signs, livestock, nesting birds or people. Go and look first; take a spotter.</li>
    <li>Airspace: ${esc(s.airspace || '')}.</li>
  </ul>
  <h3>Your ratings</h3>
  <p>Kept in this browser only. <a href="#" data-export>${SANDBOXED ? 'Copy them' : 'Download them'}</a> to keep or feed back into the pipeline, or
  load a file: <input type="file" accept="application/json" data-import /></p>
  <p class="muted">Data: OpenStreetMap contributors (ODbL), Overture Maps, Meta/WRI canopy height, Environment Agency LiDAR (OGL),
  NATS UAS restriction zones, Open-Meteo. Built ${esc(state.meta.generated_at || '')}.</p>`;
}

boot();

if ('serviceWorker' in navigator && import.meta.env.PROD && !SANDBOXED) {
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}
