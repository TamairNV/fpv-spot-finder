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

  loadFull(key, profile) {
    const k = `${key}/${profile}`;
    if (!this.full.has(k)) {
      const i = this.index.get(key);
      this.full.set(k, fetch(`${this.data}/${key}/spots_${profile}.geojson`)
        .then((r) => (r.ok ? r.json() : EMPTY))
        .catch(() => EMPTY)
        .then((d) => d.features
          .filter((f) => ownerOf(this.regions, ...f.geometry.coordinates) === i)
          .map((f) => ({ lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1], score: f.properties.score, region: key, id: f.properties.id, title: f.properties.title }))));
    }
    return this.full.get(k);
  }

  // The dots for every region but the active one, for the map's view.
  async others(active, profile, bounds, zoom) {
    const over = (await this.loadOverview(profile)).filter((s) => s.region !== active);
    if (zoom < FULL_ZOOM) return over;
    const [w, s, e, n] = bounds;
    const inView = this.regions
      .filter((r) => r.key !== active && r.bbox[0] < e && r.bbox[2] > w && r.bbox[1] < n && r.bbox[3] > s)
      .sort((a, b) => dist2(a.center, bounds) - dist2(b.center, bounds))
      .slice(0, MAX_FULL);
    const full = new Set(inView.map((r) => r.key));
    const lists = await Promise.all(inView.map((r) => this.loadFull(r.key, profile)));
    return [...over.filter((x) => !full.has(x.region)), ...lists.flat()];
  }
}

function dist2([lon, lat], [w, s, e, n]) {
  return (lon - (w + e) / 2) ** 2 + (lat - (s + n) / 2) ** 2;
}

export const asGeojson = (rows) => ({
  type: 'FeatureCollection',
  features: rows.map((r) => ({
    type: 'Feature', geometry: { type: 'Point', coordinates: [r.lon, r.lat] },
    properties: { id: r.id, score: r.score, title: r.title, region: r.region },
  })),
});
