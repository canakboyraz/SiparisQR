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
        say("Bildirim izni engellenmiş görünüyor. Tarayıcı ayarlarından bu siteye bildirim izni verip sayfayı yeniler misin?");
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

  // ── Sipariş listesi: canlı yenileme + ses + sesli kasa modu ──
  var list = $("orders");
  if (list) {
    var latest = +list.getAttribute("data-latest"), url = list.getAttribute("data-poll");
    var ctx = null, kasa = false, alarm = null, wake = null, timer = null;
    function ensureCtx() {
      try {
        if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
        if (ctx.state === "suspended") ctx.resume();
      } catch (e) {}
      return ctx;
    }
    function chime(loud) {
      var c = ensureCtx(); if (!c) return;
      var notes = loud ? [880, 1175, 1568, 1175, 1568] : [880, 1175];
      notes.forEach(function (f, i) {
        var t = c.currentTime + i * 0.16, o = c.createOscillator(), g = c.createGain();
        o.type = "square"; o.frequency.value = f; o.connect(g); g.connect(c.destination);
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(loud ? 0.5 : 0.25, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.14);
        o.start(t); o.stop(t + 0.15);
      });
    }
    function keepAwake() {
      if (!("wakeLock" in navigator) || !kasa) return;
      navigator.wakeLock.request("screen").then(function (w) { wake = w; }).catch(function () {});
    }
    function paintBtn() {
      var btn = $("kasa-btn"); if (!btn) return;
      btn.classList.toggle("is-on", kasa);
      btn.textContent = kasa ? "🔔 Sesli mod açık" : "🔔 Sesli mod";
      btn.title = kasa ? "Kapatmak için dokun" : "Yeni siparişte sesli uyarı";
      btn.onclick = onKasaBtn;
    }
    function setKasa(on, quiet) {
      kasa = on;
      try { if (on) localStorage.setItem("kasa", "1"); else localStorage.removeItem("kasa"); } catch (e) {}
      paintBtn();
      if (on) { keepAwake(); if (!quiet) chime(false); } else if (wake) { wake.release(); wake = null; }
      schedule();
    }
    function refreshList() {
      return fetch(location.href, { cache: "no-store" }).then(function (r) { return r.text(); }).then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var cur = document.querySelector(".pn-main"), nxt = doc.querySelector(".pn-main");
        if (!cur || !nxt) return;
        ["#kasa-alert"].forEach(function (sel) { var n = nxt.querySelector(sel); if (n) n.remove(); });
        var keep = ["kasa-alert"].map(function (id) { return $(id); });
        cur.innerHTML = nxt.innerHTML;
        keep.forEach(function (el) { cur.appendChild(el); });
        list = $("orders");
        paintBtn();
        document.querySelectorAll(".pn-tabs .badge, .pn-side .badge").forEach(function (b) { b.remove(); });
      }).catch(function () {});
    }
    function ring(last) {
      $("kasa-title").textContent = last ? "#" + last.id + " · " + last.total : "";
      $("kasa-sub").textContent = last ? last.name : "";
      $("kasa-open").href = last ? "/panel/orders/" + last.id : "/panel/orders";
      $("kasa-alert").hidden = false;
      clearInterval(alarm);
      chime(true);
      alarm = setInterval(function () { chime(true); if (navigator.vibrate) navigator.vibrate([300, 150, 300]); }, 2200);
    }
    $("kasa-ok").addEventListener("click", function () {
      clearInterval(alarm); alarm = null; $("kasa-alert").hidden = true; refreshList();
    });
    function poll() {
      if (document.hidden && !kasa) return;
      fetch(url, { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
        if (!d || d.latest <= latest) return;
        latest = d.latest;
        if (kasa) { ring(d.last); return; }
        chime(false);
        if (/tab=(active|done)/.test(location.search)) { $("pn-new").hidden = false; }
        else location.reload();
      }).catch(function () {});
    }
    function schedule() { clearInterval(timer); timer = setInterval(poll, kasa ? 8000 : 15000); }
    function onKasaBtn() { if (kasa) setKasa(false); else { ensureCtx(); setKasa(true); } }
    paintBtn();
    document.addEventListener("visibilitychange", function () { if (!document.hidden && kasa) { keepAwake(); poll(); } });
    var want = /[?&]kasa=1/.test(location.search);
    try { want = want || localStorage.getItem("kasa") === "1"; } catch (e) {}
    if (want) {
      // Daha önce açıldıysa sessizce açık devam eder; ekrana uyarı çıkmaz.
      // iPhone sesi ilk dokunuşta açar: sayfada herhangi bir yere ilk dokunuşta ses hazırlanır.
      setKasa(true, true);
      var unlock = function () {
        ensureCtx(); keepAwake();
        ["pointerdown", "touchend", "keydown"].forEach(function (ev) { document.removeEventListener(ev, unlock, true); });
      };
      ["pointerdown", "touchend", "keydown"].forEach(function (ev) { document.addEventListener(ev, unlock, true); });
    }
    schedule();
  }
})();
