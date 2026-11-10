(function () {
  "use strict";
  var root = document.getElementById("st");
  if (!root) return;
  var C = JSON.parse(root.getAttribute("data-config"));
  var P = window.PRODUCTS || {};
  var $ = function (id) { return document.getElementById(id); };
  var detailId = null, detailVar = null, detailQty = 1;

  // ── Sepet: anahtar "ürün" ya da "ürün:seçenek" ────────────────────────────
  function parse(key) { var a = String(key).split(":"); return { p: P[a[0]], v: a[1] ? findVar(P[a[0]], a[1]) : null, pid: a[0], vid: a[1] || null }; }
  function findVar(p, vid) { if (!p) return null; for (var i = 0; i < p.v.length; i++) if (String(p.v[i].id) === String(vid)) return p.v[i]; return null; }
  function itemOk(key) {
    var it = parse(key);
    if (!it.p || !it.p.ok) return false;
    if (it.p.v.length) return !!(it.v && it.v.ok);
    return !it.vid;
  }
  function priceOf(key) { var it = parse(key); return num(it.v ? it.v.price : it.p.price); }
  function leftOf(key) { var it = parse(key); var l = it.v ? it.v.left : it.p.left; return l === null || l === undefined ? 99 : Math.min(99, l); }

  var cart = load();
  function num(v) { return Math.round(parseFloat(v || 0) * 100) / 100; }
  function tl(v) {
    v = num(v);
    var whole = Math.floor(v), frac = Math.round((v - whole) * 100);
    var t = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    return frac ? t + "," + (frac < 10 ? "0" : "") + frac + " TL" : t + " TL";
  }
  function load() {
    try {
      var c = JSON.parse(localStorage.getItem(C.cartKey) || "{}");
      Object.keys(c).forEach(function (k) { if (!itemOk(k) || !(c[k] > 0)) delete c[k]; else c[k] = Math.min(c[k], leftOf(k)); });
      return c;
    } catch (e) { return {}; }
  }
  function save() { try { localStorage.setItem(C.cartKey, JSON.stringify(cart)); } catch (e) {} }
  function count() { return Object.keys(cart).reduce(function (s, k) { return s + cart[k]; }, 0); }
  function subtotal() { return Object.keys(cart).reduce(function (s, k) { return s + priceOf(k) * cart[k]; }, 0); }
  function fulfilment() {
    var r = document.querySelector('input[name="fulfilment"]:checked') || document.querySelector('input[name="fulfilment"]');
    return r ? r.value : "pickup";
  }
  function zone() {
    var sel = document.querySelector('select[name="zone"]');
    if (!sel || !sel.value) return null;
    for (var i = 0; i < C.zones.length; i++) if (String(C.zones[i].id) === sel.value) return C.zones[i];
    return null;
  }
  function fee(sub) {
    if (fulfilment() !== "delivery") return 0;
    if (C.zones.length) {
      var z = zone();
      if (!z) return null;
      if (num(z.freeOver) > 0 && sub >= num(z.freeOver)) return 0;
      return num(z.fee);
    }
    var free = num(C.freeOver);
    if (free > 0 && sub >= free) return 0;
    return num(C.deliveryFee);
  }
  function firstDiscount(sub) {
    if (!C.first || hasOrdered()) return 0;
    var v = num(C.first.value);
    var d = C.first.type === "percent" ? Math.round(sub * Math.min(v, 90)) / 100 : v;
    return Math.min(d, sub);
  }
  function mineKey() { return "siparislerim:" + C.cartKey.slice(6); }
  function mine() { try { return JSON.parse(localStorage.getItem(mineKey()) || "[]"); } catch (e) { return []; } }
  function hasOrdered() { return mine().length > 0; }

  function setQty(key, q) {
    key = String(key);
    q = Math.max(0, Math.min(leftOf(key), q));
    if (q) cart[key] = q; else delete cart[key];
    save(); render();
  }

  function render() {
    document.querySelectorAll(".st-add").forEach(function (box) {
      var q = cart[box.getAttribute("data-add")] || 0;
      box.querySelector(".st-plus").hidden = q > 0;
      var qty = box.querySelector(".st-qty");
      qty.hidden = q === 0;
      qty.querySelector("b").textContent = q;
    });
    // seçenekli ürünlerde karttaki "+" yanına sepet adedi
    document.querySelectorAll('.st-plus[data-open]').forEach(function (b) {
      var pid = b.getAttribute("data-open"), n = 0;
      Object.keys(cart).forEach(function (k) { if (k.split(":")[0] === pid) n += cart[k]; });
      b.textContent = n ? String(n) : "+";
      b.classList.toggle("has-n", n > 0);
    });
    var n = count(), bar = $("st-cartbar");
    if (bar) {
      bar.hidden = n === 0 || !C.open;
      $("st-count").textContent = n;
      $("st-total").textContent = tl(subtotal());
    }
    renderCart();
  }

  function renderCart() {
    var list = $("st-lines");
    if (!list) return;
    list.innerHTML = "";
    Object.keys(cart).forEach(function (k) {
      var it = parse(k), li = document.createElement("li");
      li.innerHTML = '<div class="st-ln"><b></b><small></small></div>' +
        '<div class="st-qty"><button type="button" data-act="dec" aria-label="Azalt">−</button><b></b>' +
        '<button type="button" data-act="inc" aria-label="Artır">+</button></div><span class="st-lt"></span>';
      li.querySelector(".st-ln b").textContent = it.p.name;
      var unit = it.v ? it.v.name : it.p.unit;
      li.querySelector(".st-ln small").textContent = (unit ? unit + " · " : "") + tl(priceOf(k));
      li.querySelector(".st-qty b").textContent = cart[k];
      li.querySelector(".st-lt").textContent = tl(priceOf(k) * cart[k]);
      li.setAttribute("data-key", k);
      list.appendChild(li);
    });
    var sub = subtotal(), f = fee(sub), d = firstDiscount(sub);
    $("st-sub").textContent = tl(sub);
    var dr = $("st-discrow");
    if (dr) { dr.hidden = !d; $("st-disc").textContent = "−" + tl(d); }
    $("st-fee").textContent = f === null ? "Bölge seç" : (f ? tl(f) : (fulfilment() === "delivery" ? "Ücretsiz" : "—"));
    $("st-feerow").hidden = fulfilment() !== "delivery";
    $("st-grand").textContent = tl(sub - d + (f || 0));
    var addr = $("st-addr"), pick = $("st-pickup"), zr = $("st-zone");
    if (addr) addr.hidden = fulfilment() !== "delivery";
    if (zr) zr.hidden = fulfilment() !== "delivery";
    if (pick) pick.hidden = fulfilment() !== "pickup";
    if (!count()) close($("st-cart"));
  }

  // ── Alttan açılan pencere ─────────────────────────────────────────────────
  var lastFocus = null;
  function open(el) {
    lastFocus = document.activeElement;
    el.hidden = false;
    document.body.classList.add("no-scroll");
    requestAnimationFrame(function () { el.classList.add("is-open"); });
    var f = el.querySelector("button, input, textarea");
    if (f) setTimeout(function () { f.focus({ preventScroll: true }); }, 60);
  }
  function close(el) {
    if (!el || el.hidden) return;
    el.classList.remove("is-open");
    setTimeout(function () { el.hidden = true; }, 220);
    if (!document.querySelector(".sheet-bg.is-open")) document.body.classList.remove("no-scroll");
    if (lastFocus) lastFocus.focus({ preventScroll: true });
  }
  document.querySelectorAll(".sheet-bg").forEach(function (bg) {
    bg.addEventListener("click", function (e) { if (e.target === bg || e.target.hasAttribute("data-close")) close(bg); });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll(".sheet-bg.is-open").forEach(close);
  });

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]");
    if (b) {
      var box = b.closest("[data-add]") || b.closest("li[data-key]");
      if (!box) return;
      var key = box.getAttribute("data-add") || box.getAttribute("data-key");
      var q = cart[key] || 0, act = b.getAttribute("data-act");
      if (act !== "dec" && q >= leftOf(key)) { flashLow(box, leftOf(key)); return; }
      setQty(key, act === "dec" ? q - 1 : q + 1);
      if (act === "add" && navigator.vibrate) navigator.vibrate(8);
      return;
    }
    var o = e.target.closest("[data-open]");
    if (o) showDetail(o.getAttribute("data-open"));
  });
  function flashLow(box, n) {
    box.classList.add("shake"); setTimeout(function () { box.classList.remove("shake"); }, 400);
    if (navigator.vibrate) navigator.vibrate([10, 40, 10]);
  }

  // ── Ürün detayı ───────────────────────────────────────────────────────────
  function detailKey() { return detailVar ? detailId + ":" + detailVar : String(detailId); }
  function paintDetail() {
    var p = P[detailId], v = detailVar ? findVar(p, detailVar) : null;
    $("st-d-price").textContent = tl(v ? v.price : p.price);
    $("st-d-qty").textContent = detailQty;
    var left = leftOf(detailKey()), low = $("st-d-low");
    low.hidden = !(left <= 5 && left > 0 && (v ? v.ok : p.ok));
    low.textContent = "Son " + left + " adet";
    var okNow = p.ok && C.open && (!p.v.length || (v && v.ok));
    $("st-d-add").hidden = !okNow; $("st-d-qtybox").hidden = !okNow;
    $("st-d-out").hidden = p.ok && (!v || v.ok);
    $("st-d-add").textContent = cart[detailKey()] ? "Sepeti güncelle" : "Sepete ekle";
  }
  function showDetail(id) {
    var p = P[id];
    if (!p) return;
    detailId = id; detailVar = null;
    var img = $("st-d-img");
    img.hidden = !p.img; if (p.img) img.src = p.img;
    $("st-d-name").textContent = p.name;
    $("st-d-unit").textContent = p.v.length ? "" : (p.unit || "");
    $("st-d-desc").textContent = p.desc || "";
    var fs = $("st-d-vars");
    fs.querySelectorAll("label").forEach(function (l) { l.remove(); });
    fs.hidden = !p.v.length;
    p.v.forEach(function (v, i) {
      var l = document.createElement("label");
      l.innerHTML = '<input type="radio" name="dvar"><span><b></b><small></small></span>';
      var inp = l.querySelector("input");
      inp.value = v.id; inp.disabled = !v.ok;
      l.querySelector("b").textContent = v.name;
      l.querySelector("small").textContent = v.ok ? tl(v.price) : "tükendi";
      inp.addEventListener("change", function () { detailVar = String(v.id); detailQty = cart[detailKey()] || 1; paintDetail(); });
      fs.appendChild(l);
      if (!detailVar && v.ok) { detailVar = String(v.id); inp.checked = true; }
    });
    detailQty = cart[detailKey()] || 1;
    paintDetail();
    open($("st-detail"));
  }
  $("st-d-dec").addEventListener("click", function () { detailQty = Math.max(1, detailQty - 1); paintDetail(); });
  $("st-d-inc").addEventListener("click", function () { detailQty = Math.min(leftOf(detailKey()), detailQty + 1); paintDetail(); });
  $("st-d-add").addEventListener("click", function () { setQty(detailKey(), detailQty); close($("st-detail")); });

  var bar = $("st-cartbar");
  if (bar) bar.addEventListener("click", function () { renderCart(); open($("st-cart")); });
  document.querySelectorAll('input[name="fulfilment"], select[name="zone"]').forEach(function (r) { r.addEventListener("change", renderCart); });

  // ── Davet kodu (?ref=) ve önceki siparişler ───────────────────────────────
  try {
    var ref = new URLSearchParams(location.search).get("ref");
    if (ref && !hasOrdered()) localStorage.setItem(C.refKey, ref.toUpperCase().slice(0, 10));
    if (localStorage.getItem(C.refKey) && !hasOrdered() && $("st-ref")) $("st-ref").hidden = false;
    var last = mine()[0], ml = $("st-mine");
    if (last && ml) { ml.href = last; ml.hidden = false; ml.textContent = "Son siparişim"; }
  } catch (e) {}

  // ── Müşteri bilgilerini hatırla ───────────────────────────────────────────
  var form = $("st-form"), ME = "musteri";
  try {
    var me = JSON.parse(localStorage.getItem(ME) || "{}");
    ["name", "phone", "address"].forEach(function (k) { if (me[k] && form.elements[k]) form.elements[k].value = me[k]; });
    if (me.name && form.elements.kvkk) form.elements.kvkk.checked = true;
  } catch (e) {}

  function showErr(msg) { var el = $("st-err"); el.textContent = msg; el.hidden = !msg; if (msg) el.scrollIntoView({ block: "nearest", behavior: "smooth" }); }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    showErr("");
    var el = form.elements, pay = form.querySelector('input[name="payment"]:checked');
    var phone = (el.phone.value || "").replace(/\D/g, "");
    if (!count()) return showErr("Sepetin boş.");
    if ((el.name.value || "").trim().length < 2) { el.name.focus(); return showErr("Adını ve soyadını yaz."); }
    if (!/^(90)?0?5\d{9}$/.test(phone)) { el.phone.focus(); return showErr("Telefon numaranı 05XX XXX XX XX şeklinde yaz."); }
    if (fulfilment() === "delivery" && C.zones.length && !zone()) { el.zone.focus(); return showErr("Teslimat bölgeni seç."); }
    if (fulfilment() === "delivery" && el.address && (el.address.value || "").trim().length < 10) { el.address.focus(); return showErr("Teslimat adresini açık yaz."); }
    if (!pay) return showErr("Bir ödeme yöntemi seç.");
    if (!el.kvkk.checked) return showErr("Devam etmek için bilgilendirme kutusunu işaretle.");
    var min = num(C.minOrder);
    if (min > 0 && subtotal() < min) return showErr("En az sipariş tutarı " + tl(min) + ".");

    var btn = $("st-send");
    btn.disabled = true; btn.textContent = "Gönderiliyor…";
    var ref = null;
    try { ref = localStorage.getItem(C.refKey); } catch (err) {}
    var body = {
      items: Object.keys(cart).map(function (k) { var it = parse(k); return { id: +it.pid, variant: it.vid ? +it.vid : null, qty: cart[k] }; }),
      name: el.name.value, phone: el.phone.value, address: el.address ? el.address.value : "",
      note: el.note.value, payment: pay.value, fulfilment: fulfilment(), zone: el.zone ? el.zone.value : null,
      ref: ref, marketing_ok: el.marketing_ok ? el.marketing_ok.checked : false,
      birth_day: el.birth_day ? el.birth_day.value : null, birth_month: el.birth_month ? el.birth_month.value : null
    };
    fetch(C.orderUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: "Sunucuya ulaşılamadı." }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.error || "Sipariş gönderilemedi.");
        try {
          localStorage.setItem(ME, JSON.stringify({ name: body.name, phone: body.phone, address: body.address }));
          localStorage.removeItem(C.cartKey);
          localStorage.removeItem(C.refKey);
          var list = mine(); list.unshift(res.url); localStorage.setItem(mineKey(), JSON.stringify(list.slice(0, 20)));
        } catch (err) {}
        location.href = res.pay_url || res.url;
      })
      .catch(function (err) {
        showErr(err.message || "Bağlantı sorunu. Tekrar dene.");
        btn.disabled = false; btn.textContent = "Siparişi gönder";
      });
  });

  render();
})();
