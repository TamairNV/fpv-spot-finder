// Turns `vite build --outDir dist-artifact` (with VITE_TARGET=artifact) into a page for a sandboxed
// host that wraps it in its own <html>/<head>/<body>: page.html holds the title, the asset tags and
// the app markup, and the files the host can't use (service worker and its offline list, install icons, GPX) are dropped.
// The host takes about 500 files a page, so only the region the app opens on goes in (ARTIFACT_REGION, newcastle
// by default), with the 3D previews of its top ARTIFACT_PREVIEWS spots per drone, the seeds and the bandos.
import { readFileSync, writeFileSync, rmSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const REGION = process.env.ARTIFACT_REGION || 'newcastle';
const TOP = Number(process.env.ARTIFACT_PREVIEWS || 150);
const dir = new URL('../dist-artifact/', import.meta.url).pathname;
const html = readFileSync(join(dir, 'index.html'), 'utf8');

const title = html.match(/<title>[\s\S]*?<\/title>/)[0];
const assets = [...html.matchAll(/<(script|link)\b[^>]*\b(src|href)="\.\/assets\/[^"]+"[^>]*>(<\/script>)?/g)]
  .map((m) => m[0].replace(' crossorigin', ''));
const body = html.match(/<body>([\s\S]*)<\/body>/)[1]
  .replace(/<script[^>]*src="\/src\/[^"]*"[^>]*><\/script>/, '')
  .trim();

writeFileSync(join(dir, 'page.html'), [title, ...assets, body].join('\n') + '\n');
rmSync(join(dir, 'index.html'));

for (const f of ['sw.js', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png']) rmSync(join(dir, f), { force: true });
for (const f of readdirSync(join(dir, 'data'))) if (f !== REGION) rmSync(join(dir, 'data', f), { recursive: true });
const rd = join(dir, 'data', REGION);
for (const f of readdirSync(rd)) {
  if (f.endsWith('.gpx') || f === 'offline.json') rmSync(join(rd, f));
}

// A spot past the cut shows no thumbnail or 3D view here, rather than a broken one.
const json = (f) => JSON.parse(readFileSync(join(rd, f), 'utf8'));
const lists = readdirSync(rd).filter((f) => /^spots_.+\.geojson$/.test(f)).map((f) => [f, json(f)]);
const keep = new Set([
  ...json('meta.json').seeds.map((s) => s.preview?.file),
  ...json('bandos.geojson').features.map((b) => b.properties.p?.file),
  ...lists.flatMap(([, g]) => g.features.filter((s) => s.properties.rank <= TOP).map((s) => s.properties.preview?.file)),
]);
for (const [f, g] of lists) {
  for (const s of g.features) if (s.properties.preview && !keep.has(s.properties.preview.file)) s.properties.preview = null;
  writeFileSync(join(rd, f), JSON.stringify(g));
}
for (const f of readdirSync(join(rd, 'previews'))) if (!keep.has(`previews/${f}`)) rmSync(join(rd, 'previews', f));

// Nor does it serve .pbf: the basemap tiles go in one file, base64 by z/x/y (main.js liteTiles).
const tiles = {};
const pack = (d, rel) => {
  for (const f of readdirSync(d)) {
    const p = join(d, f), r = rel ? `${rel}/${f}` : f;
    if (statSync(p).isDirectory()) pack(p, r);
    else tiles[r.replace(/\.pbf$/, '')] = readFileSync(p).toString('base64');
  }
};
pack(join(rd, 'tiles'), '');
writeFileSync(join(rd, 'tiles.json'), JSON.stringify(tiles));
rmSync(join(rd, 'tiles'), { recursive: true });

const files = [];
const walk = (d, rel = '') => {
  for (const f of readdirSync(d)) {
    const p = join(d, f), r = rel ? `${rel}/${f}` : f;
    if (statSync(p).isDirectory()) walk(p, r);
    else if (r !== 'page.html') files.push(r);
  }
};
walk(dir);
writeFileSync(join(dir, 'files.json'), JSON.stringify(files.sort(), null, 1));
console.log(`page.html + ${files.length} files in dist-artifact/ (list in files.json)`);
