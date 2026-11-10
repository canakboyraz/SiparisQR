/* Panel service worker: sipariş bildirimlerini gösterir ve dokununca siparişi açar. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });

self.addEventListener("push", function (event) {
  var d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { title: "Yeni bildirim", body: event.data && event.data.text() }; }
  var title = d.title || "Yeni sipariş";
  event.waitUntil(self.registration.showNotification(title, {
    body: d.body || "",
    tag: d.tag || undefined,
    renotify: !!d.tag,
    requireInteraction: true,
    icon: "/static/icon-192.png",
    badge: "/static/badge-72.png",
    vibrate: [200, 100, 200],
    data: { url: d.url || "/panel/orders" }
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/panel/orders";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (new URL(c.url).pathname.indexOf("/panel") === 0 && "focus" in c) {
        return c.navigate ? c.navigate(url).then(function (w) { return (w || c).focus(); }) : c.focus();
      }
    }
    return self.clients.openWindow(url);
  }));
});
