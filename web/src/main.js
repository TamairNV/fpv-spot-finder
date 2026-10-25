import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import './style.css';
import { $, $$, el, esc, circlePolygon, scoreColour, haversine, metres, store, save, SANDBOXED } from './util.js';
import { LegalLookup, verdicts } from './legal.js';
import { Ratings, learnTaste, likeThis } from './ratings.js';
import { renderSpot, legalBadge, nice, BANDO_STATE } from './detail.js';
import { nowConditions, sunTimes } from './conditions.js';
import { watchThumbs } from './thumbs.js';

const params = new URLSearchParams(location.search);
const REGION = params.get('region') || 'newcastle';
const DATA = `${import.meta.env.BASE_URL}data`;
const BASE = `${DATA}/${REGION}`;
const PREFS_KEY = 'fpvspots:prefs';
const PHONE = matchMedia('(max-width: 820px)');
const TOUCH = matchMedia('(hover: none)');

const abs = (path) => new URL(path, location.href).href;

const BASEMAP_URL = 'https://tiles.openfreemap.org/styles/positron';
// The bundled basemap: water, woods, built-up land, main roads and place names from the pipeline.
// Used offline, when the tile server can't be reached, and in sandboxed hosts.
function liteStyle() {
  const k = (kind) => ['==', ['get', 'k'], kind];
  const w = (lo, hi) => ['interpolate', ['linear'], ['zoom'], 9, lo, 16, hi];
  return {
    version: 8,
    sources: {
      lite: {
        type: 'vector', tiles: [`${abs(`${BASE}/tiles/`)}{z}/{x}/{y}.pbf`], minzoom: 8, maxzoom: 12, bounds: regionBounds(),
        attribution: '© OpenStreetMap contributors, Overture Maps',
      },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#f2f2ee' } },
      { id: 'lite-built', type: 'fill', source: 'lite', 'source-layer': 'base', filter: k('built'), paint: { 'fill-color': '#e7e4de' } },
      { id: 'lite-green', type: 'fill', source: 'lite', 'source-layer': 'base', filter: k('green'), paint: { 'fill-color': '#e0eadb' } },
      { id: 'lite-wood', type: 'fill', source: 'lite', 'source-layer': 'base', filter: k('wood'), paint: { 'fill-color': '#d2e2c8' } },
      { id: 'lite-water', type: 'fill', source: 'lite', 'source-layer': 'base', filter: k('water'), paint: { 'fill-color': '#c2d8e8' } },
      { id: 'lite-waterline', type: 'line', source: 'lite', 'source-layer': 'base', filter: k('waterline'), paint: { 'line-color': '#c2d8e8', 'line-width': w(1, 5) } },
      { id: 'lite-rail', type: 'line', source: 'lite', 'source-layer': 'base', filter: k('rail'), paint: { 'line-color': '#a9a9a9', 'line-width': w(0.8, 2), 'line-dasharray': [3, 2] } },
      {
        id: 'lite-road-case', type: 'line', source: 'lite', 'source-layer': 'base', filter: k('road'), layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': '#d3cfc8', 'line-width': w(['match', ['get', 'r'], 1, 2.6, 2, 1.8, 1.2], ['match', ['get', 'r'], 1, 10, 2, 8, 6]) },
      },
      {
        id: 'lite-road', type: 'line', source: 'lite', 'source-layer': 'base', filter: k('road'), layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: {
          'line-color': ['match', ['get', 'r'], 1, '#f7dca6', 2, '#fbefcf', '#ffffff'],
          'line-width': w(['match', ['get', 'r'], 1, 1.6, 2, 1, 0.6], ['match', ['get', 'r'], 1, 8, 2, 6, 4.5]),
        },
      },
    ],
  };
}
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
  a3only: false,
  kind: null,
  bandoList: false, // the list shows bandos instead of spots
  saved: false,
  more: {},
  moreOpen: false,
  bandos: false,
  selected: null,
  detail: null,
  pins: [],
  seedMarkers: [],
  me: null,
  base: 'map',
  sheet: 'half',
  checking: false,
  listScroll: 0,
  ...store(PREFS_KEY, {}),
};
let map, lookup, ratings, checkPopup;
let liteActive = false;
let placeLabels = [];
let heatHiLoaded = false;
const heatHiWanted = () => heatHiLoaded || map.getZoom() >= 11;
const BLANK_PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';

// The basemap tiles stop at the region's edge; asking for ones outside would only 404.
function regionBounds() {
  const lons = state.meta.region.corners.map((c) => c[0]), lats = state.meta.region.corners.map((c) => c[1]);
  return [Math.min(...lons) - 0.01, Math.min(...lats) - 0.01, Math.max(...lons) + 0.01, Math.max(...lats) + 0.01];
}

const profiles = () => Object.keys(state.meta.profiles);
const isA3 = (key = state.profile) => /A3/.test(state.meta.profiles[key]?.category || '');

// Spot types to filter the list by: the reasons worth 2+ points of a spot's score (kinds, from the
// pipeline). A type nearly every spot has, or only one or two do, isn't worth a chip.
const KINDS = {
  park: 'Parks', water: 'Water', bridge: 'Bridges', landmark: 'Landmarks', hero: 'Big landmarks', relief: 'Hills and banks', sunset: 'Golden hour', coast: 'Coast', beach: 'Beaches',
  plaza: 'Plazas', under: 'Under the trees', public: 'Public space', edges: 'Tree lines', space: 'Wide open', flow: 'Good lines',
};
const hasKind = (f, k) => f.properties.kinds?.includes(k) ?? (f.properties.why || []).some((w) => w.key === k && w.pts >= 2);
const kindCache = {};
function kindsFor(profile) {
  if (!kindCache[profile]) {
    const feats = state.spots[profile].features;
    kindCache[profile] = Object.keys(KINDS)
      .map((k) => [k, feats.filter((f) => hasKind(f, k)).length])
      .filter(([, n]) => n >= 3 && n <= feats.length * 0.85);
  }
  return kindCache[profile];
}
const liked = (f) => ratings.get(state.profile, f.properties.id) > 0;

// Filters on the numbers behind each spot. Distances are fixed steps; counts follow the drone's own
// spread (25 gaps is a lot for a whoop spot and few for a 5" one), from the middle up to the top quarter.
const gapsOf = (s) => (isA3() ? s.gaps : s.gaps_whoop);
const near = [100, 300, 1000];
const dist = (v) => (v >= 1000 ? `${v / 1000} km` : `${v} m`);
function upper(get, step) {
  const xs = state.spots[state.profile].features.map((f) => get(f.properties.stats || {})).filter(Number.isFinite).sort((a, b) => a - b);
  const at = (q) => xs[Math.floor(q * (xs.length - 1))] ?? 0;
  const round = (v) => (step ? Math.round(v / step) * step : v >= 50 ? Math.round(v / 10) * 10 : Math.max(1, Math.round(v / 5) * 5));
  return [...new Set([round(at(0.5)), round(at(0.75))])].filter((v) => v > 0);
}
// chip: how the step reads on the Filters chip once the panel is shut
const MORE = [
  { key: 'bando', label: 'Bando within', title: 'A derelict building that LiDAR says is still standing', opts: () => near, fmt: dist, chip: (v) => `Bando ≤ ${dist(v)}`, test: (s, v) => s.bando_m <= v },
  { key: 'gaps', label: 'Gaps', title: 'Openings between trees or buildings to fly through', opts: () => upper(gapsOf), fmt: (v) => `${v}+`, chip: (v) => `${v}+ gaps`, test: (s, v) => gapsOf(s) >= v },
  { key: 'lone', label: 'Lone trees', title: 'Single trees with room all round, to orbit and power loop', opts: () => upper((s) => s.lone_trees), fmt: (v) => `${v}+`, chip: (v) => `${v}+ lone trees`, test: (s, v) => s.lone_trees >= v },
  { key: 'tall', label: 'Tallest thing', title: 'The tallest tree or building in the spot, to dive', opts: () => upper((s) => s.max_height_m, 1), fmt: (v) => `${v} m+`, chip: (v) => `${v} m+ tall`, test: (s, v) => s.max_height_m >= v },
  { key: 'water', label: 'Water within', title: 'A river, lake or the sea', opts: () => near, fmt: dist, chip: (v) => `Water ≤ ${dist(v)}`, test: (s, v) => s.water_m <= v },
  { key: 'parking', label: 'Parking within', title: 'A car park, from the take-off', opts: () => near, fmt: dist, chip: (v) => `Parking ≤ ${dist(v)}`, test: (s, v) => s.parking_m <= v },
  { key: 'quiet', label: 'People about', title: 'Few shops, cafés and busy paths nearby', opts: () => [0.15], fmt: () => 'Quiet only', chip: () => 'Quiet', test: (s, v, p) => (p.components?.busy ?? 1) < v },
];
const moreOn = () => MORE.filter((m) => state.more[m.key] != null);
const moreActive = () => moreOn().length;

// ---------------------------------------------------------------- boot

async function boot() {
  // The last-used drone's spots download alongside meta.json rather than after it.
  const early = fetch(`${BASE}/spots_${state.profile}.geojson`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const metaRes = await fetch(`${BASE}/meta.json`);
  if (!metaRes.ok) {
    document.body.innerHTML = `<div style="padding:40px;color:#eee;font:16px system-ui">No data for “${esc(REGION)}” yet.
      Run <code>python -m spotfinder run --region ${esc(REGION)}</code> first.</div>`;
    return;
  }
  state.meta = await metaRes.json();
  ratings = new Ratings(REGION);
  $('#region-name').textContent = state.meta.region.name;
  const keys = profiles();
  const loaded = await Promise.all(keys.map(async (k) => (k === state.profile && await early)
    || fetch(`${BASE}/spots_${k}.geojson`).then((r) => r.json())));
  keys.forEach((k, i) => {
    loaded[i].features.forEach((f) => { f.properties.a3 = f.properties.legal?.a3_clear_pct ?? 0; });
    state.spots[k] = loaded[i];
  });
  if (!state.spots[state.profile]) state.profile = keys[0];
  buildProfileSwitch();

  lookup = new LegalLookup(state.meta);
  lookup.load(`${BASE}/legal_lookup.png`).catch(() => {});

  applySheet();
  initMap();
  wireUI();
  wireSheet();
  renderList();
  renderNow();
  syncControls();
  if (!SANDBOXED) loadRegions();
  setTimeout(() => loadDetails(state.profile), 300);
}

// The spot card's extras (gap and tree positions, the why breakdown, links) come in a second file
// per drone, fetched once the list is up, so the list itself shows sooner. Older data has them inline.
const detailsLoad = {}, detailsDone = new Set();
function loadDetails(key) {
  detailsLoad[key] ||= fetch(`${BASE}/spot_details_${key}.json`)
    .then((r) => (r.ok ? r.json() : {}))
    .catch(() => ({}))
    .then((byId) => {
      state.spots[key]?.features.forEach((f) => Object.assign(f.properties, byId[f.properties.id]));
      detailsDone.add(key);
    });
  return detailsLoad[key];
}

// "Now" banner: wind and rain for the next hour at the middle of the region, and today's light.
async function renderNow() {
  const box = $('#now-box');
  const [lon, lat] = state.meta.region.center;
  const hm = (d) => d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  const t = sunTimes(lon, lat);
  const light = Date.now() < t.sunset.getTime()
    ? `Golden hour from ${hm(t.goldenEveningStart)} · sunset ${hm(t.sunset)}`
    : `Sunrise tomorrow about ${hm(new Date(t.sunrise.getTime() + 86400e3))}`;
  box.replaceChildren(el('span', {}, light));
  box.hidden = false;
  if (SANDBOXED) return;
  const profile = state.profile;
  try {
    const w = await nowConditions(lon, lat, profile);
    if (profile !== state.profile) return;
    const who = state.meta.profiles[profile].short || state.meta.profiles[profile].label;
    const verdict = { go: `good for the ${who}`, meh: `marginal for the ${who}`, no: `not great for the ${who}` }[w.cls];
    box.replaceChildren(
      el('span', { class: w.cls }, el('b', {}, 'Now: '), `wind ${Math.round(w.wind)} mph, gusts ${Math.round(w.gust)}, rain ${w.rain}% · `, el('b', { class: 'v' }, verdict)),
      el('span', {}, light));
  } catch {
    /* no forecast: the light still shows */
  }
}

function buildProfileSwitch() {
  const box = $('#profile-switch');
  box.replaceChildren(...profiles().map((k) => {
    const p = state.meta.profiles[k];
    return el('button', { 'data-profile': k, role: 'tab', title: p.drone || p.label },
      el('span', { class: 'long' }, p.label), el('span', { class: 'short' }, p.short || p.label));
  }));
  $$('#profile-switch button').forEach((b) => b.addEventListener('click', () => setProfile(b.dataset.profile)));
}

// Other regions, when the pipeline has made more than one.
async function loadRegions() {
  try {
    const res = await fetch(`${DATA}/regions.json`);
    if (!res.ok) return;
    const regions = await res.json();
    if (!Array.isArray(regions) || regions.length < 2) return;
    const sel = el('select', { 'aria-label': 'Region' },
      regions.map((g) => el('option', { value: g.key, selected: g.key === REGION }, g.name)));
    sel.addEventListener('change', () => {
      const u = new URL(location.href);
      u.searchParams.set('region', sel.value);
      u.hash = '';
      location.href = u.href;
    });
    $('#region-name').replaceChildren(sel);
  } catch {
    /* one region only */
  }
}

// ---------------------------------------------------------------- bottom sheet (phones)

function sheetHeights() {
  const vh = window.innerHeight;
  return { peek: Math.round(Math.min(170, vh * 0.3)), half: Math.round(vh * 0.5), full: vh - 58 };
}

function applySheet(px) {
  if (!PHONE.matches) {
    document.documentElement.style.removeProperty('--sheet-h');
    return;
  }
  const h = px ?? sheetHeights()[state.sheet];
  document.documentElement.style.setProperty('--sheet-h', `${Math.round(h)}px`);
}

function setSheet(name) {
  state.sheet = name;
  applySheet();
}

// Drag the handle or either header to resize; a flick moves one step; a tap on the handle toggles.
function wireSheet() {
  const panel = $('#panel');
  let drag = null;
  const zones = [$('#sheet-handle'), ...$$('.drag-zone')];
  zones.forEach((z) => z.addEventListener('pointerdown', (e) => {
    if (!PHONE.matches || e.button > 0) return;
    if (e.target.closest('input')) return;
    const now = performance.now();
    drag = { id: e.pointerId, zone: z, y0: e.clientY, h0: panel.getBoundingClientRect().height, moved: false, lastY: e.clientY, lastT: now, v: 0 };
  }));
  window.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dy = e.clientY - drag.y0;
    if (!drag.moved) {
      if (Math.abs(dy) < 8) return;
      drag.moved = true;
      panel.classList.add('dragging');
      document.documentElement.classList.add('sheet-dragging');
      try { drag.zone.setPointerCapture(e.pointerId); } catch { /* fine without */ }
    }
    const now = performance.now();
    drag.v = 0.7 * drag.v + 0.3 * ((e.clientY - drag.lastY) / Math.max(1, now - drag.lastT));
    drag.lastY = e.clientY;
    drag.lastT = now;
    const H = sheetHeights();
    applySheet(Math.max(H.peek - 50, Math.min(H.full, drag.h0 - dy)));
  });
  const end = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    if (!d.moved) return; // a tap: let the click through
    panel.classList.remove('dragging');
    document.documentElement.classList.remove('sheet-dragging');
    const swallow = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
    window.addEventListener('click', swallow, { capture: true, once: true });
    setTimeout(() => window.removeEventListener('click', swallow, { capture: true }), 60);
    const H = sheetHeights();
    const h = panel.getBoundingClientRect().height;
    let name;
    if (d.v < -0.45) name = h > H.half ? 'full' : 'half';
    else if (d.v > 0.45) name = h < H.half ? 'peek' : 'half';
    else name = Object.entries(H).sort((a, b) => Math.abs(a[1] - h) - Math.abs(b[1] - h))[0][0];
    setSheet(name);
  };
  window.addEventListener('pointerup', end);
  window.addEventListener('pointercancel', end);
  $('#sheet-handle').addEventListener('click', () => setSheet(state.sheet === 'half' ? 'full' : 'half'));
  $('#sheet-handle').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      setSheet(state.sheet === 'half' ? 'full' : 'half');
    }
  });
  window.addEventListener('resize', () => applySheet());
  PHONE.addEventListener('change', () => {
    applySheet();
    map?.resize();
  });
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
    zoom: PHONE.matches ? 9.4 : 10.2,
    maxZoom: 18.5,
    attributionControl: false,
    hash: false,
    dragRotate: !TOUCH.matches,
  });
  // On a phone the list sheet covers the bottom half, so centre the region in the part you can see.
  if (PHONE.matches) map.setPadding({ top: 0, right: 0, bottom: sheetHeights().half, left: 0 });
  if (params.has('bench')) window.__map = map;  // for scripts/bench.mjs
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: 'Heights © Environment Agency (OGL)' }), 'bottom-right');
  if (!PHONE.matches) map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
  if (!SANDBOXED) {
    const geo = new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: true }, trackUserLocation: false });
    map.addControl(geo, 'top-right');
    geo.on('geolocate', (e) => {
      state.me = [e.coords.longitude, e.coords.latitude];
      if (state.sort === 'near') renderList();
    });
  }
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric', maxWidth: 90 }), 'bottom-left');

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
  map.on('zoom', () => {
    if (heatHiLoaded || !heatHiWanted() || !map.getSource('heat')) return;
    heatHiLoaded = true;
    map.getSource('heat').updateImage({ url: `${BASE}/heat_${state.profile}.png`, coordinates: state.meta.region.heat_corners });
  });
  map.on('click', onMapClick);
  map.on('contextmenu', (e) => {
    e.preventDefault();
    showCheck(e.lngLat, e.point);
  });
  wireLongPress();
  loadPlaceLabels();
}

// Long-press on a touch screen = "can I fly here?" at that point.
function wireLongPress() {
  let timer = null, start = null;
  const cancel = () => {
    clearTimeout(timer);
    timer = null;
  };
  map.on('touchstart', (e) => {
    cancel();
    if (e.originalEvent.touches.length !== 1) return;
    start = e.point;
    timer = setTimeout(() => {
      timer = null;
      showCheck(e.lngLat, e.point);
    }, 550);
  });
  map.on('touchmove', (e) => {
    if (timer && start && Math.hypot(e.point.x - start.x, e.point.y - start.y) > 10) cancel();
  });
  map.on('touchend', cancel);
  map.on('touchcancel', cancel);
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
  // Zoomed out: a coarse heatmap (max of 4 x 4 cells) so good areas stay visible.
  // Zoomed in: the fine heatmap, fading as the streets get readable, plus the legal overlay.
  map.addSource('heatlo', { type: 'image', url: `${BASE}/heatlo_${state.profile}.png`, coordinates: m.heat_lo_corners || m.heat_corners });
  map.addLayer({ id: 'heatlo', type: 'raster', source: 'heatlo', maxzoom: 12, paint: { 'raster-opacity': 0.85, 'raster-fade-duration': 0 } }, before);
  // The fine heatmap only draws from zoom 12, so it isn't fetched until the map gets close.
  heatHiLoaded = heatHiWanted();
  map.addSource('heat', { type: 'image', url: heatHiLoaded ? `${BASE}/heat_${state.profile}.png` : BLANK_PNG, coordinates: m.heat_corners });
  map.addLayer({ id: 'heat', type: 'raster', source: 'heat', minzoom: 12, paint: { 'raster-opacity': ['interpolate', ['linear'], ['zoom'], 12, 0.8, 16, 0.3], 'raster-fade-duration': 0 } }, before);
  map.addSource('legal', { type: 'image', url: `${BASE}/legal_${state.profile}.png`, coordinates: m.legal_corners });
  map.addLayer({ id: 'legal', type: 'raster', source: 'legal', minzoom: 11.5, paint: { 'raster-opacity': ['interpolate', ['linear'], ['zoom'], 11.5, 0, 12.5, 0.85], 'raster-resampling': 'nearest', 'raster-fade-duration': 0 } }, before);
  map.addSource('zones', { type: 'geojson', data: `${BASE}/zones.geojson` });
  map.addLayer({
    id: 'zones-fill', type: 'fill', source: 'zones',
    paint: { 'fill-color': ['match', ['get', 'kind'], 'Danger area', '#ff9f1c', '#e5383b'], 'fill-opacity': 0.06 },
  });
  map.addLayer({
    id: 'zones-line', type: 'line', source: 'zones',
    paint: { 'line-color': ['match', ['get', 'kind'], 'Danger area', '#ff9f1c', '#e5383b'], 'line-width': 1.5, 'line-dasharray': [3, 2] },
  });
  // Mapped bandos: a solid dot where LiDAR still sees something standing, a ring where little is left.
  if (hasBandos()) {
    map.addSource('bandos', { type: 'geojson', data: bandosData || { type: 'FeatureCollection', features: [] } });
    map.addLayer({
      id: 'bandos-site', type: 'circle', source: 'bandos', minzoom: 10.5, filter: ['!=', ['get', 's'], 1],
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 2.5, 15, 5], 'circle-opacity': 0,
        'circle-stroke-color': BANDO_COLOUR, 'circle-stroke-width': 1.5, 'circle-stroke-opacity': 0.8,
      },
    });
    map.addLayer({
      id: 'bandos-dots', type: 'circle', source: 'bandos', filter: ['==', ['get', 's'], 1],
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 2.5, 12, 4.5, 15, 7], 'circle-color': BANDO_COLOUR,
        'circle-stroke-color': '#1b1030', 'circle-stroke-width': 1,
      },
    });
    for (const id of ['bandos-site', 'bandos-dots']) {
      map.on('mouseenter', id, () => { if (!state.checking) map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', id, () => { map.getCanvas().style.cursor = ''; });
    }
  }
  map.addSource('sel', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addLayer({ id: 'sel-area', type: 'fill', source: 'sel', filter: ['==', ['get', 'k'], 'area'], paint: { 'fill-color': '#ffffff', 'fill-opacity': 0.1 } });
  map.addLayer({ id: 'sel-halo', type: 'line', source: 'sel', filter: ['==', ['get', 'k'], 'area'], paint: { 'line-color': '#ffffff', 'line-width': 3.5, 'line-opacity': 0.8 } });
  map.addLayer({ id: 'sel-line', type: 'line', source: 'sel', filter: ['==', ['get', 'k'], 'area'], paint: { 'line-color': '#1b2328', 'line-width': 1.5, 'line-dasharray': [3, 2] } });
  map.addLayer({
    id: 'sel-path', type: 'line', source: 'sel', filter: ['==', ['get', 'k'], 'path'], minzoom: 12.5,
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': '#5cc8ff', 'line-width': ['interpolate', ['linear'], ['zoom'], 13, 1.5, 17, 4], 'line-opacity': 0.9 },
  });
  map.addLayer({
    id: 'sel-pts', type: 'circle', source: 'sel', filter: ['==', ['geometry-type'], 'Point'], minzoom: 12.5,
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 13, ['match', ['get', 'k'], 'takeoff', 5, 'move', 4, 2], 17, ['match', ['get', 'k'], 'takeoff', 9, 'move', 8, 5.5]],
      'circle-color': ['match', ['get', 'k'], 'gap', '#ff3d7f', 'lone', '#ffc400', 'takeoff', '#22c55e',
        'move', ['match', ['get', 'kind'], 'gap', '#ff3d7f', 'orbit', '#ffc400', 'dive', '#a78bfa', 'arch', '#fb923c', '#22d3ee'], '#fff'],
      'circle-stroke-color': ['match', ['get', 'k'], 'takeoff', '#ffffff', '#1b2328'],
      'circle-stroke-width': ['match', ['get', 'k'], 'takeoff', 2.5, 1],
    },
  });
  map.addSource('spots', { type: 'geojson', data: { type: 'FeatureCollection', features: state.visible || [] } });
  map.addLayer({
    id: 'spots-dots', type: 'circle', source: 'spots',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 2.2, 12, 4, 15, 6.5],
      'circle-color': ['interpolate', ['linear'], ['get', 'score'], 65, '#ffc400', 80, '#ff7030', 95, '#d6246e'],
      'circle-opacity': ['interpolate', ['linear'], ['zoom'], 8, 0.75, 12, 1],
      'circle-stroke-color': '#14191d', 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 8, 0.8, 12, 1.2],
    },
  });
  map.on('mouseenter', 'spots-dots', () => { if (!state.checking) map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'spots-dots', () => { map.getCanvas().style.cursor = ''; });
  hoverTip('spots-dots', (p) => `<b>${esc(p.title)}</b> · ${p.score}`);
  for (const id of ['bandos-site', 'bandos-dots']) {
    hoverTip(id, (p) => `<b>${esc(p.n || p.k)}</b><br>${p.n ? `${esc(p.k)} · ` : ''}${BANDO_STATE[p.s](p.h)}${p.m ? ' · protected site' : ''}`);
  }
  applyLayerToggles();
  drawPins();
  drawSeeds();
  if (state.selected) {
    const f = feature(state.selected);
    if (f) showSelection(f);
  }
  if (!state.booted) {
    state.booted = true;
    if (location.hash.length > 1) openFromHash();
  }
  // Start with the attribution folded away; the (i) button opens it.
  $('.maplibregl-ctrl-attrib')?.classList.remove('maplibregl-compact-show');
}

// With a mouse, hovering a dot names it.
let hoverPop = null;
function hoverTip(layer, html) {
  if (TOUCH.matches || !map.getLayer(layer)) return;
  map.on('mousemove', layer, (e) => {
    const f = e.features?.[0];
    if (!f || state.checking) return;
    hoverPop ||= new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 9, className: 'hover-tip', maxWidth: '260px' });
    hoverPop.setLngLat(f.geometry.coordinates).setHTML(html(f.properties)).addTo(map);
  });
  map.on('mouseleave', layer, () => hoverPop?.remove());
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
  $('#lyr-legal-row').hidden = !isA3();
  $('#lyr-bandos-row').hidden = !hasBandos();
  syncBandos();
}

function setProfile(key) {
  if (key === state.profile || !state.spots[key]) return;
  state.profile = key;
  state.like = null;
  for (const m of MORE) if (state.more[m.key] != null && !m.opts().includes(state.more[m.key])) delete state.more[m.key];
  savePrefs();
  loadDetails(key);
  showList();
  const m = state.meta.region;
  if (map.getSource('heat')) {
    if (heatHiLoaded) map.getSource('heat').updateImage({ url: `${BASE}/heat_${key}.png`, coordinates: m.heat_corners });
    map.getSource('heatlo').updateImage({ url: `${BASE}/heatlo_${key}.png`, coordinates: m.heat_lo_corners || m.heat_corners });
    map.getSource('legal').updateImage({ url: `${BASE}/legal_${key}.png`, coordinates: m.legal_corners });
  }
  applyLayerToggles();
  syncControls();
  renderList();
  renderNow();
  drawSeeds();
}

function savePrefs() {
  save(PREFS_KEY, { profile: state.profile, sort: state.sort === 'near' ? 'score' : state.sort, base: state.base, a3only: state.a3only, bandos: state.bandos });
}

// ---------------------------------------------------------------- list

function orderedSpots() {
  const feats = [...state.spots[state.profile].features];
  let note = '';
  let key = (f) => f.properties.rank;
  let desc = false;
  let label = 'Best spots';
  if (state.like) {
    const target = feats.find((f) => f.properties.id === state.like);
    if (target) {
      const sim = likeThis(feats, target);
      key = (f) => sim(f);
      desc = true;
      label = 'Most like it';
      note = `Spots most like “${esc(target.properties.title)}”. <button class="linklike" data-clear-like>Back to best</button>`;
    }
  } else if (state.sort === 'taste') {
    const model = learnTaste(feats, ratings, state.profile);
    label = 'By your taste';
    if (model.ready) {
      key = (f) => model.predict(f) * 0.7 + (f.properties.score / 100) * 0.3;
      desc = true;
      const top = Object.entries(model.weights).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([k]) => k);
      note = `Learned from your ${model.pos} 👍 and ${model.neg} 👎. You seem to like: ${top.map(nice).join(', ').toLowerCase()}. `
        + (SANDBOXED
          ? 'To teach the whole-region ranking, copy your ratings (bottom of the list) and paste them back to Claude.'
          : 'To teach the whole-region ranking, download your ratings (bottom of the list) into config/ratings/ and re-run the scorer.');
    } else {
      note = `Rate a few spots 👍 / 👎 (at least 2 good, 1 not) and this list sorts itself by your taste. So far: ${model.pos} 👍, ${model.neg} 👎.`;
    }
  } else if (state.sort === 'near') {
    label = 'Closest first';
    if (state.me) {
      key = (f) => haversine(state.me, f.geometry.coordinates);
    } else {
      note = 'Finding you… (allow location access)';
      if (!state.locating && navigator.geolocation) {
        state.locating = true;
        navigator.geolocation.getCurrentPosition((pos) => {
          state.locating = false;
          state.me = [pos.coords.longitude, pos.coords.latitude];
          renderList();
        }, () => {
          state.locating = false;
          state.sort = 'score';
          syncControls();
          renderList();
        });
      }
    }
  }
  const q = state.query.trim().toLowerCase();
  const hay = (p) => `${p.title} ${p.locality} ${p.gridref} ${p.park || ''} ${ratings.note(p.id)}`.toLowerCase();
  let list = feats.filter((f) => !q || hay(f.properties).includes(q));
  if (state.a3only && isA3()) list = list.filter((f) => f.properties.a3 >= 50);
  if (state.kind && !kindsFor(state.profile).some(([k]) => k === state.kind)) state.kind = null;
  if (state.kind) list = list.filter((f) => hasKind(f, state.kind));
  if (state.saved && !feats.some(liked)) state.saved = false;
  if (state.saved) list = list.filter(liked);
  for (const m of MORE) {
    const v = state.more[m.key];
    if (v != null) list = list.filter((f) => f.properties.stats && m.test(f.properties.stats, v, f.properties));
  }
  list.sort((a, b) => (desc ? key(b) - key(a) : key(a) - key(b)));
  // spots you've binned sink to the bottom
  list = [...list.filter((f) => ratings.get(state.profile, f.properties.id) >= 0), ...list.filter((f) => ratings.get(state.profile, f.properties.id) < 0)];
  return { list, note, label };
}

function subline(p) {
  const s = p.stats;
  if (!s) return '';
  if (!isA3()) return `${s.gaps_whoop} whoop gaps · ${s.bld_pct}% buildings`;
  return `${s.gaps} gaps · ${s.lone_trees} lone trees`;
}

function spotRow(f, i) {
  const p = f.properties;
  const r = ratings.get(state.profile, p.id);
  const place = p.park && !p.title.includes(p.park) ? `in ${p.park}` : p.locality;
  const sub = [place, state.me ? metres(haversine(state.me, f.geometry.coordinates)) : null, subline(p)].filter(Boolean).join(' · ');
  return el('li', { class: `row-item${state.selected === p.id ? ' on' : ''}${r < 0 ? ' rated-down' : ''}`, 'data-id': p.id },
    thumbBox(p.preview, el('i', {}, i + 1)),
    el('div', {},
      el('div', { class: 'spot-name' }, p.title, r > 0 ? el('span', { class: 'rated-up', title: 'You rated it good' }, '▲') : null,
        ratings.note(p.id) ? el('span', { class: 'has-note', title: ratings.note(p.id) }, '📝') : null),
      el('div', { class: 'spot-sub' }, sub)),
    el('div', { class: 'row-side' },
      el('span', { class: 'score', style: `--c:${scoreColour(p.score)}` }, p.score),
      isA3() ? legalBadge(p, true) : null));
}

// The spot's little top-down picture, with the rank or a star on it.
function thumbBox(preview, tag) {
  return el('span', { class: `thumb${preview ? '' : ' none'}` },
    preview ? el('img', { alt: '', 'data-thumb': `${BASE}/${preview.file}`, decoding: 'async' }) : null, tag);
}

function renderFilters() {
  const box = $('#filters');
  const x = box.scrollLeft;
  const chip = (key, on, title, label, n) => el('button', { class: 'fchip', 'data-filter': key, 'aria-pressed': String(on), title },
    label, n != null ? el('span', { class: 'n' }, n) : null);
  const saved = state.spots[state.profile].features.filter(liked).length;
  // shut, the chip says which filters are on; open, the panel does
  const on = moreOn();
  const more = chip('more', on.length > 0, 'Filter by bandos nearby, gaps, lone trees, height, water, parking, people about',
    [el('span', { class: 'ico', html: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h11v2H3zm14 0h4v2h-4zm-3-2h2v6h-2zM3 16h5v2H3zm9 0h9v2h-9zm-3-2h2v6H9z"/></svg>' }),
      on.length && !state.moreOpen ? on.map((m) => m.chip(state.more[m.key])).join(' · ') : 'Filters'],
    on.length && state.moreOpen ? on.length : null);
  more.setAttribute('aria-expanded', String(state.moreOpen));
  more.classList.add('more');
  box.replaceChildren(...[
    more,
    isA3() ? chip('a3', state.a3only, 'Only spots that are mostly 150 m clear of houses and parks', 'A3-clear only') : null,
    saved ? chip('saved', state.saved, 'Spots you rated 👍', 'Your 👍', saved) : null,
    hasBandos() ? chip('bandos', state.bandoList, 'List the derelict buildings themselves', 'Bandos', bandoCount()) : null,
    ...kindsFor(state.profile).map(([k, n]) => chip(`kind:${k}`, state.kind === k, `Spots with ${KINDS[k].toLowerCase()}`, KINDS[k], n)),
  ].filter(Boolean));
  box.hidden = !box.children.length;
  box.scrollLeft = x;
  renderMore();
}

// On a phone the panel needs the whole sheet; closing it puts the sheet back so the map shows the result.
let sheetBeforeMore = null;
function toggleMore(open) {
  state.moreOpen = open;
  if (PHONE.matches && open && state.sheet !== 'full') {
    sheetBeforeMore = state.sheet;
    setSheet('full');
  } else if (!open && sheetBeforeMore) {
    if (PHONE.matches && state.sheet === 'full') setSheet(sheetBeforeMore);  // unless moved by hand since
    sheetBeforeMore = null;
  }
  renderFilters();
}

function renderMore() {
  const box = $('#more-filters');
  box.hidden = !state.moreOpen;
  if (!state.moreOpen) return;
  const row = (m) => {
    const opts = m.opts();
    const cur = state.more[m.key];
    return el('div', { class: 'mf-row', title: m.title },
      el('span', { class: 'mf-label' }, m.label),
      el('div', { class: 'seg small' },
        el('button', { 'data-more': m.key, 'data-v': '', class: cur == null ? 'on' : '', 'aria-pressed': String(cur == null) }, 'Any'),
        ...opts.map((v) => el('button', { 'data-more': m.key, 'data-v': String(v), class: cur === v ? 'on' : '', 'aria-pressed': String(cur === v) }, m.fmt(v)))));
  };
  box.replaceChildren(...MORE.map(row),
    el('div', { class: 'mf-foot' },
      moreActive() ? el('button', { class: 'linklike', 'data-more-clear': '' }, 'Clear these') : el('span'),
      el('button', { class: 'linklike', 'data-more-done': '' }, 'Done')));
}

function renderList() {
  if (state.bandoList && hasBandos()) return renderBandoList();
  const { list, note, label } = orderedSpots();
  renderFilters();
  const noteBox = $('#taste-note');
  noteBox.hidden = !note;
  noteBox.innerHTML = note;
  noteBox.querySelector('[data-clear-like]')?.addEventListener('click', () => {
    state.like = null;
    syncControls();
    renderList();
  });
  renderSeeds(label);
  renderBandoHits();
  $('#spot-list').replaceChildren(...list.map(spotRow));
  const empty = $('#list-empty');
  empty.hidden = list.length > 0;
  if (!list.length) {
    empty.replaceChildren(state.query ? `No spots match “${state.query}”.` : 'No spots with these filters.',
      el('br'), el('button', { class: 'linklike', onclick: clearFilters }, 'Clear filters'));
  }
  const total = state.spots[state.profile].features.length;
  $('#list-count').textContent = list.length === total ? `${total} spots` : `${list.length} of ${total}`;
  renderFoot();
  state.order = list;
  state.visible = list;
  map?.getSource('spots')?.setData({ type: 'FeatureCollection', features: list });
  syncBandos();
  drawPins();
  watchThumbs($('#list-scroll'), $('#list-scroll'));
}

function clearFilters() {
  state.query = '';
  state.a3only = false;
  state.kind = null;
  state.bandoList = false;
  state.saved = false;
  state.more = {};
  $('#search').value = '';
  $('#search-clear').hidden = true;
  savePrefs();
  syncControls();
  renderList();
}

function renderFoot() {
  $('#list-foot').replaceChildren(...[
    SANDBOXED ? null : el('a', { href: `${BASE}/spots_${state.profile}.gpx`, download: `fpv-spots-${REGION}-${state.profile}.gpx` }, 'Spots as GPX'),
    el('button', { class: 'linklike', onclick: exportRatings }, SANDBOXED ? 'Copy my ratings and notes' : 'Download my ratings and notes'),
    el('button', { class: 'linklike', onclick: openAbout }, 'How it works'),
  ].filter(Boolean));
}

function drawPins() {
  if (!map || !map.getSource?.('spots')) return;
  state.pins.forEach((m) => m.remove());
  state.pins = [];
  const show = $('#lyr-spots').checked;
  (state.order || []).slice(0, 40).forEach((f, i) => {
    const p = f.properties;
    const node = el('div', { class: `mk${state.selected === p.id ? ' on' : ''}`, title: p.title, 'data-id': p.id },
      el('div', { class: 'pin', style: `--c:${scoreColour(p.score)}` }, i + 1));
    node.addEventListener('click', (e) => {
      e.stopPropagation();
      if (state.checking) return;
      select(p.id, false);
    });
    if (!show) node.style.display = 'none';
    state.pins.push(new maplibregl.Marker({ element: node }).setLngLat(f.geometry.coordinates).addTo(map));
  });
}

function feature(id) {
  return state.spots[state.profile].features.find((x) => x.properties.id === id);
}

// ---------------------------------------------------------------- seeds ("your spots")

function seedsFor(profile) {
  return (state.meta.seeds || []).filter((s) => s.profile === profile);
}

function seedSpot(s) {
  const a3 = isA3(s.profile);
  return {
    type: 'Feature', geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
    properties: {
      id: `seed-${s.name}`, title: s.name, locality: 'your spot', score: s.score, rules: null, taste: null,
      components: s.components, drivers: s.note ? [s.note] : [],
      legal: {
        summary: a3
          ? (s.a3_frac >= 0.9 ? 'Clear of the A3 distances.' : 'Not A3-clear for an 800 g quad: it\'s within 150 m of a park or houses. Fine for a sub-250 g drone under A1.')
          : 'Open A1: fine for a sub-250 g drone outside restriction zones; never over crowds.',
        flags: s.flags, a3_clear_pct: Math.round(100 * (s.a3_frac ?? 0)),
      },
      preview: s.preview, gaps_list: [], lone_list: [],
    },
  };
}

function renderSeeds(label) {
  const box = $('#seeds-box');
  box.hidden = false;
  const seeds = seedsFor(state.profile);
  const showSeeds = seeds.length && !state.query && !state.like;
  box.replaceChildren(...[
    showSeeds ? el('div', { class: 'section-label' }, 'Your spots') : null,
    showSeeds ? el('ol', { class: 'seed-list' }, seeds.map((s) => {
      const f = seedSpot(s);
      const li = el('li', { class: 'row-item', title: 'One of your favourite spots: the ranking looks for places like it' },
        thumbBox(s.preview, el('i', { class: 'star' }, '★')),
        el('div', {}, el('div', { class: 'spot-name' }, s.name), el('div', { class: 'spot-sub' }, 'Your spot · the style it looks for')),
        el('div', { class: 'row-side' },
          el('span', { class: 'score', style: `--c:${scoreColour(s.score ?? 0)}` }, s.score ?? '–'),
          isA3() ? legalBadge(f.properties, true) : null));
      li.addEventListener('click', () => openSeed(s));
      return li;
    })) : null,
    el('div', { class: 'section-label' }, label),
  ].filter(Boolean));
}

function drawSeeds() {
  state.seedMarkers.forEach((m) => m.remove());
  state.seedMarkers = seedsFor(state.profile).map((s) => {
    const node = el('div', { class: 'mk', title: `Your spot: ${s.name}` }, el('div', { class: 'seed-pin' }));
    node.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!state.checking) openSeed(s);
    });
    return new maplibregl.Marker({ element: node }).setLngLat([s.lon, s.lat]).addTo(map);
  });
}

function openSeed(s) {
  const f = seedSpot(s);
  state.selected = null;
  markSelected(null);
  map.getSource('sel')?.setData({ type: 'FeatureCollection', features: [] });
  showSpot(f, true);
  if (PHONE.matches) setSheet('half');
  flyToSpot(f, 16);
}

// ---------------------------------------------------------------- list <-> spot

function select(id, fromList, push = true) {
  const f = feature(id);
  if (!f) return;
  const wasOpen = !$('#view-spot').hidden;
  state.selected = id;
  setHash(`${state.profile}.${id}`, push && !wasOpen);
  markSelected(id);
  showSelection(f);
  showSpot(f, false);
  const key = state.profile;
  if (!detailsDone.has(key)) {
    loadDetails(key).then(() => {
      if (state.selected === id && state.profile === key) {
        showSelection(f);
        showSpot(f, false);
      }
    });
  }
  if (PHONE.matches) setSheet('half');
  flyToSpot(f, fromList ? 15.5 : Math.max(map.getZoom(), 14.5));
}

function flyToSpot(f, zoom) {
  const H = sheetHeights();
  map.flyTo({
    center: f.geometry.coordinates, zoom, essential: true,
    padding: PHONE.matches ? { top: 40, bottom: H.half, left: 0, right: 0 } : { top: 0, bottom: 0, left: 0, right: 0 },
    duration: 1400,
  });
}

function markSelected(id) {
  $$('#spot-list .row-item').forEach((li) => li.classList.toggle('on', li.dataset.id === id));
  state.pins.forEach((m) => m.getElement().classList.toggle('on', m.getElement().dataset.id === id));
}

// The moves as a path on the map. Between two arches side by side the line swings out round the pier,
// alternately either side of the viaduct, like the 3D view draws it; a move whose way in bends round
// something carries the bend after its own point ([lon, lat, dx, dy]).
function weave(moves) {
  const out = [];
  let n = 0;
  moves.forEach((m, i) => {
    const prev = moves[i - 1];
    if (m[5]) out.push([m[5][0], m[5][1]]);
    if (m[0] === 'arch' && prev?.[0] === 'arch') {
      const ex = m[3] - prev[3], ey = m[4] - prev[4], len = Math.hypot(ex, ey) || 1, side = (n++ % 2 ? 1 : -1) * 9;
      const ox = (-ey / len) * side, oy = (ex / len) * side; // metres across the viaduct
      const lat = (prev[2] + m[2]) / 2;
      out.push([(prev[1] + m[1]) / 2 + ox / (111320 * Math.cos((lat * Math.PI) / 180)), lat + oy / 110540]);
    }
    out.push([m[1], m[2]]);
  });
  return out;
}

function showSelection(f) {
  const p = f.properties;
  const feats = [{ type: 'Feature', geometry: circlePolygon(f.geometry.coordinates, state.meta.profiles[state.profile].radius_m), properties: { k: 'area' } }];
  (p.gaps_list || []).forEach((g) => feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [g[0], g[1]] }, properties: { k: 'gap' } }));
  (p.lone_list || []).forEach((t) => feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [t[0], t[1]] }, properties: { k: 'lone' } }));
  const moves = p.line?.moves || [];
  if (moves.length) { // the suggested line: take-off, then each move in order
    const start = p.takeoff ? [[p.takeoff.lon, p.takeoff.lat]] : [];
    feats.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: [...start, ...weave(moves)] }, properties: { k: 'path' } });
    moves.forEach((m, i) => feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [m[1], m[2]] }, properties: { k: 'move', n: i + 1, kind: m[0] } }));
  }
  if (p.takeoff) feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [p.takeoff.lon, p.takeoff.lat] }, properties: { k: 'takeoff' } });
  map.getSource('sel')?.setData({ type: 'FeatureCollection', features: feats });
}

function showSpot(f, isSeed) {
  if ($('#view-spot').hidden) state.listScroll = $('#list-scroll').scrollTop;
  state.detail?.dispose();
  $('#view-list').hidden = true;
  $('#view-spot').hidden = false;
  $('#panel').dataset.view = 'spot';
  const order = state.order || [];
  const i = isSeed ? -1 : order.findIndex((g) => g.properties.id === f.properties.id);
  const stepTo = (j) => (j >= 0 && j < order.length ? () => select(order[j].properties.id, false) : null);
  state.detail = renderSpot({
    head: $('#spot-head'), body: $('#detail-body'),
    spot: f, meta: state.meta, profile: state.profile, ratings, base: BASE, isSeed,
    total: state.spots[state.profile].features.length,
    nav: i < 0 ? null : { prev: stepTo(i - 1), next: stepTo(i + 1) },
    onRate: (v) => {
      ratings.set(state.profile, f.properties.id, v, { title: f.properties.title });
      renderList();
    },
    onLike: () => {
      state.like = f.properties.id;
      showList();
      renderList();
      syncControls();
      $('#list-scroll').scrollTop = 0;
    },
    onBack: () => goBack(),
    onNote: () => renderList(),
    nearBandos: hasBandos() ? () => loadBandos().then(() => bandosNear(f.geometry.coordinates, 1000, 4)) : null,
    onBando: showBandoOnMap,
  });
  $('#spot-scroll').scrollTop = 0;
}

function goBack() {
  if (!SANDBOXED && history.state?.spot) history.back();
  else showList();
}

function showList() {
  const last = state.selected;
  state.detail?.dispose();
  state.detail = null;
  state.selected = null;
  $('#view-spot').hidden = true;
  $('#view-list').hidden = false;
  $('#panel').dataset.view = 'list';
  const sc = $('#list-scroll');
  sc.scrollTop = state.listScroll || 0;
  // after stepping through spots, keep the last one looked at in view
  const row = last && sc.querySelector(`.row-item[data-id="${last}"]`);
  if (row) {
    const r = row.getBoundingClientRect(), b = sc.getBoundingClientRect();
    if (r.top < b.top || r.bottom > b.bottom) sc.scrollTop += r.top - b.top - b.height / 3;
  }
  map?.getSource('sel')?.setData({ type: 'FeatureCollection', features: [] });
  markSelected(null);
  setHash('', false);
}

// Spot links look like #freestyle5.123-456. Opening a spot adds a history entry so the
// phone's back button returns to the list.
function setHash(h, push) {
  const url = location.pathname + location.search + (h ? `#${h}` : '');
  try {
    if (push && !SANDBOXED) history.pushState({ spot: true }, '', url);
    else history.replaceState(h ? history.state : null, '', url);
  } catch {
    /* some embeds refuse history changes */
  }
}

function openFromHash() {
  const [profile, id] = location.hash.slice(1).split(/[./]/);
  if (!state.spots[profile]) return;
  if (profile !== state.profile) setProfile(profile);
  if (!id) return;
  if (feature(id)) select(id, true, false);
  else {
    // an old link: the spot isn't in this ranking any more
    setHash('', false);
    const note = $('#taste-note');
    note.hidden = false;
    note.textContent = 'That shared spot isn\'t in the latest ranking (the scores have been updated since). Here are the best ones now.';
  }
}

window.addEventListener('popstate', () => {
  if (!state.meta) return;
  if (location.hash.length > 1) openFromHash();
  else if (!$('#view-spot').hidden) showList();
});

// ---------------------------------------------------------------- can I fly here?

function setChecking(on) {
  state.checking = on;
  $('#btn-check').setAttribute('aria-pressed', String(on));
  $('#check-hint').hidden = !on;
  $('#map').classList.toggle('checking', on);
  if (!on) closeCheck();
}

function onMapClick(e) {
  if (state.checking) {
    showCheck(e.lngLat, e.point);
    return;
  }
  const hit = map.getLayer('spots-dots') && map.queryRenderedFeatures(e.point, { layers: ['spots-dots'] })[0];
  if (hit) return select(hit.properties.id, false);
  const b = bandoAt(e.point);
  if (b) showBando(b);
  else if (bandoOpen) closeCheck();
}

// The answer goes in a card under the button (a popup on the map gets cut off on phones),
// with a dot where you checked.
function showCheck(lngLat, point) {
  const { lng, lat } = lngLat;
  const zones = map.getLayer('zones-fill') ? map.queryRenderedFeatures(point, { layers: ['zones-fill'] }).map((z) => z.properties.name) : [];
  const v = verdicts(state.meta, lookup?.at(lng, lat), [...new Set(zones)]);
  const card = $('#check-card');
  card.replaceChildren(...[
    el('button', { class: 'x', 'aria-label': 'Close', onclick: () => (state.checking ? closeCheck() : setChecking(false)) }, '×'),
    el('h4', {}, 'Can I fly here?'),
    ...(v
      ? [...v.verdicts.map((x) => el('div', { class: 'check-row' },
        el('span', { class: `ico ${x.ok ? 'ok' : 'bad'}` }, x.ok ? '✓' : '✕'),
        el('span', {}, el('b', {}, x.who), el('br'), x.text))),
      v.flags.length ? el('ul', { class: 'legal-flags' }, v.flags.map((t) => el('li', {}, t))) : null,
      el('div', { class: 'check-note' }, 'From OpenStreetMap land use and the NATS restriction zones, at 20 m. Always check on the ground.')]
      : [el('div', { class: 'muted' }, 'Outside the area this region covers.')]),
  ].filter(Boolean));
  card.hidden = false;
  bandoOpen = false;
  if (!checkPopup) checkPopup = new maplibregl.Marker({ element: el('div', { class: 'check-dot' }) });
  checkPopup.setLngLat(lngLat).addTo(map);
}

function closeCheck() {
  $('#check-card').hidden = true;
  checkPopup?.remove();
  bandoOpen = false;
}

// ---------------------------------------------------------------- bandos

const BANDO_COLOUR = '#c084fc';
const hasBandos = () => !!state.meta.sources?.bandos;
const bandosWanted = () => hasBandos() && (state.bandos || state.bandoList || state.more.bando != null);
let bandosData = null, bandosLoad = null, bandoOpen = false;

function loadBandos() {
  bandosLoad ||= fetch(`${BASE}/bandos.geojson`)
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null)
    .then((d) => {
      d ||= { type: 'FeatureCollection', features: [] };
      d.features.forEach((f, i) => { f.properties.i = i; });
      bandosData = d;
      map?.getSource('bandos')?.setData(d);
    });
  return bandosLoad;
}

// Shown when ticked in Layers, or while the Bandos chip or the bando filter is on.
function syncBandos() {
  if (!map?.getLayer('bandos-dots')) return;
  const on = bandosWanted();
  if (on) loadBandos();
  for (const id of ['bandos-site', 'bandos-dots']) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
}

// Asked to see one (or some), so the layer stays on: it's ticked in Layers.
function bandosOnMap() {
  state.bandos = $('#lyr-bandos').checked = true;
  savePrefs();
  syncBandos();
  if (PHONE.matches && state.sheet === 'full') setSheet('half');
  return PHONE.matches ? { top: 260, bottom: sheetHeights().half, left: 30, right: 30 } : { top: 60, bottom: 60, left: 60, right: 60 };
}

function showBandoOnMap(b) {
  const padding = bandosOnMap();
  map.flyTo({ center: b.geometry.coordinates, zoom: Math.max(map.getZoom(), 15.5), essential: true, duration: 1200,
    padding: PHONE.matches ? padding : { top: 0, bottom: 0, left: 0, right: 0 } });
  showBando(b);
}

function showAllBandos(list) {
  const padding = bandosOnMap();
  const xs = list.map((f) => f.geometry.coordinates[0]), ys = list.map((f) => f.geometry.coordinates[1]);
  map.setPadding({ top: 0, bottom: 0, left: 0, right: 0 });  // a fly-to's padding sticks and would add to this
  map.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]], { padding: { ...padding, top: 70 }, maxZoom: 15.5, duration: 1200 });
}

// Searching names a bando too ("priory", "mine", "bunker"; "bandos" lists the lot), above the spots.
const bandoCount = () => {
  const c = state.meta.sources?.bandos;
  return c ? (c.standing || 0) + (c.unknown || 0) : null;
};
const bandoSub = (p) => [p.n ? p.k : null, p.b > 1 ? `${p.b} buildings` : null, BANDO_STATE[p.s](p.h),
  `${Math.round(p.a / 10) * 10} m² of roof`].filter(Boolean).join(' · ');

// The Bandos chip: the derelict buildings themselves, biggest first, instead of spots near them.
function renderBandoList() {
  renderFilters();
  if (!bandosData) {
    loadBandos().then(() => state.bandoList && renderList());
    $('#spot-list').replaceChildren();
    return;
  }
  const q = state.query.trim().toLowerCase();
  const list = bandosData.features
    .filter((f) => !q || /^bandos?$/.test(q) || `${f.properties.n || ''} ${f.properties.k}`.toLowerCase().includes(q))
    .sort((a, b) => (b.properties.s === 1) - (a.properties.s === 1) || b.properties.a - a.properties.a);
  $('#taste-note').hidden = true;
  $('#seeds-box').hidden = true;
  $('#bando-hits').replaceChildren(el('div', { class: 'section-label bando-label' }, 'Derelict buildings, still standing',
    list.length > 1 ? el('button', { class: 'linklike', onclick: () => showAllBandos(list) }, 'Show all on the map') : null));
  $('#spot-list').replaceChildren(...list.map((f) => {
    const p = f.properties;
    return el('li', { class: 'row-item bando-row', title: 'Show it on the map', onclick: () => showBandoOnMap(f) },
      thumbBox(p.p, el('i', { class: `bdot s${p.s}` })),
      el('div', {},
        el('div', { class: 'spot-name' }, p.n || p.k),
        el('div', { class: 'spot-sub' }, [state.me ? metres(haversine(state.me, f.geometry.coordinates)) : null, bandoSub(p)].filter(Boolean).join(' · '))));
  }));
  const empty = $('#list-empty');
  empty.hidden = list.length > 0;
  if (!list.length) empty.replaceChildren(`No bandos match “${state.query}”.`);
  $('#list-count').textContent = `${list.length} bandos`;
  renderFoot();
  state.order = [];
  state.visible = [];
  map?.getSource('spots')?.setData({ type: 'FeatureCollection', features: [] });
  syncBandos();
  drawPins();
  watchThumbs($('#list-scroll'), $('#list-scroll'));
}

function renderBandoHits() {
  const box = $('#bando-hits');
  const q = state.query.trim().toLowerCase();
  if (q.length < 3 || !hasBandos() || state.bandoList) return box.replaceChildren();
  if (!bandosData) {
    loadBandos().then(() => state.query.trim().toLowerCase() === q && renderBandoHits());
    return box.replaceChildren();
  }
  const all = /^bandos?$/.test(q);
  const hits = bandosData.features
    .filter((f) => all || `${f.properties.n || ''} ${f.properties.k}`.toLowerCase().includes(q))
    .sort((a, b) => (b.properties.s === 1) - (a.properties.s === 1) || !!b.properties.n - !!a.properties.n
      || (b.properties.h ?? 0) - (a.properties.h ?? 0));
  if (!hits.length) return box.replaceChildren();
  box.replaceChildren(
    el('div', { class: 'section-label bando-label' }, `Bandos · ${hits.length}`,
      hits.length > 1 ? el('button', { class: 'linklike', onclick: () => showAllBandos(hits) }, 'Show all on the map') : null),
    el('ul', { class: 'bando-hits' }, hits.slice(0, 5).map((f) => {
      const p = f.properties;
      return el('li', { class: 'bando-hit', title: 'Show it on the map', onclick: () => showBandoOnMap(f) },
        el('i', { class: `bdot s${p.s}` }),
        el('div', {},
          el('div', { class: 'spot-name' }, p.n || p.k),
          el('div', { class: 'spot-sub' }, bandoSub(p))));
    })));
}

// A close-up of the bando in the full-screen 3D view; closing it throws the view away.
let bando3d = null;
async function openBando3D(f, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Loading 3D…';
  try {
    const { Preview3D } = await import('./preview3d.js');
    bando3d?.dispose();
    const host = el('div', { hidden: true });
    document.body.append(host);
    const pv = (bando3d = new Preview3D(host, {
      spot: { title: f.properties.n || f.properties.k }, preview: { ...f.properties.p, center: f.geometry.coordinates },
      center: f.geometry.coordinates, profile: state.profile, radiusM: 0,
      onClose: () => setTimeout(() => {
        pv.dispose();
        host.remove();
        if (bando3d === pv) bando3d = null;
      }),
    }));
    await pv.load(`${BASE}/${f.properties.p.file}`);
    if (!pv.disposed) pv.toggleFull(true);
  } catch (e) {
    btn.after(el('div', { class: 'check-note' }, `3D view unavailable (${e.message || e}).`));
  }
  btn.disabled = false;
  btn.textContent = label;
}

function bandosNear(c, maxD, n) {
  return (bandosData?.features || [])
    .map((f) => ({ f, d: haversine(c, f.geometry.coordinates) }))
    .filter((x) => x.d <= maxD)
    .sort((a, b) => (b.f.properties.s === 1) - (a.f.properties.s === 1) || a.d - b.d)
    .slice(0, n);
}

// The closest bando to a tap, with some slack: the dots are small for a finger.
function bandoAt(point) {
  if (!bandosData || !bandosWanted()) return null;
  const pad = TOUCH.matches ? 14 : 7;
  const hits = map.queryRenderedFeatures([[point.x - pad, point.y - pad], [point.x + pad, point.y + pad]], { layers: ['bandos-dots', 'bandos-site'] });
  let best = null, bestD = Infinity;
  for (const h of hits) {
    const f = bandosData.features[h.properties.i];
    const q = f && map.project(f.geometry.coordinates);
    const d = q ? (q.x - point.x) ** 2 + (q.y - point.y) ** 2 : Infinity;
    if (d < bestD) [best, bestD] = [f, d];
  }
  return best;
}

const BANDO_STATUS = {
  1: (h) => `Still standing: LiDAR sees something about ${Math.round(h)} m tall here.`,
  0: () => 'Little left: LiDAR sees nothing much standing, so it may only be a site now.',
  '-1': () => 'Not checked: no LiDAR here, so it may or may not still be standing.',
};

function showBando(f) {
  const p = f.properties;
  const [lon, lat] = f.geometry.coordinates;
  let best = null, bestD = Infinity;
  for (const s of state.spots[state.profile].features) {
    const d = haversine(f.geometry.coordinates, s.geometry.coordinates);
    if (d < bestD) [best, bestD] = [s, d];
  }
  const who = state.meta.profiles[state.profile].short || state.meta.profiles[state.profile].label;
  const link = (href, text) => el('a', { href, target: '_blank', rel: 'noopener' }, text);
  const card = $('#check-card');
  card.replaceChildren(...[
    el('button', { class: 'x', 'aria-label': 'Close', onclick: closeCheck }, '×'),
    el('h4', {}, p.n || p.k),
    el('div', { class: 'bando-status' }, p.n ? el('b', {}, `${p.k}. `) : null, BANDO_STATUS[p.s](p.h)),
    best ? el('button', {
      class: 'bando-spot',
      onclick: () => {
        closeCheck();
        select(best.properties.id, false);
      },
    },
    el('span', { class: 'score', style: `--c:${scoreColour(best.properties.score)}` }, best.properties.score),
    el('span', {}, el('small', {}, `Nearest ${who} spot, ${metres(bestD)} away`), el('br'), best.properties.title)) : null,
    p.p ? el('button', { class: 'bando-3d', onclick: (e) => openBando3D(f, e.currentTarget) }, '⤢ See it in 3D (LiDAR close-up)') : null,
    el('div', { class: 'bando-links' },
      link(`https://www.google.com/maps/dir/?api=1&destination=${lat},${lon}`, 'Directions'),
      link(`https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat},${lon}`, 'Street View'),
      p.osm ? link(p.osm, 'OpenStreetMap') : null),
    el('div', { class: 'check-note' }, p.m
      ? 'A protected heritage site (a scheduled monument, listed or open to visitors): it has a keeper, so ask before flying and keep clear of people.'
      : 'Old buildings are usually private and can be unsafe: ask the owner first and keep out of anything unstable.'),
  ].filter(Boolean));
  card.hidden = false;
  bandoOpen = true;
  if (!checkPopup) checkPopup = new maplibregl.Marker({ element: el('div', { class: 'check-dot' }) });
  checkPopup.setLngLat(f.geometry.coordinates).addTo(map);
}

// ---------------------------------------------------------------- UI wiring

function syncControls() {
  $$('#profile-switch button').forEach((b) => b.classList.toggle('on', b.dataset.profile === state.profile));
  $$('#list-sort button').forEach((b) => b.classList.toggle('on', b.dataset.sort === state.sort && !state.like));
  $$('#basemap-switch button').forEach((b) => b.classList.toggle('on', b.dataset.base === state.base));
}

function wireUI() {
  if (SANDBOXED) {
    $('#list-sort button[data-sort="near"]')?.remove();
    $('#basemap-title')?.remove();
    $('#basemap-switch')?.remove();
  }
  $$('#list-sort button').forEach((b) => b.addEventListener('click', () => {
    state.sort = b.dataset.sort;
    state.like = null;
    savePrefs();
    syncControls();
    renderList();
    $('#list-scroll').scrollTop = 0;
  }));
  $$('#basemap-switch button').forEach((b) => b.addEventListener('click', () => {
    state.base = b.dataset.base;
    savePrefs();
    syncControls();
    applyLayerToggles();
  }));
  $('#filters').addEventListener('wheel', (e) => {
    const box = e.currentTarget;
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX) || box.scrollWidth <= box.clientWidth) return;
    box.scrollLeft += e.deltaY;
    e.preventDefault();
  }, { passive: false });
  $('#filters').addEventListener('click', (e) => {
    const f = e.target.closest('.fchip')?.dataset.filter;
    if (!f) return;
    if (f === 'more') return toggleMore(!state.moreOpen);
    if (f === 'a3') {
      state.a3only = !state.a3only;
      savePrefs();
    } else if (f === 'saved') state.saved = !state.saved;
    else if (f === 'bandos') state.bandoList = !state.bandoList;
    else if (f.startsWith('kind:')) state.kind = state.kind === f.slice(5) ? null : f.slice(5);
    renderList();
    $('#list-scroll').scrollTop = 0;
  });
  $('#more-filters').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.more) {
      if (b.dataset.v === '') delete state.more[b.dataset.more];
      else state.more[b.dataset.more] = Number(b.dataset.v);
    } else if (b.hasAttribute('data-more-clear')) state.more = {};
    else if (b.hasAttribute('data-more-done')) return toggleMore(false);
    renderList();
    $('#list-scroll').scrollTop = 0;
  });
  const search = $('#search');
  search.addEventListener('input', () => {
    state.query = search.value;
    $('#search-clear').hidden = !search.value;
    renderList();
  });
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') search.blur();
  });
  search.addEventListener('focus', () => {
    if (PHONE.matches && state.sheet !== 'full') setSheet('full');
  });
  $('#search-clear').addEventListener('click', () => {
    search.value = '';
    state.query = '';
    $('#search-clear').hidden = true;
    renderList();
  });
  $('#spot-list').addEventListener('click', (e) => {
    const li = e.target.closest('.row-item');
    if (li?.dataset.id) select(li.dataset.id, true);
  });
  // hovering a row lights up its pin on the map
  $('#spot-list').addEventListener('mouseover', (e) => {
    const id = e.target.closest('.row-item')?.dataset.id;
    if (id === state.hover) return;
    state.hover = id;
    state.pins.forEach((m) => m.getElement().classList.toggle('hover', m.getElement().dataset.id === id));
  });
  $('#spot-list').addEventListener('mouseleave', () => {
    state.hover = null;
    state.pins.forEach((m) => m.getElement().classList.remove('hover'));
  });
  const pop = $('#layers-pop');
  $('#btn-layers').addEventListener('click', (e) => {
    e.stopPropagation();
    pop.hidden = !pop.hidden;
    $('#btn-layers').setAttribute('aria-expanded', String(!pop.hidden));
  });
  document.addEventListener('click', (e) => {
    if (!pop.hidden && !pop.contains(e.target)) {
      pop.hidden = true;
      $('#btn-layers').setAttribute('aria-expanded', 'false');
    }
  });
  $('#lyr-bandos').checked = !!state.bandos;
  $('#lyr-bandos').addEventListener('change', (e) => {
    state.bandos = e.target.checked;
    savePrefs();
  });
  $$('#layers-pop input').forEach((i) => i.addEventListener('change', applyLayerToggles));
  $('#btn-about').addEventListener('click', openAbout);
  $('#btn-check').addEventListener('click', () => setChecking(!state.checking));
  $('#check-done').addEventListener('click', () => setChecking(false));
  window.addEventListener('keydown', (e) => {
    if (document.querySelector('.p3d-wrap.full') || $('#about').open) return;
    if (e.key === 'Escape') {
      if (e.target.matches?.('textarea, input')) return e.target.blur();
      if (state.checking) setChecking(false);
      else if (!$('#check-card').hidden) closeCheck();
      else if (!$('#view-spot').hidden) goBack();
      return;
    }
    // / jumps to the search box from anywhere but a text field
    if (e.key === '/' && !e.target.closest?.('input, textarea, select') && !$('#view-list').hidden) {
      e.preventDefault();
      $('#search').focus();
      return;
    }
    // ← / → (or k / j) step through the spots while one is open, unless typing or steering the map
    const dir = { ArrowLeft: -1, ArrowRight: 1, k: -1, j: 1 }[e.key];
    if (!dir || $('#view-spot').hidden || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target.closest?.('input, textarea, select, .maplibregl-canvas-container')) return;
    e.preventDefault();
    $(`#spot-head .step${dir < 0 ? '.prev' : ':not(.prev)'}`)?.click();
  });
}

function openAbout() {
  $('#about-body').innerHTML = aboutHtml();
  $('#about-body [data-export]')?.addEventListener('click', exportRatings);
  $('#about-body [data-import]')?.addEventListener('change', importRatings);
  $('#about').showModal();
}

function exportRatings(e) {
  e.preventDefault();
  if (SANDBOXED) {
    const link = e.currentTarget;
    const json = ratings.export();
    const showText = () => { // no clipboard here: put the text where it can be selected and copied by hand
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
  <p>No satellite photos and no AI image guessing. Every 50 m square of the region is measured from <b>1 m height data</b>
  and map data, then scored on what FPV pilots look for:</p>
  <ul>
    <li><b>Gaps</b>: openings 2–12 m wide between trees or buildings at least 5 m tall (whoop gaps up to 6 m).</li>
    <li><b>Lone trees</b>: single trees with open ground all round, for orbits and power loops.</li>
    <li><b>Viaducts and bridges</b> (arches to fly through), <b>beaches</b>, <b>public land</b> to fly from, and <b>edges, open space, tall stuff, slopes, water and bandos</b> nearby; for 5" cine: a subject to film (a lighthouse, castle, pier, viaduct or the coast) with big water, hills and a sunrise or sunset round it; for the whoop: architecture, plazas, landmarks and under-canopy space. A spot's pin sits on the viaduct, landmark or beach it's about, and a run can weave through a viaduct's arches where trees don't crowd it.</li>
    <li><b>Your taste</b>: each square is compared with your favourite spots (the ★ ones), and with your 👍 / 👎 ratings in “My taste”.</li>
    <li><b>Parks</b>: every public park or common of 4 ha or more gets a spot at its best place, even if it doesn't make the top 250. Tap “Parks” above the list to see them.</li>
    <li><b>Bandos</b>: derelict, abandoned and disused buildings mapped on OpenStreetMap, still standing at least 4 m tall in the LiDAR. Ruins in parks, monuments, old pit sites and capped shafts aren't bandos, so they're left out. Tap the “Bandos” chip to list them biggest first, tap one for its card and a 3D close-up, or use Filters › Bando within for spots near one.</li>
  </ul>
  <h3>Places worth a look, not permission</h3>
  <p>Spots are suggestions to go and check. Each one says how it sits with the rules: the 800 g 5" flies Open <b>A3</b>
  (150 m from houses, businesses, schools and parks; 50 m from buildings and busy roads), so parks show up but are marked
  <b>sub-250 g only</b> unless they're A3-clear; the 85 mm flies <b>A1</b>. Airport and prison flight restriction zones and
  MOD land are left out. Land-owner rules (National Trust, Forestry England, Woodland Trust, nature reserves) show as warnings.
  Use <b>Can I fly here?</b>, right-click or long-press the map to check any point.</p>
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
  NATS UAS restriction zones, Open-Meteo. Base map © OpenFreeMap, OpenMapTiles. Built ${esc(state.meta.generated_at || '')}.</p>`;
}

boot();

if ('serviceWorker' in navigator && import.meta.env.PROD && !SANDBOXED) {
  // A redeploy with a new worker takes over this page straight away; if that happens while the page
  // is still starting up, reload once so the new code and data load together.
  const hadWorker = !!navigator.serviceWorker.controller, t0 = performance.now();
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!hadWorker || performance.now() - t0 > 15000) return;
    try {
      if (sessionStorage.getItem('sw-reloaded')) return;  // once per tab, never a loop
      sessionStorage.setItem('sw-reloaded', '1');
    } catch {
      return;
    }
    location.reload();
  });
  // Once it's running, have it fetch this region's map data so the app works offline in the field.
  navigator.serviceWorker.register('./sw.js')
    .then(() => navigator.serviceWorker.ready)
    .then((reg) => reg.active?.postMessage({
      precache: `${BASE}/offline.json`,
      // this first visit loaded the app before the worker was running, so hand it what was loaded
      urls: performance.getEntriesByType('resource').map((r) => r.name).filter((u) => u.includes('/assets/')),
    }))
    .catch(() => {});
}
