(function () {
  "use strict";
  var root = document.getElementById("os");
  if (!root) return;
  var $ = function (id) { return document.getElementById(id); };
  var ORDER_STEPS = ["pending", "approved", "on_the_way", "delivered"];
  var TEXT = {
    pending: "Siparişin işletmeye ulaştı. Onaylandığında bu ekran kendiliğinden güncellenir.",
    approved: "Siparişin onaylandı ve hazırlanıyor.",
    on_the_way: null,
    delivered: "Siparişin teslim edildi. Afiyet olsun!",
    rejected: "Siparişin onaylanmadı.",
    cancelled: "Sipariş iptal edildi."
  };
  var current = root.getAttribute("data-status");

  function paint(d) {
    var st = $("os-state");
    st.className = "os-card os-state is-" + d.status;
    $("os-label").textContent = d.label;
    if (d.status !== current) {
      var t = TEXT[d.status];
      if (t) $("os-text").textContent = t;
      else if (d.status === "on_the_way") $("os-text").textContent = document.querySelector('[data-step="on_the_way"]').textContent === "Yolda" ? "Siparişin yola çıktı." : "Siparişin hazır, gelip alabilirsin.";
      if (navigator.vibrate) navigator.vibrate([20, 40, 20]);
      current = d.status;
    }
    var msg = d.message || d.reason;
    $("os-msg").hidden = !msg; $("os-msg").textContent = msg || "";
    var idx = ORDER_STEPS.indexOf(d.status);
    document.querySelectorAll(".os-steps li").forEach(function (li, i) {
      li.classList.toggle("is-done", idx >= 0 && i <= idx);
      li.classList.toggle("is-now", i === idx);
    });
    document.querySelector(".os-steps").hidden = idx < 0;
    var iban = $("os-iban"), paid = $("os-paid");
    if (iban) iban.hidden = d.paid || idx < 0;
    if (paid) paid.hidden = !d.paid;
    var it = $("os-iban-text");
    if (it && d.paid_amount && !d.paid) it.innerHTML = "<b></b> ödemen alındı. Kalan <b></b> tutarı aşağıdaki hesaba gönder. Açıklamaya <b></b> yaz.";
    if (it && d.paid_amount && !d.paid) { var bs = it.querySelectorAll("b"); bs[0].textContent = d.paid_amount; bs[1].textContent = d.remaining; bs[2].textContent = "Sipariş " + d.id; }
    var cash = $("os-cash");
    if (cash && d.paid) cash.textContent = "✓ Ödemen alındı";
    else if (cash && d.paid_amount) cash.textContent = d.paid_amount + " alındı · kalan " + d.remaining;
    card(d.card);
    var cg = $("os-cargo");
    if (cg) {
      cg.hidden = !d.cargo;
      if (d.cargo) {
        $("os-cargo-co").textContent = d.cargo.company || "Kargo";
        $("os-cargo-no").textContent = d.cargo.no || "";
        $("os-cargo-copy").hidden = !d.cargo.no; $("os-cargo-copy").setAttribute("data-copy", d.cargo.no || "");
        $("os-cargo-url").hidden = !d.cargo.url; if (d.cargo.url) $("os-cargo-url").href = d.cargo.url;
      }
    }
  }

  function card(c) {
    var box = $("os-loyal");
    if (!c) { box.hidden = true; return; }
    box.hidden = false;
    var stamps = $("os-stamps");
    stamps.innerHTML = "";
    stamps.style.setProperty("--n", Math.min(c.every, 10));
    for (var i = 0; i < c.every; i++) {
      var s = document.createElement("span");
      if (i < c.filled) s.className = "is-on";
      if (i === c.every - 1) { s.classList.add("is-gift"); s.textContent = "🎁"; }
      stamps.appendChild(s);
    }
    var t;
    if (!c.active) t = "Kampanya şu an kapalı.";
    else if (c.filled === 0 && !c.rewards.length) t = "Her " + c.every + " siparişte " + c.text + " hediye. İlk damgan sipariş onaylanınca gelir.";
    else t = c.filled + " / " + c.every + " · " + c.text + " için " + c.left + " sipariş kaldı.";
    $("os-loyal-text").textContent = t;
    var gift = $("os-gift");
    gift.hidden = !c.rewards.length;
    if (c.rewards.length) gift.textContent = "🎉 Ödülün hazır: " + c.rewards.join(", ") + ". Bir sonraki siparişinde işletme sana verecek.";
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy]");
    if (!b) return;
    var text = b.getAttribute("data-copy");
    var done = function () { var o = b.textContent; b.textContent = "Kopyalandı ✓"; setTimeout(function () { b.textContent = o; }, 1800); };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () {});
    else { var ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); done(); }
  });

  var ro = $("os-reorder");
  if (ro) ro.addEventListener("click", function () {
    try {
      var c = {};
      JSON.parse(ro.getAttribute("data-items")).forEach(function (it) {
        var k = it.variant ? it.id + ":" + it.variant : String(it.id);
        c[k] = (c[k] || 0) + it.qty;
      });
      localStorage.setItem(ro.getAttribute("data-cart"), JSON.stringify(c));
    } catch (e) {}
    location.href = ro.getAttribute("data-store");
  });

  paint(window.ORDER);
  var url = root.getAttribute("data-poll"), timer;
  function poll() {
    if (document.hidden) return;
    fetch(url, { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) paint(d); }).catch(function () {});
  }
  function schedule() {
    clearInterval(timer);
    var fast = current === "pending" || current === "approved" || current === "on_the_way";
    timer = setInterval(function () { poll(); if (!fast) clearInterval(timer); }, fast ? 8000 : 60000);
  }
  schedule();
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { poll(); schedule(); } });
})();
