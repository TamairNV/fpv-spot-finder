// The whole country on one map. The pipeline still works region by region (40 km boxes), so the app
// keeps one region "active" (its list, ratings, heatmap and legal overlay) and follows the map from box
// to box, while every other region's spots show as dots: the best of each from national_<drone>.json
// when zoomed out, and the full set of the boxes in view once zoomed in.

// Regions overlap (the hand-made ones overlap the grid and each other), so each point belongs to one:
// the region whose centre is nearest, of those whose box holds it. spotfinder/export.owner_of does the same.
export function ownerOf(regions, lon, lat) {
  let best = null, bestD = Infinity;
  const k = Math.cos((lat * Math.PI) / 180);
  regions.forEach((r, i) => {
    const [w, s, e, n] = r.bbox;
    if (lon < w || lon > e || lat < s || lat > n) return;
    const d = ((lon - r.center[0]) * k) ** 2 + (lat - r.center[1]) ** 2;
    if (d < bestD) {
      best = i;
      bestD = d;
    }
  });
  return best;
}

const EMPTY = { type: 'FeatureCollection', features: [] };
const MAX_FULL = 9;  // regions fetched in full at once when zoomed in (a 3 x 3 block)
export const FULL_ZOOM = 10;
const KEEP_FULL = 40;  // regions' dots kept in memory while you roam about

export class National {
  constructor(dataUrl, regions) {
    this.data = dataUrl;
    this.regions = regions;
    this.index = new Map(regions.map((r, i) => [r.key, i]));
    this.overview = {};  // drone -> [{lon, lat, score, region, id, title}]
    this.full = new Map();  // "<region>/<drone>" -> promise of that region's own spots
  }

  regionAt(lon, lat) {
    const i = ownerOf(this.regions, lon, lat);
    return i == null ? null : this.regions[i];
  }

  loadOverview(profile) {
    this.overview[profile] ||= fetch(`${this.data}/national_${profile}.json`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .then((d) => (d?.spots || []).map(([lon, lat, score, ri, id, title]) => ({ lon, lat, score, region: d.regions[ri], id, title })));
    return this.overview[profile];
  }

  // Every region's bandos (national_bandos.json), for the map at any zoom: there are only a few per region.
  loadBandos() {
    this.bandos ||= fetch(`${this.data}/national_bandos.json`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .then((d) => (d?.bandos || []).map(([lon, lat, ri, s, h, a, n, k, id], rank) => ({ lon, lat, region: d.regions[ri], s, h, a, n, k, id, rank })));
    return this.bandos;
  }

  // A region's own spots as dots: dots_<drone>.json (just what a dot needs, already only the spots
  // the region owns), or, for data made before that file existed, its full spots file, filtered here.
  loadFull(key, profile) {
    const k = `${key}/${profile}`;
    if (this.full.has(k)) {
      const p = this.full.get(k);
      this.full.delete(k);  // most recently used last, so the oldest go first
      this.full.set(k, p);
      return p;
    }
    const i = this.index.get(key);
    const slim = () => fetch(`${this.data}/${key}/dots_${profile}.json`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((rows) => rows.map(([lon, lat, score, id, title]) => ({ lon, lat, score, region: key, id, title })));
    const full = () => fetch(`${this.data}/${key}/spots_${profile}.geojson`)
      .then((r) => (r.ok ? r.json() : EMPTY))
      .then((d) => d.features
        .filter((f) => ownerOf(this.regions, ...f.geometry.coordinates) === i)
        .map((f) => ({ lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1], score: f.properties.score, region: key, id: f.properties.id, title: f.properties.title })));
    const p = slim().catch(full).catch(() => []);
    this.full.set(k, p);
    while (this.full.size > KEEP_FULL) this.full.delete(this.full.keys().next().value);
    return p;
  }

  // Which regions others() would fill in full for this view: when that and the zoom band are the same
  // as last time, the dots haven't changed and the map needn't be given them again.
  viewKey(active, profile, bounds, zoom) {
    return zoom < FULL_ZOOM ? `${active}|${profile}|over` : `${active}|${profile}|${this.inView(active, bounds).map((r) => r.key).sort().join(',')}`;
  }

  inView(active, [w, s, e, n]) {
    return this.regions
      .filter((r) => r.key !== active && r.bbox[0] < e && r.bbox[2] > w && r.bbox[1] < n && r.bbox[3] > s)
      .sort((a, b) => dist2(a.center, [w, s, e, n]) - dist2(b.center, [w, s, e, n]))
      .slice(0, MAX_FULL);
  }

  // The dots for every region but the active one, for the map's view.
  async others(active, profile, bounds, zoom) {
    const over = (await this.loadOverview(profile)).filter((s) => s.region !== active);
    if (zoom < FULL_ZOOM) return over;
    const inView = this.inView(active, bounds);
    const full = new Set(inView.map((r) => r.key));
    const lists = await Promise.all(inView.map((r) => this.loadFull(r.key, profile)));
    return [...over.filter((x) => !full.has(x.region)), ...lists.flat()];
  }
}

function dist2([lon, lat], [w, s, e, n]) {
  return (lon - (w + e) / 2) ** 2 + (lat - (s + n) / 2) ** 2;
}

export const bandosGeojson = (rows) => ({
  type: 'FeatureCollection',
  features: rows.map((b) => ({
    type: 'Feature', geometry: { type: 'Point', coordinates: [b.lon, b.lat] },
    properties: { id: b.id, region: b.region, s: b.s, h: b.h ?? 0, a: b.a, n: b.n, k: b.k, rank: b.rank },
  })),
});

export const asGeojson = (rows) => ({
  type: 'FeatureCollection',
  features: rows.map((r) => ({
    type: 'Feature', geometry: { type: 'Point', coordinates: [r.lon, r.lat] },
    properties: { id: r.id, score: r.score, title: r.title, region: r.region },
  })),
});
