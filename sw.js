/* Service worker: casca em cache para arranque instantâneo e leitura offline. */
const CACHE = "noticias-v3";
const CASCA = ["./", "./index.html", "./assets/styles.css", "./assets/app.js", "./manifest.webmanifest"];

self.addEventListener("install", (evento) => {
  evento.waitUntil(caches.open(CACHE).then((c) => c.addAll(CASCA)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    caches.keys()
      .then((chaves) => Promise.all(chaves.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (evento) => {
  const pedido = evento.request;
  if (pedido.method !== "GET" || new URL(pedido.url).origin !== location.origin) return;

  // Dados: rede primeiro (queremos notícias frescas), cache como rede de segurança.
  if (pedido.url.includes("/data/")) {
    evento.respondWith(
      fetch(pedido)
        .then((r) => { const c = r.clone(); caches.open(CACHE).then((ch) => ch.put(pedido, c)); return r; })
        .catch(() => caches.match(pedido))
    );
    return;
  }

  // Casca: cache primeiro, com actualização em segundo plano.
  evento.respondWith(
    caches.match(pedido).then((emCache) => {
      const naRede = fetch(pedido).then((r) => {
        if (r.ok) { const c = r.clone(); caches.open(CACHE).then((ch) => ch.put(pedido, c)); }
        return r;
      }).catch(() => emCache);
      return emCache || naRede;
    })
  );
});
