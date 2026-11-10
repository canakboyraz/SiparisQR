(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var csrf = (document.querySelector('input[name="csrf"]') || {}).value || "";
  var hasPush = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  var isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

  function b64ToBytes(s) {
    var pad = "=".repeat((4 - s.length % 4) % 4), raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {}) }).then(function (r) { return r.json(); });
  }
  function registration() { return navigator.serviceWorker.register("/sw.js", { scope: "/" }); }

  // ── Kopyala düğmeleri ──
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy]");
    if (!b) return;
    navigator.clipboard && navigator.clipboard.writeText(b.getAttribute("data-copy")).then(function () {
      var o = b.textContent; b.textContent = "Kopyalandı ✓"; setTimeout(function () { b.textContent = o; }, 1600);
    });
  });

  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-dismiss-push]")) return;
    try { localStorage.setItem("push-dismiss", "1"); } catch (err) {}
    $("pn-push").hidden = true;
  });

  // ── Bildirim önerisi şeridi ──
  var banner = $("pn-push");
  if (banner && !banner.hasAttribute("data-off") && !$("np") && hasPush) {
    registration().then(function (reg) { return reg.pushManager.getSubscription(); }).then(function (sub) {
      var dismissed = false;
      try { dismissed = localStorage.getItem("push-dismiss") === "1"; } catch (e) {}
      if (!sub && Notification.permission !== "denied" && !dismissed) banner.hidden = false;
    }).catch(function () {});
  }

  // ── Bildirimler sayfası ──
  var np = $("np");
  if (np) {
    var state = $("np-state"), on = $("np-on"), off = $("np-off"), test = $("np-test");
    var say = function (t) { state.textContent = t; };
    var refresh = function () {
      if (!hasPush) {
        if (isIOS && !standalone) { say("iPhone'da bildirim almak için paneli ana ekrana eklemen gerekiyor."); $("np-ios").hidden = false; }
        else say("Bu tarayıcı bildirimleri desteklemiyor. Android'de Chrome, bilgisayarda Chrome/Edge/Firefox kullan.");
        return;
      }
      if (Notification.permission === "denied") {
        say("Bildirim izni engellenmiş. Tarayıcı ayarlarından bu site için bildirimlere izin ver, sonra sayfayı yenile.");
        return;
      }
      registration().then(function (reg) { return reg.pushManager.getSubscription(); }).then(function (sub) {
        on.hidden = !!sub; off.hidden = !sub; test.hidden = !sub;
        say(sub ? "✓ Bu cihazda bildirimler açık. Yeni sipariş gelince haber vereceğiz." : "Bu cihazda bildirimler kapalı.");
        if (sub) post(np.getAttribute("data-sub"), sub.toJSON()).catch(function () {});
      });
    };
    on.addEventListener("click", function () {
      on.disabled = true; say("İzin isteniyor…");
      Notification.requestPermission().then(function (perm) {
        if (perm !== "granted") throw new Error("İzin verilmedi.");
        return registration();
      }).then(function (reg) {
        return navigator.serviceWorker.ready.then(function () {
          return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(np.getAttribute("data-vapid")) });
        });
      }).then(function (sub) { return post(np.getAttribute("data-sub"), sub.toJSON()); })
        .then(function (res) {
          if (!res.ok) throw new Error(res.error || "Kaydedilemedi.");
          try { localStorage.removeItem("push-dismiss"); } catch (e) {}
          return post(np.getAttribute("data-test"));
        })
        .then(function () { refresh(); setTimeout(function () { location.reload(); }, 1200); })
        .catch(function (e) { say("Açılamadı: " + e.message); })
        .then(function () { on.disabled = false; });
    });
    off.addEventListener("click", function () {
      registration().then(function (reg) { return reg.pushManager.getSubscription(); }).then(function (sub) {
        if (!sub) return;
        return post(np.getAttribute("data-unsub"), { endpoint: sub.endpoint }).then(function () { return sub.unsubscribe(); });
      }).then(function () { location.reload(); });
    });
    test.addEventListener("click", function () {
      test.disabled = true;
      post(np.getAttribute("data-test")).then(function (r) { say(r.ok ? "Deneme bildirimi gönderildi. Birkaç saniye içinde gelmeli." : "Kayıtlı cihaz bulunamadı."); })
        .then(function () { test.disabled = false; });
    });
    refresh();
  }

  // ── Sipariş listesi: canlı yenileme + ses ──
  var list = $("orders");
  if (list) {
    var latest = +list.getAttribute("data-latest"), url = list.getAttribute("data-poll");
    var ding = function () {
      try {
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        [0, 0.18].forEach(function (t) {
          var o = ctx.createOscillator(), g = ctx.createGain();
          o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
          g.gain.setValueAtTime(0.0001, ctx.currentTime + t);
          g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + t + 0.02);
          g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t + 0.16);
          o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.17);
        });
      } catch (e) {}
    };
    setInterval(function () {
      if (document.hidden) return;
      fetch(url, { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
        if (!d || d.latest <= latest) return;
        latest = d.latest; ding();
        if (/tab=(active|done)/.test(location.search)) { $("pn-new").hidden = false; }
        else location.reload();
      }).catch(function () {});
    }, 15000);
  }
})();
