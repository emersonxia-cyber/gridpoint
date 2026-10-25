/* Gridpoint service worker.
   Keeps a copy of the app itself on the device, so it opens with no connection (and from the
   Home Screen). Weather data is never cached here: requests to weather.gov, the Iowa Mesonet,
   Open-Meteo and the radar go straight to the network, and the app saves each place's last
   forecast itself. */
const CACHE = 'gridpoint-app-v2';
const PAGE = new URL('./', self.registration.scope).href;
const BASE = new URL(PAGE).pathname;
/* addresses that open the app: the app itself, and its Home Screen guide */
const APP_PATHS = new Set([BASE, BASE + 'index.html', BASE + 'install', BASE + 'install.html']);
const FILES = [PAGE, 'manifest.json', 'icon-192.png', 'icon-512.png'].map(f => new URL(f, self.registration.scope).href);

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(FILES.map(url => fetch(url, { cache: 'no-store' })
      .then(res => { if (res.ok && !res.redirected) return cache.put(url, res); })
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
   background, so an updated app shows up the next time it's opened. Language files are the
   exception: the network first (an updated app asks for them once, and needs the new words),
   the saved copy when there's no connection. Other pages on the site (README, check.py)
   aren't part of the app and load normally. */
self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const isPage = APP_PATHS.has(url.pathname);
  if (req.mode === 'navigate' && !isPage) return;
  const key = isPage ? PAGE : url.origin + url.pathname;
  const netFirst = url.pathname.startsWith(BASE + 'i18n/');
  const cacheP = caches.open(CACHE);
  const fresh = fetch(isPage ? PAGE : req, { cache: 'no-store' }).then(res => {
    if (res && res.ok && !res.redirected) {
      const copy = res.clone();
      cacheP.then(cache => cache.put(key, copy)).catch(() => {});
    }
    return res;
  });
  event.waitUntil(fresh.then(() => {}, () => {}));
  event.respondWith((async () => {
    const saved = await (await cacheP).match(key);
    if (saved && !netFirst) return saved;
    try {
      const res = await fresh;
      return !res.ok && saved ? saved : res;
    } catch (e) {
      if (saved) return saved;
      return new Response('Gridpoint needs a connection the first time it opens on a device. Connect once, and after that it opens offline too.', { status: 503, headers: { 'content-type': 'text/plain; charset=utf-8' } });
    }
  })());
});
