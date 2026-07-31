/* couple-daily service worker — offline app shell + installability.
 * Strategy: cache-first for static assets, network-first for pages with an
 * offline fallback. Bump CACHE_VERSION to invalidate old caches on deploy.
 */
const CACHE_VERSION = "couple-daily-v1";
const APP_SHELL = [
  "/offline",
  "/static/style.css",
  "/static/emoji/two_hearts.svg",
  "/static/emoji/sun.svg",
  "/static/emoji/spiral_calendar.svg",
  "/static/emoji/chart_increasing.svg",
  "/static/emoji/gear.svg",
  "/static/emoji/sparkling_heart.svg",
  "/static/emoji/love_letter.svg",
  "/static/emoji/locked.svg",
  "/static/emoji/sparkles.svg",
  "/static/emoji/party_popper.svg",
  "/static/emoji/speech_balloon.svg",
  "/static/icons/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Cache-first for our static assets.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
        return res;
      }).catch(() => hit))
    );
    return;
  }

  // Network-first for navigations; fall back to offline shell.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/offline"))
    );
    return;
  }
});
