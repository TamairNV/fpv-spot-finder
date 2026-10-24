// Load-time benchmark: serves a built folder over a throttled connection and times the first useful
// paint (spot list, map idle with overlays). Usage: node scripts/bench.mjs [dist-dir] [runs]
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT || 'playwright');
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { join, extname, resolve } from 'node:path';
import { gzipSync } from 'node:zlib';

const dir = resolve(process.argv[2] || 'dist');
const runs = +(process.argv[3] || 3);
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.geojson': 'application/json', '.png': 'image/png', '.pbf': 'application/x-protobuf', '.bin': 'application/octet-stream' };
let bytes = 0;
const server = createServer(async (req, res) => {
  const p = join(dir, decodeURIComponent(new URL(req.url, 'http://x').pathname));
  try {
    const f = (await stat(p)).isDirectory() ? join(p, 'index.html') : p;
    let body = await readFile(f);
    const t = types[extname(f)] || 'application/octet-stream';
    const headers = { 'content-type': t };
    if (!t.startsWith('image/') && body.length > 1024) { body = gzipSync(body); headers['content-encoding'] = 'gzip'; }
    bytes += body.length;
    res.writeHead(200, headers).end(body);
  } catch { res.writeHead(404).end(); }
}).listen(0);
const url = `http://127.0.0.1:${server.address().port}/?bench`;

const browser = await chromium.launch({ args: ['--enable-unsafe-swiftshader', '--use-gl=swiftshader'] });
const results = [];
for (let i = 0; i < runs; i++) {
  bytes = 0;
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 150, downloadThroughput: 9e6 / 8, uploadThroughput: 1.5e6 / 8 });
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 });
  const t0 = Date.now();
  await page.goto(url);
  await page.waitForSelector('#spot-list li', { timeout: 120000 });
  const list = Date.now() - t0;
  await page.waitForFunction(() => window.__map?.loaded() && window.__map.getLayer('spots-dots') && window.__map.areTilesLoaded(), null, { timeout: 120000, polling: 50 });
  const idle = Date.now() - t0;
  const heap = await page.evaluate(() => performance.memory?.usedJSHeapSize / 1e6 | 0);
  results.push({ list, idle, kB: bytes / 1024 | 0, heapMB: heap });
  await ctx.close();
}
console.table(results);
const med = (k) => results.map((r) => r[k]).sort((a, b) => a - b)[results.length >> 1];
console.log(`median: list ${med('list')} ms, map ready ${med('idle')} ms, ${med('kB')} kB over the wire`);
await browser.close();
server.close();
