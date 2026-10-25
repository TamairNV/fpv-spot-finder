// Offline support: once you've opened the app it keeps working with no signal. The page sends the
// region's file list (data/<region>/offline.json) and the worker fetches the map, spots and basemap
// tiles in the background, so the field works offline even where you never zoomed in at home.
// Hashed build assets never change, so they're served cache-first. The page itself and the spot
// lists come from the network when there is one (old code must never meet new data after a
// redeploy: a re-scored list names different 3D previews), falling back to the cache offline or
// when the network is slow. Everything else is served from the cache and refreshed in the background.
const CACHE = 'fpv-spots-v2';
const SLOW_MS = 4000;

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
  // the server's API is always live, except flight photos, which never change and should work offline
  if (url.pathname.includes('/api/') && !url.pathname.includes('/api/photos/')) return;
  const immutable = /\/assets\/[^/]+-[\w-]{8,}\.\w+$/.test(url.pathname);
  if (req.mode === 'navigate' || /\.(geo)?json$/.test(url.pathname)) {
    e.respondWith(caches.open(CACHE).then((cache) => networkFirst(cache, req)));
    return;
  }
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

async function networkFirst(cache, req) {
  const net = fetch(req).then((res) => {
    if (res.ok) cache.put(req, res.clone());
    return res;
  });
  net.catch(() => {});
  const res = await Promise.race([net, new Promise((r) => setTimeout(r, SLOW_MS))]).catch(() => null);
  if (res) return res;
  const hit = await cache.match(req, { ignoreSearch: req.mode === 'navigate' });
  return hit || net.catch(() => Response.error());
}

// "Good flying tomorrow" from the server (see server/spotter_api/main.py).
self.addEventListener('push', (e) => {
  let msg = {};
  try {
    msg = e.data?.json() || {};
  } catch { /* plain text */ }
  e.waitUntil(self.registration.showNotification(msg.title || 'FPV Spot Finder', {
    body: msg.body || '', icon: './icon-192.png', badge: './icon-192.png', tag: 'forecast', data: { url: msg.url || './' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || './', self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    const win = wins.find((w) => w.url.startsWith(self.registration.scope));
    if (win) return win.navigate(url).then((w) => (w || win).focus());
    return self.clients.openWindow(url);
  }));
});
