/* Gridpoint service worker.
   Keeps a copy of the app itself on the device, so the home-screen app still opens
   when the computer serving it is asleep or out of reach. Weather data is never cached
   here: requests to weather.gov, Open-Meteo and the radar go straight to the network. */
const CACHE = 'gridpoint-app-v1';
const PAGE = new URL('./', self.registration.scope).href;
const FILES = [PAGE, 'manifest.json', 'icon-192.png', 'icon-512.png'].map(f => new URL(f, self.registration.scope).href);

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(FILES.map(url => fetch(url, { cache: 'no-store' })
      .then(res => { if (res.ok) return cache.put(url, res); })
      .catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

/* Same-origin files: answer from the saved copy right away, then refresh that copy in the
   background, so an updated index.html shows up the next time the app is opened. */
self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const isPage = req.mode === 'navigate' || url.href === PAGE || url.pathname.endsWith('/index.html');
  const key = isPage ? PAGE : url.origin + url.pathname;
  const cacheP = caches.open(CACHE);
  const fresh = fetch(isPage ? PAGE : req, { cache: 'no-store' }).then(res => {
    if (res && res.ok) {
      const copy = res.clone();
      cacheP.then(cache => cache.put(key, copy)).catch(() => {});
    }
    return res;
  });
  event.waitUntil(fresh.then(() => {}, () => {}));
  event.respondWith((async () => {
    const saved = await (await cacheP).match(key);
    if (saved) return saved;
    try { return await fresh; }
    catch (e) {
      return new Response('Gridpoint can’t reach the computer it’s served from, and this device doesn’t have a saved copy yet. Open it once while connected.', { status: 503, headers: { 'content-type': 'text/plain; charset=utf-8' } });
    }
  })());
});
