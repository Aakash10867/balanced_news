// Nishpaksh offline support (nishpaksh_version_1.0). The page itself and its icons are kept for
// offline use; news (the feed, articles) is always asked for fresh and the last copy is used offline;
// fonts are kept once loaded.
const VERSION = "np-main-25";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png", "./icon-maskable-192.png",
               "./icon-maskable-512.png", "./apple-touch-icon.png", "./mark.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
async function fresh(req) {           // network first, the saved copy when offline
  const cache = await caches.open(VERSION);
  try {
    const r = await fetch(req);
    if (r.ok) cache.put(req, r.clone());
    return r;
  } catch (err) {
    const hit = await cache.match(req, {ignoreSearch: req.mode === "navigate"});
    if (hit) return hit;
    throw err;
  }
}
async function kept(req) {            // saved copy first (fonts never change)
  const cache = await caches.open(VERSION);
  const hit = await cache.match(req);
  if (hit) return hit;
  const r = await fetch(req);
  if (r.ok || r.type === "opaque") cache.put(req, r.clone());
  return r;
}
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const u = new URL(req.url);
  if (u.pathname.includes("/audio/")) return;          // audio is read in ranges: straight to the network
  if (u.hostname === "fonts.gstatic.com" || u.hostname === "fonts.googleapis.com") { e.respondWith(kept(req)); return; }
  if (u.hostname === "raw.githubusercontent.com" || u.origin === self.location.origin) { e.respondWith(fresh(req)); return; }
  // Supabase and anything else: straight to the network
});

// notifications (nishpaksh/notify.py sends them: audio ready, the daily recap)
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {title: "Nishpaksh", body: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(d.title || "Nishpaksh",
    {body: d.body || "", tag: d.tag, data: {url: d.url || "./"}, icon: "icon-192.png", badge: "icon-192.png"}));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "./";
  e.waitUntil((async () => {
    for (const c of await self.clients.matchAll({type: "window", includeUncontrolled: true})) {
      if ("focus" in c && c.url.startsWith(self.registration.scope)) { await c.focus(); if ("navigate" in c) return c.navigate(url); return; }
    }
    return self.clients.openWindow(url);
  })());
});
