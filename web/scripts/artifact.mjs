// Turns `vite build --outDir dist-artifact` (with VITE_TARGET=artifact) into a page for a sandboxed
// host that wraps it in its own <html>/<head>/<body>: page.html holds the title, the asset tags and
// the app markup, and the files the host can't use (service worker, install icons, GPX) are dropped.
import { readFileSync, writeFileSync, rmSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

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
for (const region of readdirSync(join(dir, 'data'))) {
  if (!statSync(join(dir, 'data', region)).isDirectory()) continue;
  for (const f of readdirSync(join(dir, 'data', region))) {
    if (f.endsWith('.gpx')) rmSync(join(dir, 'data', region, f));
  }
}

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
