/* List shell and index.json are network-first. Versioned episode/media files are cache-first. */
const SHELL = "bp-shell-v1";
const INDEX = "bp-index-v1";
const MEDIA = "bp-media-v1";

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

async function deleteSiblingVersions(cache, request) {
  const target = new URL(request.url);
  const keys = await cache.keys();
  await Promise.all(keys.map((req) => {
    const u = new URL(req.url);
    if (u.pathname === target.pathname && u.href !== target.href) {
      return cache.delete(req);
    }
    return Promise.resolve(false);
  }));
}

async function networkFirst(request, cacheName, cleanSiblings = false) {
  const cache = await caches.open(cacheName);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) {
      await cache.put(request, fresh.clone());
      if (cleanSiblings) await deleteSiblingVersions(cache, request);
    }
    return fresh;
  } catch (err) {
    const hit = await cache.match(request);
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(request, cacheName, cleanSiblings = false) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) {
    if (cleanSiblings) await deleteSiblingVersions(cache, request);
    return hit;
  }
  const fresh = await fetch(request);
  if (fresh && fresh.ok) {
    await cache.put(request, fresh.clone());
    if (cleanSiblings) await deleteSiblingVersions(cache, request);
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
    if (url.searchParams.has("v")) {
      event.respondWith(cacheFirst(req, MEDIA, true));
    } else {
      // Old bookmarks / legacy unversioned links should not stay stuck on stale cached HTML.
      event.respondWith(networkFirst(req, MEDIA, true));
    }
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
