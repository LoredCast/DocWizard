// Service worker (ARCHITECTURE.md §6.5). The app serves this file at /sw.js with the
// release's manifest injected, so each release precaches its own assets and the two
// offline shells, and drops the caches of older releases.
const MANIFEST = self.__MANIFEST__;
const CACHE = `docwizard-${MANIFEST.release}`;
const [HOME_SHELL, EDITOR_SHELL] = MANIFEST.shells;

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // Fetched fresh, never from the HTTP cache, so a release can't precache stale files.
    await cache.addAll([...MANIFEST.assets, ...MANIFEST.shells].map((url) => new Request(url, { cache: "reload" })));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith("docwizard-") && name !== CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

// Pages are network-first; offline, "/" gets the home shell and documents get the
// editor shell, which opens the copy kept in IndexedDB.
function shellFor(url) {
  const parts = url.pathname.split("/").filter(Boolean);
  if (parts[0] === "p" && parts.length >= 3) return EDITOR_SHELL;
  if (url.pathname === "/" || parts[0] === "p") return HOME_SHELL;
  return null;
}

async function page(request) {
  const url = new URL(request.url);
  const shell = shellFor(url);
  try {
    const response = await fetch(request);
    // A proxy in front of a stopped app answers 502/503/504: treat that as offline too.
    if (shell && response.status >= 502 && response.status <= 504) return (await caches.match(shell)) ?? response;
    return response;
  } catch (err) {
    if (shell) {
      const cached = await caches.match(shell);
      if (cached) return cached;
    }
    throw err;
  }
}

async function asset(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(CACHE);
    cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith(`${MANIFEST.static}/`)) {
    event.respondWith(asset(request));
  } else if (request.mode === "navigate") {
    event.respondWith(page(request));
  }
});
