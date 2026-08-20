const CACHE = "certego-inventering-v5";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => Promise.all(
    SHELL.map(u => c.add(u).catch(() => {}))
  )));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  if (e.request.url.includes("/api/")) return;
  const url = new URL(e.request.url);
  const isDoc = e.request.mode === "navigate"
    || url.pathname.endsWith("/index.html")
    || url.pathname.endsWith("/");
  // Network-first for the app document so updates always come through when online.
  if (isDoc) {
    e.respondWith(
      fetch(e.request).then(resp => {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put("./index.html", copy)).catch(() => {});
        return resp;
      }).catch(() => caches.match("./index.html").then(h => h || caches.match("./")))
    );
    return;
  }
  // Cache-first for everything else (icons, manifest, CDN libs) for offline speed.
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(resp => {
      const copy = resp.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {});
      return resp;
    }).catch(() => caches.match("./index.html")))
  );
});
