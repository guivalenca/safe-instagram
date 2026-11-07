// Serve the app shell from cache so it opens instantly, even on bad signal.
// Updates download in the background and apply on the next launch.
// Instagram data (/api) never goes through here.
const VERSION = "v3";
const SHELL = ["/", "/static/app.js", "/static/style.css", "/static/vendor/preact-htm.js", "/static/manifest.json", "/static/icon-180.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  const key = e.request.mode === "navigate" ? "/" : e.request;
  e.respondWith(
    caches.open(VERSION).then(async (cache) => {
      const cached = await cache.match(key);
      const fresh = fetch(e.request)
        .then((res) => {
          if (res.ok) cache.put(key, res.clone());
          return res;
        })
        .catch(() => cached);
      return cached || fresh;
    })
  );
});
