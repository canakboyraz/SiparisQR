/* Barkodla ürün ekleme.
   Kamera: tarayıcıda BarcodeDetector varsa onu, yoksa (iPhone Safari) aşağıdaki EAN-13 / EAN-8 çözücüyü kullanır.
   USB barkod okuyucu klavye gibi yazar + Enter'a basar: elle giriş kutusu bunu da karşılar. */
(function () {
  "use strict";

  // ── EAN-13 / EAN-8 çözücü (tarama çizgisi üzerinden) ─────────────────────────
  var L = ["3211", "2221", "2122", "1411", "1132", "1231", "1114", "1312", "1213", "3112"];
  var PAR = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"];
  var PATS = [];   // {d, set, w:[4]}
  L.forEach(function (s, d) {
    var w = s.split("").map(Number);
    PATS.push({ d: d, set: "L", w: w });
    PATS.push({ d: d, set: "G", w: w.slice().reverse() });
  });

  function matchDigit(runs, i, onlyL) {
    var s = runs[i] + runs[i + 1] + runs[i + 2] + runs[i + 3];
    if (!s) return null;
    var best = null, bestErr = 9;
    for (var k = 0; k < PATS.length; k++) {
      var p = PATS[k];
      if (onlyL && p.set !== "L") continue;
      var e = 0;
      for (var j = 0; j < 4; j++) e += Math.abs(runs[i + j] * 7 / s - p.w[j]);
      if (e < bestErr) { bestErr = e; best = p; }
    }
    return bestErr < 1.6 ? best : null;
  }

  function guardOk(runs, i, n, unit) {
    for (var j = 0; j < n; j++) { var r = runs[i + j] / unit; if (r < 0.45 || r > 1.8) return false; }
    return true;
  }

  function checksum(digits) {
    var n = digits.length, sum = 0;
    for (var i = 0; i < n - 1; i++) sum += digits[i] * (((n - 1 - i) % 2 === 1) ? 3 : 1);
    return (10 - (sum % 10)) % 10 === digits[n - 1];
  }

  // runs: koyu ile başlayan genişlik dizisi (koyu, açık, koyu, ...)
  function decodeRuns(runs) {
    var i, j;
    // EAN-13: 59 eleman, 95 modül
    for (i = 0; i + 59 <= runs.length; i += 2) {
      var tot = 0;
      for (j = 0; j < 59; j++) tot += runs[i + j];
      var unit = tot / 95;
      if (unit < 0.8) continue;
      if (!guardOk(runs, i, 3, unit) || !guardOk(runs, i + 27, 5, unit) || !guardOk(runs, i + 56, 3, unit)) continue;
      var digits = [], par = "", ok = true;
      for (j = 0; j < 6 && ok; j++) {
        var m = matchDigit(runs, i + 3 + j * 4, false);
        if (!m) ok = false; else { digits.push(m.d); par += m.set; }
      }
      for (j = 0; j < 6 && ok; j++) {
        var m2 = matchDigit(runs, i + 32 + j * 4, true);
        if (!m2) ok = false; else digits.push(m2.d);
      }
      if (!ok) continue;
      var first = PAR.indexOf(par);
      if (first < 0) continue;
      var all = [first].concat(digits);
      if (checksum(all)) return all.join("");
    }
    // EAN-8: 43 eleman, 67 modül
    for (i = 0; i + 43 <= runs.length; i += 2) {
      var t8 = 0;
      for (j = 0; j < 43; j++) t8 += runs[i + j];
      var u8 = t8 / 67;
      if (u8 < 0.8) continue;
      if (!guardOk(runs, i, 3, u8) || !guardOk(runs, i + 19, 5, u8) || !guardOk(runs, i + 40, 3, u8)) continue;
      var d8 = [], ok8 = true;
      for (j = 0; j < 4 && ok8; j++) { var a = matchDigit(runs, i + 3 + j * 4, true); if (!a) ok8 = false; else d8.push(a.d); }
      for (j = 0; j < 4 && ok8; j++) { var b = matchDigit(runs, i + 24 + j * 4, true); if (!b) ok8 = false; else d8.push(b.d); }
      if (ok8 && checksum(d8)) return d8.join("");
    }
    return null;
  }

  function lineRuns(gray, len, stride, start) {
    // yerel eşik: kayan ortalama ile koyu/açık ayrımı
    var vals = new Float32Array(len), i, min = 255, max = 0;
    for (i = 0; i < len; i++) { var v = gray[start + i * stride]; vals[i] = v; if (v < min) min = v; if (v > max) max = v; }
    if (max - min < 40) return [];
    var win = Math.max(8, len >> 4);
    var thr = new Float32Array(len);
    var pref = new Float32Array(len + 1);
    for (i = 0; i < len; i++) pref[i + 1] = pref[i] + vals[i];
    for (i = 0; i < len; i++) {
      var a = Math.max(0, i - win), b = Math.min(len, i + win + 1);
      var local = (pref[b] - pref[a]) / (b - a);
      thr[i] = (local + (min + max) / 2) / 2;
    }
    var runs = [], cur = vals[0] < thr[0], n = 0, started = false;
    for (i = 0; i < len; i++) {
      var dark = vals[i] < thr[i];
      if (dark === cur) { n++; continue; }
      if (started || cur) { runs.push(n); started = true; }
      cur = dark; n = 1;
    }
    if (started) runs.push(n);
    return runs;     // koyu ile başlar
  }

  function scanGray(gray, w, h) {
    var lines = 13, k, y, runs, code;
    for (k = 0; k < lines; k++) {
      y = Math.round(h * (0.3 + 0.4 * k / (lines - 1)));
      runs = lineRuns(gray, w, 1, y * w);
      code = decodeRuns(runs) || decodeRuns(runs.slice().reverse().slice(runs.length % 2 === 0 ? 1 : 0));
      if (code) return code;
    }
    // dikey tutulmuş barkod
    for (k = 0; k < lines; k++) {
      var x = Math.round(w * (0.3 + 0.4 * k / (lines - 1)));
      runs = lineRuns(gray, h, w, x);
      code = decodeRuns(runs) || decodeRuns(runs.slice().reverse().slice(runs.length % 2 === 0 ? 1 : 0));
      if (code) return code;
    }
    return null;
  }

  function decodeCanvas(ctx, w, h) {
    var img = ctx.getImageData(0, 0, w, h).data, gray = new Uint8ClampedArray(w * h);
    for (var i = 0, p = 0; i < gray.length; i++, p += 4) gray[i] = (img[p] * 299 + img[p + 1] * 587 + img[p + 2] * 114) / 1000;
    return scanGray(gray, w, h);
  }
  window.EAN = { decodeCanvas: decodeCanvas, decodeRuns: decodeRuns };

  // ── Sayfa ────────────────────────────────────────────────────────────────────
  var root = document.getElementById("sc");
  if (!root) return;
  var $ = function (id) { return document.getElementById(id); };
  var video = $("sc-video"), stream = null, detector = null, busy = false, current = null, lastCode = "", lastSeen = 0;
  var canvas = document.createElement("canvas"), ctx = canvas.getContext("2d", { willReadFrequently: true });
  var tl = function (s) { return String(s || "").replace(".", ",").replace(/,00$/, ""); };

  function beep() {
    try {
      var A = window.AudioContext || window.webkitAudioContext, a = new A(), o = a.createOscillator(), g = a.createGain();
      o.frequency.value = 1400; o.connect(g); g.connect(a.destination); g.gain.value = 0.08;
      o.start(); o.stop(a.currentTime + 0.09);
    } catch (e) {}
    if (navigator.vibrate) navigator.vibrate(40);
  }

  function startCam() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert("Bu tarayıcı kamerayı açamıyor. Barkodu kutuya yazabilir ya da USB okuyucu kullanabilirsin."); return;
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false })
      .then(function (s) {
        stream = s; video.srcObject = s; video.play();
        $("sc-view").hidden = false; $("sc-hint").hidden = false; $("sc-start").hidden = true;
        if ("BarcodeDetector" in window) {
          try { detector = new window.BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e", "code_128"] }); } catch (e) { detector = null; }
        }
        requestAnimationFrame(tick);
      })
      .catch(function () { alert("Kameraya izin verilmedi. Tarayıcı ayarlarından kamera iznini açabilir ya da barkodu elle yazabilirsin."); });
  }

  function stopCam() {
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null; $("sc-view").hidden = true; $("sc-hint").hidden = true; $("sc-start").hidden = false;
  }

  function found(code) {
    var now = Date.now();
    if (code === lastCode && now - lastSeen < 2500) return;
    lastCode = code; lastSeen = now;
    beep();
    lookup(code);
  }

  function tick() {
    if (!stream) return;
    if (busy || current || video.readyState < 2) { setTimeout(function () { requestAnimationFrame(tick); }, 200); return; }
    if (detector) {
      busy = true;
      detector.detect(video).then(function (r) {
        busy = false;
        if (r && r.length) found(r[0].rawValue);
        requestAnimationFrame(tick);
      }).catch(function () { busy = false; detector = null; requestAnimationFrame(tick); });
      return;
    }
    var vw = video.videoWidth, vh = video.videoHeight;
    if (!vw) { requestAnimationFrame(tick); return; }
    var w = Math.min(960, vw), h = Math.round(vh * w / vw);
    canvas.width = w; canvas.height = h;
    ctx.drawImage(video, 0, 0, w, h);
    var code = decodeCanvas(ctx, w, h);
    if (code) found(code);
    setTimeout(function () { requestAnimationFrame(tick); }, 60);
  }

  function lookup(code) {
    code = String(code).replace(/\D/g, "");
    if (code.length < 6) return;
    current = { barcode: code };
    $("sc-form").hidden = false; $("sc-err").hidden = true;
    $("sc-found").innerHTML = "<small>Barkod " + code + "</small><b>Aranıyor…</b>";
    ["sc-name", "sc-price", "sc-unit", "sc-stock"].forEach(function (id) { $(id).value = ""; });
    fetch(root.dataset.lookup + "?barcode=" + code, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var f = $("sc-found");
        if (d.existing) {
          current.existing = d.existing;
          f.innerHTML = "<small>Barkod " + code + " · mağazanda var</small><b></b><em>Şu anki fiyat: " + tl(d.existing.price) + " TL" +
            (d.existing.stock != null ? " · stok " + d.existing.stock : "") + "</em>";
          f.querySelector("b").textContent = d.existing.name;
          $("sc-name").value = d.existing.name; $("sc-unit").value = d.existing.unit || "";
          $("sc-price").value = tl(d.existing.price); $("sc-catwrap").hidden = true;
          $("sc-save").textContent = "Fiyatı güncelle ve sonrakine geç";
        } else {
          $("sc-catwrap").hidden = false; $("sc-save").textContent = "Ekle ve sonrakine geç";
          if (d.suggest) {
            f.innerHTML = "<small>Barkod " + code + " · " + (d.suggest.source === "off" ? "açık ürün veritabanında" : "ortak katalogda") +
              " bulundu</small><b></b><em>Adı kontrol et, fiyatı yaz.</em>";
            f.querySelector("b").textContent = d.suggest.name;
            $("sc-name").value = d.suggest.name; $("sc-unit").value = d.suggest.unit || "";
            if (d.suggest.category) {
              var opt = Array.prototype.find.call($("sc-cat").options, function (o) { return o.text.toLowerCase() === d.suggest.category.toLowerCase(); });
              if (opt) $("sc-cat").value = opt.value;
            }
          } else {
            f.innerHTML = "<small>Barkod " + code + "</small><b>Yeni ürün</b><em>Katalogda yok; adını ve fiyatını yaz. Bundan sonra diğer işletmelere de hazır gelecek.</em>";
          }
        }
        setTimeout(function () { (d.existing || d.suggest ? $("sc-price") : $("sc-name")).focus(); }, 60);
      })
      .catch(function () { $("sc-found").innerHTML = "<b>Bağlantı sorunu</b><em>Tekrar dener misin?</em>"; });
  }

  function save() {
    if (!current) return;
    var body = { barcode: current.barcode, name: $("sc-name").value, price: $("sc-price").value, unit: $("sc-unit").value,
                 stock: $("sc-stock").value, category_id: $("sc-cat").value || null };
    var btn = $("sc-save"); btn.disabled = true;
    fetch(root.dataset.save, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": root.dataset.csrf }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        if (!d.ok) { $("sc-err").textContent = d.error || "Kaydedilemedi."; $("sc-err").hidden = false; return; }
        var li = document.createElement("li");
        li.innerHTML = "<span></span><b></b><small></small>";
        li.querySelector("span").textContent = d.updated ? "↻" : "+";
        li.querySelector("b").textContent = d.name;
        li.querySelector("small").textContent = tl(d.price) + " TL";
        $("sc-log").insertBefore(li, $("sc-log").firstChild); $("sc-log-card").hidden = false;
        reset();
      })
      .catch(function () { btn.disabled = false; $("sc-err").textContent = "Bağlantı sorunu, tekrar dener misin?"; $("sc-err").hidden = false; });
  }

  function reset() {
    current = null; $("sc-form").hidden = true; $("sc-code").value = "";
    if (!stream) $("sc-code").focus();
  }

  $("sc-start").addEventListener("click", startCam);
  $("sc-save").addEventListener("click", save);
  $("sc-skip").addEventListener("click", reset);
  $("sc-price").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); save(); } });
  $("sc-manual").addEventListener("submit", function (e) { e.preventDefault(); lookup($("sc-code").value); });
  window.addEventListener("pagehide", stopCam);
  document.addEventListener("visibilitychange", function () { if (document.hidden) stopCam(); });
})();
