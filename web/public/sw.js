// Offline support: once you've opened the app it keeps working with no signal. The page sends the
// region's file list (data/<region>/offline.json) and the worker fetches the map, spots and basemap
// tiles in the background, so the field works offline even where you never zoomed in at home.
// Hashed build assets never change, so they're served cache-first; everything else is served from
// the cache and refreshed in the background.
const CACHE = 'fpv-spots-v2';

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['./', './index.html'])).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('message', (e) => {
  const list = e.data?.precache;
  if (typeof list !== 'string') return;
  const urls = (e.data.urls || []).filter((u) => new URL(u).origin === self.location.origin);
  e.waitUntil(Promise.all([
    caches.open(CACHE).then((c) => Promise.all(urls.map((u) => c.match(u).then((hit) => hit || c.add(u).catch(() => {}))))),
    precache(new URL(list, self.location.href)),
  ]));
});

async function precache(listUrl) {
  const cache = await caches.open(CACHE);
  const res = await fetch(listUrl).catch(() => null);
  if (!res?.ok) return;
  cache.put(listUrl, res.clone());
  const { files = [], generated_at: version } = await res.json();
  const urls = files.map((f) => new URL(f, listUrl).href);
  const stamp = `${listUrl.href.split('?')[0]}?stamp`;  // refetch everything once per pipeline run
  const fresh = (await cache.match(stamp).then((r) => r?.text()).catch(() => null)) === version;
  for (let i = 0; i < urls.length; i += 6) {
    await Promise.all(urls.slice(i, i + 6).map(async (u) => {
      if (fresh && await cache.match(u)) return;
      const r = await fetch(u).catch(() => null);
      if (r?.ok) await cache.put(u, r);
    }));
  }
  await cache.put(stamp, new Response(version || ''));
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;
  const immutable = /\/assets\/[^/]+-[\w-]{8,}\.\w+$/.test(url.pathname);
  e.respondWith(caches.open(CACHE).then(async (cache) => {
    const hit = await cache.match(req, { ignoreSearch: req.mode === 'navigate' });
    if (hit && immutable) return hit;
    const fresh = fetch(req).then((res) => {
      if (res.ok) cache.put(req, res.clone());
      return res;
    }).catch(() => hit || Response.error());
    return hit || fresh;
  }));
});
