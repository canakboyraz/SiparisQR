"""İşletme sahibi paneli: sipariş onayı, ürünler, kampanya, müşteriler, ayarlar, QR ve bildirimler."""
import io
import json

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for
from PIL import Image, ImageOps

import core
import db
import webpush
from app import current_user, login_required, platform_name, public_store_url, push_subject

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
    return render_template("panel/order.html", active="orders", o=o, lines=core.order_lines(o["id"]),
                           customer=customer, previous=int(history["n"]), card=card,
                           given_here=given_here, earned_here=earned_here,
                           pay=core.payment_state(o), pays=core.payments(o["id"]))


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
    flash(msg, "ok")
    if earned:
        flash(f"🎁 Bu siparişle müşteri ödül kazandı: {earned['text']}. Siparişe ekleyip 'Verildi' işaretleyebilirsin.", "gift")
    if new in ("approved",):
        return redirect(url_for("panel.order", order_id=order_id))
    return redirect(url_for("panel.orders", tab="pending" if o["status"] == "pending" else "active"))


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
    return render_template("panel/products.html", active="products", groups=groups, cats=cats)


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


@bp.route("/products/new", methods=["GET", "POST"])
@bp.route("/products/<int:product_id>", methods=["GET", "POST"])
@login_required
def product_form(product_id=None):
    p = None
    if product_id:
        p = db.one("SELECT * FROM products WHERE id = ? AND business_id = ?", (product_id, g.biz["id"]))
        if not p:
            abort(404)
    cats = db.query("SELECT * FROM categories WHERE business_id = ? ORDER BY position, id", (g.biz["id"],))
    if request.method == "POST":
        f = request.form
        name = (f.get("name") or "").strip()[:100]
        try:
            price = core.money(f.get("price"))
        except ValueError:
            price = None
        if not name:
            flash("Ürün adını yaz.", "error")
        elif price is None or price <= 0:
            flash("Fiyatı doğru yaz (ör. 350 ya da 349,90).", "error")
        else:
            try:
                values = {"name": name, "description": (f.get("description") or "").strip()[:500],
                          "unit": (f.get("unit") or "").strip()[:40], "price": str(price),
                          "category_id": _category_id(f), "available": 1 if f.get("available") else 0,
                          "visible": 1 if f.get("visible") else 0}
                photo = request.files.get("photo")
                if photo and photo.filename:
                    values["image_id"] = save_image(photo, g.biz["id"])
                elif f.get("remove_photo"):
                    values["image_id"] = None
                if p:
                    db.update("products", p["id"], values)
                else:
                    pos = db.one("SELECT COALESCE(MAX(position), 0) AS m FROM products WHERE business_id = ?", (g.biz["id"],))
                    db.insert("products", {"business_id": g.biz["id"], "position": int(pos["m"]) + 1,
                                           "created_at": core.now(), **values})
                db.commit()
                flash("Ürün kaydedildi.", "ok")
                return redirect(url_for("panel.products"))
            except ValueError as e:
                db.rollback()
                flash(str(e), "error")
        p = {**(p or {}), **{k: f.get(k, "") for k in ("name", "description", "unit", "price")},
             "available": bool(f.get("available")), "visible": bool(f.get("visible")),
             "category_id": f.get("category_id", type=int), "image_id": (p or {}).get("image_id"), "id": product_id}
    return render_template("panel/product_form.html", active="products", p=p, cats=cats)


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
        try:
            every = int(f.get("reward_every") or 0)
            min_total = core.money(f.get("reward_min_total") or "0")
        except ValueError:
            every, min_total = 0, None
        text = (f.get("reward_text") or "").strip()[:120]
        active = 1 if f.get("reward_active") else 0
        if not 2 <= every <= 100:
            flash("Kaç siparişte bir ödül verileceğini 2 ile 100 arasında yaz.", "error")
        elif min_total is None:
            flash("En az tutarı doğru yaz.", "error")
        elif active and not text:
            flash("Ödülü yaz (ör. '1 kg zeytin' ya da '%10 indirim').", "error")
        else:
            db.update("businesses", g.biz["id"], {"reward_active": active, "reward_every": every,
                                                  "reward_text": text, "reward_min_total": str(min_total)})
            db.commit()
            flash("Kampanya kaydedildi." if active else "Kampanya kaydedildi (şu an kapalı).", "ok")
            return redirect(url_for("panel.campaign"))
    biz = db.one("SELECT * FROM businesses WHERE id = ?", (g.biz["id"],))
    stats = db.one("SELECT COUNT(*) AS earned, SUM(CASE WHEN status = 'given' THEN 1 ELSE 0 END) AS given "
                   "FROM rewards WHERE business_id = ?", (g.biz["id"],))
    return render_template("panel/campaign.html", active="campaign", b=biz, rule=core.reward_rule(biz),
                           stats=stats)


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
        accent = (f.get("accent") or "").strip()
        if len(accent) == 7 and accent.startswith("#"):
            v["accent"] = accent
        for key in ("delivery_fee", "free_delivery_over", "min_order"):
            try:
                v[key] = str(core.money(f.get(key) or "0"))
            except ValueError:
                errors.append("Tutarları rakamla yaz (ör. 50 ya da 49,90).")
                break
        if not v["pay_cash"] and not v["pay_iban"]:
            errors.append("En az bir ödeme yöntemi açık olmalı.")
        if v["pay_iban"] and not core.valid_iban(v["iban"]):
            errors.append("IBAN geçersiz görünüyor. TR ile başlayan 26 karakterlik IBAN'ı kontrol et.")
        if v["pay_iban"] and not v["iban_holder"]:
            errors.append("IBAN'daki hesap sahibinin adını yaz.")
        if not v["delivery"] and not v["pickup"]:
            errors.append("Adrese teslim ya da gel al seçeneklerinden en az biri açık olmalı.")
        if errors:
            for e in dict.fromkeys(errors):
                flash(e, "error")
            b = {**b, **v}
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
    return render_template("panel/settings.html", active="settings", b=b)


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
