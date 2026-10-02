/* List shell and index.json are network-first. Episode files are cached after the first open. */
const SHELL = "bp-shell-v1";
const INDEX = "bp-index-v1";
const MEDIA = "bp-media-v1";

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) {
      cache.put(request, fresh.clone());
    }
    return fresh;
  } catch (err) {
    const hit = await cache.match(request);
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) return hit;
  const fresh = await fetch(request);
  if (fresh && fresh.ok) {
    cache.put(request, fresh.clone());
  }
  return fresh;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;
  if (path.endsWith("/index.json")) {
    event.respondWith(networkFirst(req, INDEX));
    return;
  }
  if (path.includes("/episodes/") || path.includes("/covers/")) {
    event.respondWith(cacheFirst(req, MEDIA));
    return;
  }
  if (
    req.mode === "navigate" ||
    path.endsWith("/index.html") ||
    path.endsWith("/sw.js") ||
    path.endsWith(".webmanifest") ||
    path.includes("/icons/")
  ) {
    event.respondWith(networkFirst(req, SHELL));
  }
});
