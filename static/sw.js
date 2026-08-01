/* couple-daily service worker — offline app shell + installability.
 * Strategy: stale-while-revalidate for static CSS/JS (edits reach clients on
 * next load, no manual version bump needed), cache-first for immutable images,
 * network-first for pages with an offline fallback. Bump CACHE_VERSION to
 * invalidate old caches on deploy.
 */
const CACHE_VERSION = "couple-daily-v5";
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

  if (url.pathname.startsWith("/static/")) {
    // All static assets — CSS/JS AND emoji/icon images (.svg/.png) — use
    // stale-while-revalidate: serve cache instantly if present, but ALWAYS
    // refetch in the background and update the cache, so edits (e.g. changed
    // nav icons) reach clients on the next load without bumping CACHE_VERSION.
    // If not cached, go to network then cache.
    if (/\.(css|js|svg|png)$/.test(url.pathname)) {
      event.respondWith(
        caches.open(CACHE_VERSION).then((cache) =>
          cache.match(req).then((hit) => {
            const network = fetch(req).then((res) => {
              if (res && res.ok) cache.put(req, res.clone());
              return res;
            }).catch(() => hit);
            return hit || network;
          })
        )
      );
      return;
    }

    // Any other static asset type: cache-first.
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

/* ---- Web Push -------------------------------------------------------------
 * A push arrives as an encrypted JSON payload {title, body, url}. Show it as a
 * system notification; tapping it focuses an already-open tab on that url or
 * opens a new window. The in-app Notification row is written server-side at the
 * same fan-out point — this is purely the OS-level delivery layer.
 */
self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = { body: event.data ? event.data.text() : "" };
  }
  const title = payload.title || "우리의 하루";
  const url = payload.url || "/";
  const options = {
    body: payload.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    data: { url },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  const target = new URL(url, self.location.origin);
  event.waitUntil(
    clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          const cu = new URL(client.url);
          // Focus an existing tab already on this path (ignore hash noise).
          if (cu.pathname === target.pathname && "focus" in client) {
            client.navigate ? client.navigate(target.href) : null;
            return client.focus();
          }
        }
        if (clients.openWindow) return clients.openWindow(target.href);
      })
  );
});
