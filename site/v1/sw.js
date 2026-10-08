// The reading site moved from /v1/ to the main address (Oct 8 2026). This worker removes itself and
// its saved copies so phones that opened /v1/ follow the redirect.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) await caches.delete(k);
    await self.registration.unregister();
    for (const c of await self.clients.matchAll({type: "window"})) c.navigate(c.url);
  })());
});
