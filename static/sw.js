// Keeps the app shell available offline and on bad signal. With a connection,
// the latest version always wins (so updates show up immediately); if the
// network is slow or gone, the saved copy opens instead. /api never goes through here.
const VERSION = "v4";
const SHELL = ["/", "/static/app.js", "/static/style.css", "/static/vendor/preact-htm.js", "/static/manifest.json", "/static/icon-180.png"];
const NETWORK_WAIT = 2500; // ms before falling back to the saved copy

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
    (async () => {
      const cache = await caches.open(VERSION);
      const network = fetch(e.request).then((res) => {
        if (res.ok) cache.put(key, res.clone());
        return res;
      });
      const fresh = await Promise.race([network.catch(() => null), new Promise((r) => setTimeout(r, NETWORK_WAIT))]);
      return fresh || (await cache.match(key)) || network;
    })()
  );
});
