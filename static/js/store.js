(function () {
  "use strict";
  var root = document.getElementById("st");
  if (!root) return;
  var C = JSON.parse(root.getAttribute("data-config"));
  var P = window.PRODUCTS || {};
  var $ = function (id) { return document.getElementById(id); };
  var cart = load();
  var detailId = null, detailQty = 1;

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
      Object.keys(c).forEach(function (k) { if (!P[k] || !P[k].ok || !(c[k] > 0)) delete c[k]; });
      return c;
    } catch (e) { return {}; }
  }
  function save() { try { localStorage.setItem(C.cartKey, JSON.stringify(cart)); } catch (e) {} }
  function count() { return Object.keys(cart).reduce(function (s, k) { return s + cart[k]; }, 0); }
  function subtotal() { return Object.keys(cart).reduce(function (s, k) { return s + num(P[k].price) * cart[k]; }, 0); }
  function fulfilment() {
    var r = document.querySelector('input[name="fulfilment"]:checked') || document.querySelector('input[name="fulfilment"]');
    return r ? r.value : "pickup";
  }
  function fee(sub) {
    if (fulfilment() !== "delivery") return 0;
    var free = num(C.freeOver);
    if (free > 0 && sub >= free) return 0;
    return num(C.deliveryFee);
  }

  function setQty(id, q) {
    id = String(id);
    q = Math.max(0, Math.min(99, q));
    if (q) cart[id] = q; else delete cart[id];
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
      var p = P[k], li = document.createElement("li");
      li.innerHTML = '<div class="st-ln"><b></b><small></small></div>' +
        '<div class="st-qty"><button type="button" data-act="dec" aria-label="Azalt">−</button><b></b>' +
        '<button type="button" data-act="inc" aria-label="Artır">+</button></div><span class="st-lt"></span>';
      li.querySelector(".st-ln b").textContent = p.name;
      li.querySelector(".st-ln small").textContent = (p.unit ? p.unit + " · " : "") + tl(p.price);
      li.querySelector(".st-qty b").textContent = cart[k];
      li.querySelector(".st-lt").textContent = tl(num(p.price) * cart[k]);
      li.setAttribute("data-id", k);
      list.appendChild(li);
    });
    var sub = subtotal(), f = fee(sub);
    $("st-sub").textContent = tl(sub);
    $("st-fee").textContent = f ? tl(f) : (fulfilment() === "delivery" ? "Ücretsiz" : "—");
    $("st-feerow").hidden = fulfilment() !== "delivery";
    $("st-grand").textContent = tl(sub + f);
    var addr = $("st-addr"), pick = $("st-pickup");
    if (addr) addr.hidden = fulfilment() !== "delivery";
    if (pick) pick.hidden = fulfilment() !== "pickup";
    if (!count()) close($("st-cart"));
  }

  // ── Sheet (alttan açılan pencere) ─────────────────────────────────────────
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
    bg.addEventListener("click", function (e) {
      if (e.target === bg || e.target.hasAttribute("data-close")) close(bg);
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll(".sheet-bg.is-open").forEach(close);
  });

  // ── Ürün kartları ─────────────────────────────────────────────────────────
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]");
    if (b) {
      var box = b.closest("[data-add]") || b.closest("li[data-id]");
      if (!box) return;
      var id = box.getAttribute("data-add") || box.getAttribute("data-id");
      var q = cart[id] || 0, act = b.getAttribute("data-act");
      setQty(id, act === "dec" ? q - 1 : q + 1);
      if (act === "add" && navigator.vibrate) navigator.vibrate(8);
      return;
    }
    var o = e.target.closest("[data-open]");
    if (o) showDetail(o.getAttribute("data-open"));
  });

  function showDetail(id) {
    var p = P[id];
    if (!p) return;
    detailId = id; detailQty = cart[id] || 1;
    var img = $("st-d-img");
    img.hidden = !p.img; if (p.img) img.src = p.img;
    $("st-d-name").textContent = p.name;
    $("st-d-unit").textContent = p.unit || "";
    $("st-d-desc").textContent = p.desc || "";
    $("st-d-price").textContent = tl(p.price);
    $("st-d-qty").textContent = detailQty;
    var can = p.ok && C.open;
    $("st-d-add").hidden = !can; $("st-d-qtybox").hidden = !can;
    $("st-d-out").hidden = p.ok;
    $("st-d-add").textContent = cart[id] ? "Sepeti güncelle" : "Sepete ekle";
    open($("st-detail"));
  }
  $("st-d-dec").addEventListener("click", function () { detailQty = Math.max(1, detailQty - 1); $("st-d-qty").textContent = detailQty; });
  $("st-d-inc").addEventListener("click", function () { detailQty = Math.min(99, detailQty + 1); $("st-d-qty").textContent = detailQty; });
  $("st-d-add").addEventListener("click", function () { setQty(detailId, detailQty); close($("st-detail")); });

  var bar = $("st-cartbar");
  if (bar) bar.addEventListener("click", function () { renderCart(); open($("st-cart")); });
  document.querySelectorAll('input[name="fulfilment"]').forEach(function (r) { r.addEventListener("change", renderCart); });

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
    if (fulfilment() === "delivery" && el.address && (el.address.value || "").trim().length < 10) { el.address.focus(); return showErr("Teslimat adresini açık yaz."); }
    if (!pay) return showErr("Bir ödeme yöntemi seç.");
    if (!el.kvkk.checked) return showErr("Devam etmek için bilgilendirme kutusunu işaretle.");
    var min = num(C.minOrder);
    if (min > 0 && subtotal() < min) return showErr("En az sipariş tutarı " + tl(min) + ".");

    var btn = $("st-send");
    btn.disabled = true; btn.textContent = "Gönderiliyor…";
    var body = {
      items: Object.keys(cart).map(function (k) { return { id: +k, qty: cart[k] }; }),
      name: el.name.value, phone: el.phone.value, address: el.address ? el.address.value : "",
      note: el.note.value, payment: pay.value, fulfilment: fulfilment()
    };
    fetch(C.orderUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: "Sunucuya ulaşılamadı." }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.error || "Sipariş gönderilemedi.");
        try {
          localStorage.setItem(ME, JSON.stringify({ name: body.name, phone: body.phone, address: body.address }));
          localStorage.removeItem(C.cartKey);
          var mine = JSON.parse(localStorage.getItem("siparislerim") || "[]");
          mine.unshift(res.url); localStorage.setItem("siparislerim", JSON.stringify(mine.slice(0, 20)));
        } catch (e) {}
        location.href = res.url;
      })
      .catch(function (err) {
        showErr(err.message || "Bağlantı sorunu. Tekrar dene.");
        btn.disabled = false; btn.textContent = "Siparişi gönder";
      });
  });

  render();
})();
