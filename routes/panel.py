"""İşletme sahibi paneli: sipariş onayı, ürünler, kampanya, müşteriler, ayarlar, QR ve bildirimler."""
import io
import json

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for
from PIL import Image, ImageOps

import core
import db
import webpush
from app import current_user, login_required, platform_name, public_order_url, public_store_url, push_subject

bp = Blueprint("panel", __name__, url_prefix="/panel")

TABS = {
    "pending": ("Onay bekleyen", ("pending",)),
    "active": ("Hazırlanan", ("approved", "on_the_way")),
    "done": ("Biten", ("delivered", "rejected", "cancelled")),
}


def _counts(biz_id):
    rows = db.query("SELECT status, COUNT(*) AS n FROM orders WHERE business_id = ? GROUP BY status", (biz_id,))
    by = {r["status"]: int(r["n"]) for r in rows}
    return {k: sum(by.get(s, 0) for s in sts) for k, (_, sts) in TABS.items()}


@bp.app_context_processor
def nav():
    biz = getattr(g, "biz", None)
    if not biz:
        return {}
    unpaid = 0
    for o in db.query("SELECT total, paid_cents FROM orders WHERE business_id = ? AND status IN "
                      "('approved','on_the_way','delivered')", (biz["id"],)):
        if int(o["paid_cents"] or 0) < core.cents(o["total"]):
            unpaid += 1
    return {"nav_pending": _counts(biz["id"])["pending"], "nav_unpaid": unpaid, "biz": biz, "pay_state": core.payment_state,
            "PAY_METHODS": core.PAY_METHODS, "from_cents": core.from_cents}


@bp.get("/")
@login_required
def index():
    return redirect(url_for("panel.orders"))


# ── Siparişler ──────────────────────────────────────────────────────────────

@bp.get("/orders")
@login_required
def orders():
    tab = request.args.get("tab", "pending")
    if tab not in TABS:
        tab = "pending"
    sts = TABS[tab][1]
    marks = ",".join("?" for _ in sts)
    limit = 200 if tab != "done" else 100
    rows = db.query(f"SELECT * FROM orders WHERE business_id = ? AND status IN ({marks}) "
                    f"ORDER BY id {'ASC' if tab == 'pending' else 'DESC'} LIMIT {limit}", (g.biz["id"], *sts))
    has_reward = {r["customer_id"] for r in db.query(
        "SELECT DISTINCT customer_id FROM rewards WHERE business_id = ? AND status = 'earned'", (g.biz["id"],))}
    latest = db.one("SELECT MAX(id) AS m FROM orders WHERE business_id = ?", (g.biz["id"],))
    return render_template("panel/orders.html", active="orders", tab=tab, tabs=TABS, rows=rows,
                           counts=_counts(g.biz["id"]), has_reward=has_reward, latest=int(latest["m"] or 0))


@bp.get("/orders/poll")
@login_required
def poll():
    latest = db.one("SELECT MAX(id) AS m FROM orders WHERE business_id = ?", (g.biz["id"],))
    resp = jsonify(latest=int(latest["m"] or 0), counts=_counts(g.biz["id"]))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _order_or_404(order_id):
    o = db.one("SELECT * FROM orders WHERE id = ? AND business_id = ?", (order_id, g.biz["id"]))
    if not o:
        abort(404, "Sipariş bulunamadı.")
    return o


@bp.get("/orders/<int:order_id>")
@login_required
def order(order_id):
    o = _order_or_404(order_id)
    customer = db.one("SELECT * FROM customers WHERE id = ?", (o["customer_id"],))
    history = db.one("SELECT COUNT(*) AS n FROM orders WHERE customer_id = ? AND id <> ?", (o["customer_id"], o["id"]))
    card = core.loyalty_card(g.biz, o["customer_id"])
    given_here = db.query("SELECT * FROM rewards WHERE given_order_id = ?", (o["id"],))
    earned_here = db.query("SELECT * FROM rewards WHERE earned_order_id = ?", (o["id"],))
    pay = core.payment_state(o)
    url = public_order_url(g.biz, o)
    msgs = [(label, core.wa_link(o["customer_phone"], text)) for label, text in
            core.customer_messages(g.biz, o, url, pay, earned_here)]
    return render_template("panel/order.html", active="orders", o=o, lines=core.order_lines(o["id"]),
                           customer=customer, previous=int(history["n"]), card=card,
                           given_here=given_here, earned_here=earned_here, order_url=url,
                           pay=pay, pays=core.payments(o["id"]), msgs=msgs, KIND_LABEL=core.KIND_LABEL,
                           CARGO=CARGO_COMPANIES)


@bp.post("/orders/<int:order_id>/status")
@login_required
def order_status(order_id):
    o = _order_or_404(order_id)
    new = request.form.get("status")
    try:
        _, earned = core.set_status(g.biz, o, new, reason=request.form.get("reason", ""),
                                    message=request.form.get("message"))
    except core.OrderError as e:
        flash(str(e), "error")
        return redirect(url_for("panel.order", order_id=order_id))
    msg = {"approved": "Sipariş onaylandı. Müşteri ekranında görüyor.",
           "rejected": "Sipariş reddedildi.", "on_the_way": "Sipariş 'Yolda / hazır' olarak işaretlendi.",
           "delivered": "Sipariş teslim edildi.", "cancelled": "Sipariş iptal edildi."}.get(new, "Güncellendi.")
    flash(msg + " Aşağıdan müşteriye WhatsApp'tan haber verebilirsin.", "ok")
    for r in earned:
        flash(f"🎁 Bu siparişle müşteri ödül kazandı: {r['text']}. Siparişe ekleyip 'Verildi' işaretleyebilirsin.", "gift")
    return redirect(url_for("panel.order", order_id=order_id) + "#haber")


@bp.post("/orders/<int:order_id>/payments")
@login_required
def payment_add(order_id):
    o = _order_or_404(order_id)
    f = request.form
    amount = f.get("amount")
    if f.get("full"):
        rem = core.payment_state(o)["remaining"]
        if rem <= 0:
            flash("Bu siparişin ödemesi zaten tamam.", "ok")
            return redirect(url_for("panel.order", order_id=order_id) + "#odeme")
        amount = str(rem)
    try:
        o = core.add_payment(g.biz["id"], o, amount, f.get("method") or o["payment"], f.get("note", ""),
                             current_user()["id"])
    except core.OrderError as e:
        flash(str(e), "error")
        return redirect(url_for("panel.order", order_id=order_id) + "#odeme")
    st = core.payment_state(o)
    msg = {"paid": "Ödeme kaydedildi. Bu siparişin ödemesi tamam ✓",
           "partial": f"Ödeme kaydedildi. Kalan: {core.tl(st['remaining'])}",
           "over": f"Ödeme kaydedildi. Dikkat: {core.tl(st['over'])} fazla ödenmiş."}.get(st["state"], "Kaydedildi.")
    flash(msg, "ok" if st["state"] != "over" else "gift")
    back = f.get("back")
    if back and back.startswith("/panel"):
        return redirect(back)
    return redirect(url_for("panel.order", order_id=order_id) + "#odeme")


@bp.post("/payments/<int:payment_id>/delete")
@login_required
def payment_delete(payment_id):
    try:
        p = core.delete_payment(g.biz["id"], payment_id)
        flash(f"{core.tl(core.from_cents(p['amount_cents']))} ödeme kaydı silindi.", "ok")
        return redirect(url_for("panel.order", order_id=p["order_id"]) + "#odeme")
    except core.OrderError as e:
        flash(str(e), "error")
        return redirect(url_for("panel.payments_page"))


@bp.post("/orders/<int:order_id>/note")
@login_required
def order_note(order_id):
    o = _order_or_404(order_id)
    db.update("orders", o["id"], {"internal_note": (request.form.get("internal_note") or "").strip()[:1000]})
    db.commit()
    flash("Not kaydedildi.", "ok")
    return redirect(url_for("panel.order", order_id=order_id) + "#not")


PAY_TABS = {"open": "Eksik", "paid": "Tamam", "all": "Tümü"}


@bp.get("/payments")
@login_required
def payments_page():
    tab = request.args.get("tab", "open")
    if tab not in PAY_TABS:
        tab = "open"
    marks = ",".join("?" for _ in core.CLOSED)
    rows = db.query(f"SELECT * FROM orders WHERE business_id = ? AND status NOT IN ({marks}) "
                    f"ORDER BY id DESC LIMIT 500", (g.biz["id"], *core.CLOSED))
    items, owed, collected, open_n = [], 0, 0, 0
    for o in rows:
        st = core.payment_state(o)
        collected += int(o["paid_cents"] or 0)
        if st["state"] in ("none", "partial"):
            owed += core.cents(st["remaining"])
            open_n += 1
        if tab == "open" and st["state"] not in ("none", "partial"):
            continue
        if tab == "paid" and st["state"] not in ("paid", "over"):
            continue
        items.append((o, st))
    if tab == "open":
        items.sort(key=lambda x: x[0]["id"])          # en eski alacak üstte
    return render_template("panel/payments.html", active="payments", tab=tab, tabs=PAY_TABS, items=items,
                           owed=core.from_cents(owed), collected=core.from_cents(collected), open_n=open_n)


@bp.post("/orders/<int:order_id>/message")
@login_required
def order_message(order_id):
    o = _order_or_404(order_id)
    db.update("orders", o["id"], {"owner_message": (request.form.get("message") or "").strip()[:300],
                                  "updated_at": core.now()})
    db.commit()
    flash("Müşteriye not güncellendi.", "ok")
    return redirect(url_for("panel.order", order_id=order_id))


CARGO_COMPANIES = ["Yurtiçi Kargo", "Aras Kargo", "MNG Kargo", "PTT Kargo", "Sürat Kargo", "HepsiJet",
                   "Trendyol Express", "Kolay Gelsin", "UPS", "Kendi kuryemiz", "Diğer"]


@bp.post("/orders/<int:order_id>/cargo")
@login_required
def order_cargo(order_id):
    o = _order_or_404(order_id)
    f = request.form
    url = (f.get("cargo_url") or "").strip()[:400]
    if url and not url.startswith(("https://", "http://")):
        flash("Takip linki https:// ile başlamalı.", "error")
        return redirect(url_for("panel.order", order_id=order_id) + "#kargo")
    db.update("orders", o["id"], {"cargo_company": (f.get("cargo_company") or "").strip()[:60],
                                  "cargo_tracking": (f.get("cargo_tracking") or "").strip()[:80],
                                  "cargo_url": url, "updated_at": core.now()})
    db.commit()
    if f.get("and_ship") and o["status"] == "approved":
        try:
            core.set_status(g.biz, core.get_order(o["id"]), "on_the_way")
            flash("Kargo bilgisi kaydedildi, sipariş 'Yolda' oldu. Müşteriye WhatsApp'tan haber ver.", "ok")
            return redirect(url_for("panel.order", order_id=order_id) + "#haber")
        except core.OrderError as e:
            flash(str(e), "error")
    flash("Kargo bilgisi kaydedildi. Müşteri sipariş ekranında görüyor.", "ok")
    return redirect(url_for("panel.order", order_id=order_id) + "#kargo")


@bp.post("/rewards/<int:reward_id>/give")
@login_required
def give_reward(reward_id):
    order_id = request.form.get("order_id", type=int)
    if order_id:
        _order_or_404(order_id)
    try:
        core.give_reward(g.biz["id"], reward_id, order_id)
        flash("Ödül verildi olarak işaretlendi.", "ok")
    except core.OrderError as e:
        flash(str(e), "error")
    back = request.form.get("back")
    return redirect(back if back and back.startswith("/panel") else url_for("panel.customers"))


# ── Ürünler ─────────────────────────────────────────────────────────────────

def save_image(file_storage, biz_id, size=1100):
    raw = file_storage.read()
    if not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        out = io.BytesIO()
        if im.mode in ("RGBA", "LA", "P") and size <= 512:
            im.convert("RGBA").save(out, "PNG", optimize=True)
            mime = "image/png"
        else:
            if im.mode != "RGB":
                bg = Image.new("RGB", im.size, (255, 255, 255))
                rgba = im.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                im = bg
            im.save(out, "JPEG", quality=82, optimize=True, progressive=True)
            mime = "image/jpeg"
    except Exception:
        raise ValueError("Fotoğraf okunamadı. JPG ya da PNG yükle.") from None
    return db.insert("images", {"business_id": biz_id, "mime": mime, "data": db.blob(out.getvalue()),
                                "created_at": core.now()})


@bp.get("/products")
@login_required
def products():
    groups = core.catalog(g.biz["id"], only_visible=False)
    cats = db.query("SELECT * FROM categories WHERE business_id = ? ORDER BY position, id", (g.biz["id"],))
    vmap = core.variants_by_product([p["id"] for _, items in groups for p in items])
    return render_template("panel/products.html", active="products", groups=groups, cats=cats, vmap=vmap,
                           product_available=core.product_available)


def _category_id(form):
    new_name = (form.get("new_category") or "").strip()[:60]
    if new_name:
        exists = db.one("SELECT id FROM categories WHERE business_id = ? AND name = ?", (g.biz["id"], new_name))
        if exists:
            return exists["id"]
        pos = db.one("SELECT COALESCE(MAX(position), 0) AS m FROM categories WHERE business_id = ?", (g.biz["id"],))
        return db.insert("categories", {"business_id": g.biz["id"], "name": new_name, "position": int(pos["m"]) + 1})
    cid = form.get("category_id", type=int)
    if cid and db.one("SELECT id FROM categories WHERE id = ? AND business_id = ?", (cid, g.biz["id"])):
        return cid
    return None


def _stock(raw):
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        n = int(raw)
    except ValueError:
        raise ValueError("Stok adedini tam sayı olarak yaz (boş = sınırsız).") from None
    if n < 0 or n > 1_000_000:
        raise ValueError("Stok adedi 0 ile 1.000.000 arasında olmalı.")
    return n


def _variants_from_form(f):
    names, prices, stocks, ids = (f.getlist(k) for k in ("v_name", "v_price", "v_stock", "v_id"))
    out = []
    for i, name in enumerate(names):
        name = (name or "").strip()[:40]
        price_raw = prices[i] if i < len(prices) else ""
        if not name and not (price_raw or "").strip():
            continue
        if not name:
            raise ValueError("Her seçeneğe bir ad yaz (ör. 1 kg).")
        try:
            price = core.money(price_raw)
        except ValueError:
            raise ValueError(f"'{name}' seçeneğinin fiyatını doğru yaz.") from None
        if price <= 0:
            raise ValueError(f"'{name}' seçeneğinin fiyatı 0'dan büyük olmalı.")
        vid = ids[i] if i < len(ids) else ""
        out.append({"id": int(vid) if vid.isdigit() else None, "name": name, "price": str(price),
                    "stock": _stock(stocks[i] if i < len(stocks) else "")})
    if len(out) > 20:
        raise ValueError("En fazla 20 seçenek eklenebilir.")
    return out


def _save_variants(product_id, variants):
    existing = {v["id"] for v in db.query("SELECT id FROM product_variants WHERE product_id = ?", (product_id,))}
    keep = set()
    for pos, v in enumerate(variants):
        if v["id"] in existing:
            db.update("product_variants", v["id"], {"name": v["name"], "price": v["price"], "stock": v["stock"],
                                                    "position": pos})
            keep.add(v["id"])
        else:
            db.insert("product_variants", {"business_id": g.biz["id"], "product_id": product_id, "name": v["name"],
                                           "price": v["price"], "stock": v["stock"], "position": pos})
    for vid in existing - keep:
        db.execute("DELETE FROM product_variants WHERE id = ?", (vid,))


@bp.route("/products/new", methods=["GET", "POST"])
@bp.route("/products/<int:product_id>", methods=["GET", "POST"])
@login_required
def product_form(product_id=None):
    p = None
    variants = []
    if product_id:
        p = db.one("SELECT * FROM products WHERE id = ? AND business_id = ?", (product_id, g.biz["id"]))
        if not p:
            abort(404)
        variants = core.variants_by_product([p["id"]]).get(p["id"], [])
    cats = db.query("SELECT * FROM categories WHERE business_id = ? ORDER BY position, id", (g.biz["id"],))
    if request.method == "POST":
        f = request.form
        name = (f.get("name") or "").strip()[:100]
        try:
            new_variants = _variants_from_form(f)
            if new_variants:
                price = min(core.money(v["price"]) for v in new_variants)
            else:
                try:
                    price = core.money(f.get("price"))
                except ValueError:
                    raise ValueError("Fiyatı doğru yaz (ör. 350 ya da 349,90).") from None
                if price <= 0:
                    raise ValueError("Fiyatı doğru yaz (ör. 350 ya da 349,90).")
            if not name:
                raise ValueError("Ürün adını yaz.")
            values = {"name": name, "description": (f.get("description") or "").strip()[:500],
                      "unit": (f.get("unit") or "").strip()[:40], "price": str(price),
                      "stock": None if new_variants else _stock(f.get("stock")),
                      "category_id": _category_id(f), "available": 1 if f.get("available") else 0,
                      "visible": 1 if f.get("visible") else 0}
            photo = request.files.get("photo")
            if photo and photo.filename:
                values["image_id"] = save_image(photo, g.biz["id"])
            elif f.get("remove_photo"):
                values["image_id"] = None
            if p:
                db.update("products", p["id"], values)
                pid = p["id"]
            else:
                pos = db.one("SELECT COALESCE(MAX(position), 0) AS m FROM products WHERE business_id = ?", (g.biz["id"],))
                pid = db.insert("products", {"business_id": g.biz["id"], "position": int(pos["m"]) + 1,
                                             "created_at": core.now(), **values})
            _save_variants(pid, new_variants)
            db.commit()
            flash("Ürün kaydedildi.", "ok")
            return redirect(url_for("panel.products") + f"#p{pid}")
        except ValueError as e:
            db.rollback()
            flash(str(e), "error")
        p = {**(p or {}), **{k: f.get(k, "") for k in ("name", "description", "unit", "price", "stock")},
             "available": bool(f.get("available")), "visible": bool(f.get("visible")),
             "category_id": f.get("category_id", type=int), "image_id": (p or {}).get("image_id"), "id": product_id}
        variants = [{"id": (int(i) if i.isdigit() else ""), "name": n, "price": pr, "stock": st}
                    for i, n, pr, st in zip(f.getlist("v_id"), f.getlist("v_name"), f.getlist("v_price"), f.getlist("v_stock"))
                    if n or pr]
    return render_template("panel/product_form.html", active="products", p=p, cats=cats, variants=variants)


@bp.post("/products/<int:product_id>/toggle")
@login_required
def product_toggle(product_id):
    p = db.one("SELECT * FROM products WHERE id = ? AND business_id = ?", (product_id, g.biz["id"]))
    if not p:
        abort(404)
    db.update("products", p["id"], {"available": 0 if p["available"] else 1})
    db.commit()
    return redirect(url_for("panel.products") + f"#p{p['id']}")


@bp.post("/products/<int:product_id>/delete")
@login_required
def product_delete(product_id):
    p = db.one("SELECT * FROM products WHERE id = ? AND business_id = ?", (product_id, g.biz["id"]))
    if not p:
        abort(404)
    db.execute("DELETE FROM products WHERE id = ?", (p["id"],))
    db.commit()
    flash(f"{p['name']} silindi.", "ok")
    return redirect(url_for("panel.products"))


@bp.post("/products/<int:product_id>/move")
@login_required
def product_move(product_id):
    p = db.one("SELECT * FROM products WHERE id = ? AND business_id = ?", (product_id, g.biz["id"]))
    if not p:
        abort(404)
    same = db.query("SELECT id FROM products WHERE business_id = ? AND "
                    + ("category_id = ?" if p["category_id"] else "category_id IS NULL")
                    + " ORDER BY position, id", (g.biz["id"], p["category_id"]) if p["category_id"] else (g.biz["id"],))
    ids = [r["id"] for r in same]
    i = ids.index(p["id"])
    j = i - 1 if request.form.get("dir") == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, pid in enumerate(ids):
            db.execute("UPDATE products SET position = ? WHERE id = ?", (pos, pid))
        db.commit()
    return redirect(url_for("panel.products") + f"#p{p['id']}")


@bp.post("/categories")
@login_required
def categories():
    action = request.form.get("action")
    cid = request.form.get("id", type=int)
    if action == "add":
        _category_id(request.form)
    elif cid and db.one("SELECT id FROM categories WHERE id = ? AND business_id = ?", (cid, g.biz["id"])):
        if action == "rename":
            name = (request.form.get("name") or "").strip()[:60]
            if name:
                db.execute("UPDATE categories SET name = ? WHERE id = ?", (name, cid))
        elif action == "delete":
            db.execute("UPDATE products SET category_id = NULL WHERE category_id = ?", (cid,))
            db.execute("DELETE FROM categories WHERE id = ?", (cid,))
        elif action in ("up", "down"):
            ids = [r["id"] for r in db.query("SELECT id FROM categories WHERE business_id = ? ORDER BY position, id", (g.biz["id"],))]
            i = ids.index(cid)
            j = i - 1 if action == "up" else i + 1
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
                for pos, x in enumerate(ids):
                    db.execute("UPDATE categories SET position = ? WHERE id = ?", (pos, x))
    db.commit()
    return redirect(url_for("panel.products"))


# ── Kampanya / müşteriler ───────────────────────────────────────────────────

@bp.route("/campaign", methods=["GET", "POST"])
@login_required
def campaign():
    if request.method == "POST":
        f = request.form
        section = f.get("section", "stamp")
        try:
            if section == "stamp":
                try:
                    every = int(f.get("reward_every") or 0)
                except ValueError:
                    every = 0
                text = (f.get("reward_text") or "").strip()[:120]
                active = 1 if f.get("reward_active") else 0
                if not 2 <= every <= 100:
                    raise ValueError("Kaç siparişte bir ödül verileceğini 2 ile 100 arasında yaz.")
                if active and not text:
                    raise ValueError("Ödülü yaz (ör. '1 kg zeytin' ya da '%10 indirim').")
                values = {"reward_active": active, "reward_every": every, "reward_text": text,
                          "reward_min_total": str(core.money(f.get("reward_min_total") or "0"))}
            elif section == "first":
                kind = f.get("first_discount_type")
                val = core.money(f.get("first_discount_value") or "0")
                active = 1 if f.get("first_discount_active") else 0
                if kind not in ("percent", "amount"):
                    raise ValueError("İndirim türünü seç.")
                if active and val <= 0:
                    raise ValueError("İndirim miktarını yaz.")
                if kind == "percent" and val > 50:
                    raise ValueError("Yüzde indirim en fazla %50 olabilir.")
                values = {"first_discount_active": active, "first_discount_type": kind, "first_discount_value": str(val)}
            elif section == "birthday":
                text = (f.get("birthday_text") or "").strip()[:120]
                active = 1 if f.get("birthday_active") else 0
                if active and not text:
                    raise ValueError("Doğum günü hediyesini yaz (ör. 250 g zeytin).")
                values = {"birthday_active": active, "birthday_text": text}
            elif section == "referral":
                text = (f.get("referral_text") or "").strip()[:120]
                active = 1 if f.get("referral_active") else 0
                if active and not text:
                    raise ValueError("Davet ödülünü yaz (ör. 1 kg zeytin).")
                values = {"referral_active": active, "referral_text": text}
            else:
                abort(400)
            db.update("businesses", g.biz["id"], values)
            db.commit()
            flash("Kaydedildi.", "ok")
            return redirect(url_for("panel.campaign") + "#" + section)
        except ValueError as e:
            db.rollback()
            msg = str(e)
            flash("Tutarı rakamla yaz." if "geçersiz" in msg else msg, "error")
    biz = db.one("SELECT * FROM businesses WHERE id = ?", (g.biz["id"],))
    stats = db.one("SELECT COUNT(*) AS earned, SUM(CASE WHEN status = 'given' THEN 1 ELSE 0 END) AS given "
                   "FROM rewards WHERE business_id = ?", (g.biz["id"],))
    return render_template("panel/campaign.html", active="campaign", b=biz, rule=core.reward_rule(biz),
                           first=core.first_order_offer(biz), stats=stats)


# ── Toplu duyuru ────────────────────────────────────────────────────────────

@bp.route("/broadcast", methods=["GET", "POST"])
@login_required
def broadcast():
    bid = g.biz["id"]
    if request.method == "POST":
        if request.form.get("action") == "optout":
            cid = request.form.get("customer_id", type=int)
            db.execute("UPDATE customers SET marketing_ok = 0 WHERE id = ? AND business_id = ?", (cid, bid))
            db.commit()
            flash("Müşteri duyuru listesinden çıkarıldı.", "ok")
            return redirect(url_for("panel.broadcast"))
        msg = (request.form.get("message") or "").strip()[:1000]
        if len(msg) < 5:
            flash("Duyuru metnini yaz.", "error")
            return redirect(url_for("panel.broadcast"))
        bc = db.insert("broadcasts", {"business_id": bid, "message": msg, "created_at": core.now()})
        db.commit()
        return redirect(url_for("panel.broadcast", id=bc))
    current = None
    bc_id = request.args.get("id", type=int)
    if bc_id:
        current = db.one("SELECT * FROM broadcasts WHERE id = ? AND business_id = ?", (bc_id, bid))
    people = db.query("SELECT * FROM customers WHERE business_id = ? AND marketing_ok = 1 ORDER BY name", (bid,))
    links = []
    if current:
        store = public_store_url(g.biz)
        for p in people:
            first = (p["name"] or "").split()[0] if p["name"] else ""
            text = current["message"].replace("{ad}", first).replace("{link}", store)
            if "{link}" not in current["message"] and store not in text:
                text += f"\n\n{store}"
            text += "\n\n(Duyuru almak istemiyorsan 'İPTAL' yazman yeterli.)"
            links.append((p, core.wa_link(p["phone"], text)))
    history = db.query("SELECT * FROM broadcasts WHERE business_id = ? ORDER BY id DESC LIMIT 10", (bid,))
    return render_template("panel/broadcast.html", active="customers", people=people, current=current, links=links,
                           history=history)


@bp.get("/customers")
@login_required
def customers():
    q = (request.args.get("q") or "").strip()
    sql = ("SELECT c.*, (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS orders, "
           "(SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id AND o.counts_for_reward = 1 "
           " AND o.status IN ('approved','on_the_way','delivered')) AS counted, "
           "(SELECT MAX(o.created_at) FROM orders o WHERE o.customer_id = c.id) AS last_at "
           "FROM customers c WHERE c.business_id = ?")
    params = [g.biz["id"]]
    if q:
        digits = core.normalize_phone(q) or "".join(ch for ch in q if ch.isdigit())
        sql += " AND (LOWER(c.name) LIKE ? OR c.phone LIKE ?)"
        params += ["%" + q.lower() + "%", "%" + (digits or q) + "%"]   # % yalnızca parametrede
    rows = db.query(sql + " ORDER BY last_at DESC LIMIT 300", params)
    rewards = {}
    for r in db.query("SELECT * FROM rewards WHERE business_id = ? AND status = 'earned' ORDER BY id", (g.biz["id"],)):
        rewards.setdefault(r["customer_id"], []).append(r)
    every = max(int(g.biz["reward_every"] or 0), 2)
    return render_template("panel/customers.html", active="customers", rows=rows, rewards=rewards, every=every, q=q)


# ── Ayarlar ─────────────────────────────────────────────────────────────────

@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    b = db.one("SELECT * FROM businesses WHERE id = ?", (g.biz["id"],))
    if request.method == "POST":
        f = request.form
        errors = []
        v = {
            "name": (f.get("name") or "").strip()[:80] or b["name"],
            "tagline": (f.get("tagline") or "").strip()[:160],
            "phone": core.normalize_phone(f.get("phone")) or (f.get("phone") or "").strip()[:20],
            "whatsapp": core.normalize_phone(f.get("whatsapp")),
            "ordering_open": 1 if f.get("ordering_open") else 0,
            "closed_message": (f.get("closed_message") or "").strip()[:200],
            "pay_cash": 1 if f.get("pay_cash") else 0,
            "pay_iban": 1 if f.get("pay_iban") else 0,
            "iban": core.normalize_iban(f.get("iban")),
            "iban_holder": (f.get("iban_holder") or "").strip()[:80],
            "iban_bank": (f.get("iban_bank") or "").strip()[:60],
            "delivery": 1 if f.get("delivery") else 0,
            "pickup": 1 if f.get("pickup") else 0,
            "delivery_note": (f.get("delivery_note") or "").strip()[:200],
            "pickup_address": (f.get("pickup_address") or "").strip()[:200],
        }
        v["instagram"] = (f.get("instagram") or "").strip().lstrip("@")[:60]
        v["pay_card"] = 1 if f.get("pay_card") else 0
        v["paytr_test"] = 1 if f.get("paytr_test") else 0
        v["paytr_merchant_id"] = "".join(ch for ch in (f.get("paytr_merchant_id") or "") if ch.isdigit())[:20]
        import paytr as _paytr
        if (f.get("paytr_key") or "").strip():
            v["paytr_key_enc"] = _paytr.encrypt(f.get("paytr_key").strip())
        if (f.get("paytr_salt") or "").strip():
            v["paytr_salt_enc"] = _paytr.encrypt(f.get("paytr_salt").strip())
        accent = (f.get("accent") or "").strip()
        if len(accent) == 7 and accent.startswith("#"):
            v["accent"] = accent
        for key in ("delivery_fee", "free_delivery_over", "min_order"):
            try:
                v[key] = str(core.money(f.get(key) or "0"))
            except ValueError:
                errors.append("Tutarları rakamla yaz (ör. 50 ya da 49,90).")
                break
        if not v["pay_cash"] and not v["pay_iban"] and not v["pay_card"]:
            errors.append("En az bir ödeme yöntemi açık olmalı.")
        if v["pay_card"] and not (v["paytr_merchant_id"] and (v.get("paytr_key_enc") or b["paytr_key_enc"])
                                  and (v.get("paytr_salt_enc") or b["paytr_salt_enc"])):
            errors.append("Kartla ödeme için PayTR Mağaza No, Merchant Key ve Merchant Salt gerekli.")
        if v["pay_iban"] and not core.valid_iban(v["iban"]):
            errors.append("IBAN geçersiz görünüyor. TR ile başlayan 26 karakterlik IBAN'ı kontrol et.")
        if v["pay_iban"] and not v["iban_holder"]:
            errors.append("IBAN'daki hesap sahibinin adını yaz.")
        if not v["delivery"] and not v["pickup"]:
            errors.append("Adrese teslim ya da gel al seçeneklerinden en az biri açık olmalı.")
        if errors:
            for e in dict.fromkeys(errors):
                flash(e, "error")
            b = {**b, **{k: val for k, val in v.items() if not k.endswith("_enc")}}
        else:
            try:
                logo = request.files.get("logo")
                if logo and logo.filename:
                    v["logo_id"] = save_image(logo, g.biz["id"], size=512)
                elif f.get("remove_logo"):
                    v["logo_id"] = None
                db.update("businesses", g.biz["id"], v)
                db.commit()
                flash("Ayarlar kaydedildi.", "ok")
                return redirect(url_for("panel.settings"))
            except ValueError as e:
                db.rollback()
                flash(str(e), "error")
    from app import public_base
    return render_template("panel/settings.html", active="settings", b=b, zones=core.zones(g.biz["id"]),
                           callback_url=public_base() + "/paytr/callback")


# ── QR ──────────────────────────────────────────────────────────────────────

@bp.get("/qr")
@login_required
def qr():
    return render_template("panel/qr.html", active="qr", store_url=public_store_url(g.biz),
                           rule=core.reward_rule(g.biz))


@bp.get("/qr/print")
@login_required
def qr_print():
    return render_template("panel/qr_print.html", store_url=public_store_url(g.biz), rule=core.reward_rule(g.biz),
                           copies=min(max(request.args.get("n", 4, type=int), 1), 8))


# ── Bildirimler ─────────────────────────────────────────────────────────────

@bp.get("/notifications")
@login_required
def notifications():
    subs = db.query("SELECT * FROM push_subscriptions WHERE business_id = ? ORDER BY id DESC", (g.biz["id"],))
    return render_template("panel/notifications.html", active="notifications", subs=subs,
                           vapid=webpush.vapid_public_key_b64())


@bp.post("/push/subscribe")
@login_required
def push_subscribe():
    if getattr(g, "impersonating", False):
        return jsonify(ok=False, error="Yönetici olarak paneldeyken bildirim açılamaz; işletme sahibi kendi telefonunda açmalı."), 400
    data = request.get_json(silent=True) or {}
    endpoint = str(data.get("endpoint") or "")
    keys = data.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify(ok=False, error="Abonelik bilgisi eksik."), 400
    ua = (request.headers.get("User-Agent") or "")[:300]
    existing = db.one("SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    values = {"user_id": current_user()["id"], "business_id": g.biz["id"], "p256dh": keys["p256dh"],
              "auth": keys["auth"], "user_agent": ua}
    if existing:
        db.update("push_subscriptions", existing["id"], values)
    else:
        db.insert("push_subscriptions", {"endpoint": endpoint, "created_at": core.now(), **values})
    db.commit()
    return jsonify(ok=True)


@bp.post("/push/unsubscribe")
@login_required
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    sid = data.get("id") or request.form.get("id", type=int)
    endpoint = data.get("endpoint")
    if sid:
        db.execute("DELETE FROM push_subscriptions WHERE id = ? AND business_id = ?", (sid, g.biz["id"]))
    elif endpoint:
        db.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND business_id = ?", (endpoint, g.biz["id"]))
    db.commit()
    if request.is_json:
        return jsonify(ok=True)
    flash("Cihaz kaldırıldı.", "ok")
    return redirect(url_for("panel.notifications"))


@bp.post("/push/test")
@login_required
def push_test():
    n = webpush.notify_business(g.biz["id"], {
        "title": "Deneme bildirimi", "body": "Bildirimler çalışıyor. Yeni sipariş gelince böyle haber vereceğiz.",
        "url": "/panel/orders", "tag": "test"}, push_subject())
    if request.is_json:
        return jsonify(ok=bool(n), devices=n)
    flash(f"{n} cihaza deneme bildirimi gönderildi." if n else "Kayıtlı cihaz yok. Önce bildirimleri aç.",
          "ok" if n else "error")
    return redirect(url_for("panel.notifications"))


@bp.get("/manifest.webmanifest")
def manifest():
    data = {"name": f"{platform_name()} · Panel", "short_name": "Siparişler", "start_url": "/panel/orders",
            "scope": "/", "display": "standalone", "background_color": "#F6F4EF", "theme_color": "#1F2A24",
            "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")


# ── Rapor ───────────────────────────────────────────────────────────────────

PERIODS = {"today": "Bugün", "7": "7 gün", "30": "30 gün", "month": "Bu ay", "last": "Geçen ay"}


def _range(key):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(core.TZ)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "today":
        start, end = day0, day0 + timedelta(days=1)
    elif key == "7":
        start, end = day0 - timedelta(days=6), day0 + timedelta(days=1)
    elif key == "month":
        start = day0.replace(day=1)
        end = day0 + timedelta(days=1)
    elif key == "last":
        end = day0.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    else:
        start, end = day0 - timedelta(days=29), day0 + timedelta(days=1)
    iso = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end, iso(start), iso(end)


@bp.get("/reports")
@login_required
def reports():
    from datetime import timedelta
    key = request.args.get("p", "30")
    if key not in PERIODS:
        key = "30"
    start, end, s_iso, e_iso = _range(key)
    bid = g.biz["id"]
    rows = db.query("SELECT * FROM orders WHERE business_id = ? AND created_at >= ? AND created_at < ? ORDER BY id",
                    (bid, s_iso, e_iso))
    good = [o for o in rows if o["status"] in core.COUNTED]
    revenue = sum(core.cents(o["total"]) for o in good)
    collected = sum(int(o["paid_cents"] or 0) for o in good)
    discount = sum(core.cents(o["discount"]) for o in good)
    # yeni / tekrar gelen müşteri
    first_ids = {}
    if good:
        cids = sorted({o["customer_id"] for o in good})
        marks = ",".join("?" for _ in cids)
        for r in db.query(f"SELECT customer_id, MIN(id) AS first_id FROM orders WHERE customer_id IN ({marks}) "
                          f"AND status IN ('approved','on_the_way','delivered') GROUP BY customer_id", tuple(cids)):
            first_ids[r["customer_id"]] = r["first_id"]
    customers = {}
    for o in good:
        customers.setdefault(o["customer_id"], []).append(o)
    new_c = sum(1 for cid, os_ in customers.items() if first_ids.get(cid) in {o["id"] for o in os_})
    ret_c = len(customers) - new_c
    # en çok satanlar
    top = {}
    if good:
        ids = [o["id"] for o in good]
        marks = ",".join("?" for _ in ids)
        for l in db.query(f"SELECT name, unit, qty, line_total FROM order_lines WHERE order_id IN ({marks})", tuple(ids)):
            k = f"{l['name']} ({l['unit']})" if l["unit"] else l["name"]
            t = top.setdefault(k, {"name": k, "qty": 0, "cents": 0})
            t["qty"] += int(l["qty"])
            t["cents"] += core.cents(l["line_total"])
    top = sorted(top.values(), key=lambda x: (-x["cents"], -x["qty"]))[:10]
    # günlük ciro
    days = []
    d = start
    while d < end and len(days) < 62:
        days.append({"label": d.strftime("%d.%m"), "cents": 0, "n": 0, "key": d.strftime("%Y-%m-%d")})
        d += timedelta(days=1)
    by_day = {x["key"]: x for x in days}
    for o in good:
        k = core.local_time(o["created_at"], "%Y-%m-%d")
        if k in by_day:
            by_day[k]["cents"] += core.cents(o["total"])
            by_day[k]["n"] += 1
    peak = max([x["cents"] for x in days] or [0]) or 1
    rewards = db.query("SELECT kind, COUNT(*) AS n FROM rewards WHERE business_id = ? AND created_at >= ? "
                       "AND created_at < ? GROUP BY kind", (bid, s_iso, e_iso))
    stats = {
        "revenue": core.from_cents(revenue), "orders": len(good), "avg": core.from_cents(revenue // len(good) if good else 0),
        "collected": core.from_cents(collected), "owed": core.from_cents(max(revenue - collected, 0)),
        "discount": core.from_cents(discount),
        "rejected": sum(1 for o in rows if o["status"] in ("rejected", "cancelled")),
        "pending": sum(1 for o in rows if o["status"] == "pending"),
        "new_c": new_c, "ret_c": ret_c, "repeat_pct": round(ret_c * 100 / len(customers)) if customers else 0,
        "delivery": sum(1 for o in good if o["fulfilment"] == "delivery"),
        "pickup": sum(1 for o in good if o["fulfilment"] == "pickup"),
        "by_pay": {k: sum(1 for o in good if o["payment"] == k) for k in core.PAYMENT},
        "referral": sum(1 for o in good if o["referred_by"]),
        "first": sum(1 for o in good if core.money(o["discount"]) > 0),
        "rewards": {r["kind"]: int(r["n"]) for r in rewards},
    }
    return render_template("panel/reports.html", active="reports", key=key, periods=PERIODS, s=stats, top=top,
                           days=days, peak=peak, start=start, end=end - timedelta(days=1), KIND_LABEL=core.KIND_LABEL)


# ── Teslimat bölgeleri ──────────────────────────────────────────────────────

@bp.post("/zones")
@login_required
def zones():
    f = request.form
    action = f.get("action")
    zid = f.get("id", type=int)
    try:
        if action in ("add", "save"):
            name = (f.get("name") or "").strip()[:60]
            if not name:
                raise ValueError("Bölge adını yaz (ör. Kocaeli içi).")
            fee = str(core.money(f.get("fee") or "0"))
            free_over = str(core.money(f.get("free_over") or "0"))
            if action == "add":
                n = db.one("SELECT COUNT(*) AS n FROM shipping_zones WHERE business_id = ?", (g.biz["id"],))
                if int(n["n"]) >= 20:
                    raise ValueError("En fazla 20 bölge eklenebilir.")
                db.insert("shipping_zones", {"business_id": g.biz["id"], "name": name, "fee": fee,
                                             "free_over": free_over, "position": int(n["n"])})
            elif zid:
                db.execute("UPDATE shipping_zones SET name = ?, fee = ?, free_over = ? WHERE id = ? AND business_id = ?",
                           (name, fee, free_over, zid, g.biz["id"]))
        elif action == "delete" and zid:
            db.execute("DELETE FROM shipping_zones WHERE id = ? AND business_id = ?", (zid, g.biz["id"]))
        db.commit()
        flash("Teslimat bölgeleri güncellendi.", "ok")
    except ValueError as e:
        db.rollback()
        flash(str(e) if "geçersiz" not in str(e) else "Ücretleri rakamla yaz (ör. 50).", "error")
    return redirect(url_for("panel.settings") + "#bolgeler")
