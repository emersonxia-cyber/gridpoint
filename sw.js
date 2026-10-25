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

/* the app's build stamp, and telling open pages a newer one is saved (they switch to it, or offer to) */
const buildOf = text => (/APP_BUILD = '([^']+)'/.exec(text || '') || [])[1] || null;
async function announce(build) {
  if (!build) return;
  for (const c of await self.clients.matchAll({ type: 'window', includeUncontrolled: true })) c.postMessage({ type: 'gp-updated', build });
}

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
    const page = await (await caches.open(CACHE)).match(PAGE);
    if (page) await announce(buildOf(await page.text()));
  })());
});

/* save a fresh copy; for the app itself, say so when it's a newer build than the saved one */
async function save(cacheP, key, res, isPage) {
  const cache = await cacheP;
  if (!isPage) return cache.put(key, res);
  const [old, text] = await Promise.all([cache.match(key).then(r => (r ? r.text() : null)), res.clone().text()]);
  await cache.put(key, res);
  const was = buildOf(old), now = buildOf(text);
  if (was && now && was !== now) await announce(now);
}

/* Same-origin files: answer from the saved copy right away, then refresh that copy in the
   background (and tell the open page when that copy is a newer app, so it can switch to it). Language files are the
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
  let saving = Promise.resolve();
  const fresh = fetch(isPage ? PAGE : req, { cache: 'no-store' }).then(res => {
    if (res && res.ok && !res.redirected) saving = save(cacheP, key, res.clone(), isPage);
    return res;
  });
  event.waitUntil(fresh.then(() => saving, () => {}).catch(() => {}));
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
