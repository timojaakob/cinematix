// cinematix service worker: makes opening the app paint instantly.
//
// Page loads of / and /movies/<id> are answered with a cached loading shell
// (/shell, see shell.html) straight from the phone, with no network wait;
// the shell then fetches the real page and swaps it in. Versioned static
// files (?v=<mtime>) are served cache-first. Everything else - API calls,
// form posts, ?direct=1 loads - goes to the network untouched, so the app
// never shows stale showtimes from a cache.

const SHELL_URL = '/shell';
const SHELL_CACHE = 'cx-shell';
const STATIC_CACHE = 'cx-static';
const SHELL_REFRESH_MS = 10 * 60 * 1000;
let lastShellRefresh = 0;

self.addEventListener('install', (event) => {
  event.waitUntil(refreshShell().catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// Caches the shell plus every /static/ file it references (CSS, fonts,
// logo), so it can paint with no network at all. Old versions of those
// files are dropped once the shell stops referencing them.
async function refreshShell() {
  lastShellRefresh = Date.now();
  const resp = await fetch(SHELL_URL, { credentials: 'same-origin', redirect: 'manual', cache: 'no-store' });
  if (!resp.ok || !(resp.headers.get('content-type') || '').includes('text/html')) return;
  const html = await resp.clone().text();
  const assets = new Set(
    [...html.matchAll(/(?:href|src)="(\/static\/[^"]+)"|url\('(\/static\/[^']+)'\)/g)]
      .map((m) => new URL((m[1] || m[2]).replace(/&amp;/g, '&'), self.location.origin).href),
  );
  const staticCache = await caches.open(STATIC_CACHE);
  await Promise.all([...assets].map(async (url) => {
    if (await staticCache.match(url)) return;
    const r = await fetch(url);
    if (r.ok) await staticCache.put(url, r);
  }));
  const wanted = new Set([...assets].map((u) => new URL(u).pathname));
  for (const req of await staticCache.keys()) {
    if (wanted.has(new URL(req.url).pathname) && !assets.has(req.url)) await staticCache.delete(req);
  }
  await (await caches.open(SHELL_CACHE)).put(SHELL_URL, resp);
}

function isAppPage(url) {
  return url.pathname === '/' || /^\/movies\/[^/]+$/.test(url.pathname);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === 'navigate') {
    if (url.searchParams.has('direct') || !isAppPage(url)) return;
    event.respondWith((async () => {
      const cached = await caches.match(SHELL_URL, { cacheName: SHELL_CACHE });
      if (Date.now() - lastShellRefresh > SHELL_REFRESH_MS) {
        event.waitUntil(refreshShell().catch(() => {}));
      }
      return cached || fetch(req);
    })());
    return;
  }

  if (url.pathname.startsWith('/static/') && url.searchParams.has('v')) {
    event.respondWith((async () => {
      const cache = await caches.open(STATIC_CACHE);
      const hit = await cache.match(req);
      if (hit) return hit;
      const resp = await fetch(req);
      if (resp.ok) cache.put(req, resp.clone());
      return resp;
    })());
  }
});
