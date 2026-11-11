"""Müşteri tarafı: QR ile açılan mağaza, sipariş gönderme, sipariş durumu ve sadakat kartı."""
import json

from flask import Blueprint, Response, abort, g, jsonify, render_template, request, send_from_directory, url_for

import core
import db
import webpush
from app import public_store_url, push_subject, rate_limited, store_base

bp = Blueprint("store", __name__)


def _biz(slug=None):
    biz = core.business_by_slug(slug) if slug else g.host_biz
    if not biz:
        abort(404, "Mağaza bulunamadı.")
    g.base = store_base(biz)
    return biz


def _store_page(biz):
    groups = core.catalog(biz["id"])
    vmap = core.variants_by_product([p["id"] for _, items in groups for p in items])
    products = {}
    for _, items in groups:
        for p in items:
            vs = vmap.get(p["id"], [])
            products[p["id"]] = {
                "id": p["id"], "name": p["name"], "price": p["price"], "unit": p["unit"], "desc": p["description"],
                "img": url_for("store.image", image_id=p["image_id"]) if p["image_id"] else "",
                "ok": core.product_available(p, vs), "left": p["stock"] if p["stock"] is not None else None,
                "v": [{"id": v["id"], "name": v["name"], "price": v["price"], "ok": bool(p["available"]) and core.in_stock(v["stock"]),
                       "left": v["stock"]} for v in vs],
            }
            p["_ok"] = products[p["id"]]["ok"]
            p["_variants"] = vs
    zs = core.zones(biz["id"])
    config = {
        "orderUrl": f"{g.base}/api/order", "statusBase": f"{g.base}/o/",
        "cartKey": f"sepet:{biz['slug']}", "refKey": f"davet:{biz['slug']}", "minOrder": str(core.money(biz["min_order"])),
        "deliveryFee": str(core.money(biz["delivery_fee"])),
        "freeOver": str(core.money(biz["free_delivery_over"])),
        "zones": [{"id": z["id"], "name": z["name"], "fee": str(core.money(z["fee"])), "freeOver": str(core.money(z["free_over"]))} for z in zs],
        "first": {"type": biz["first_discount_type"], "value": str(core.money(biz["first_discount_value"]))} if core.first_order_offer(biz) else None,
        "open": bool(biz["ordering_open"]) and core.subscription_ok(biz) and bool(core.payment_options(biz)),
    }
    return render_template("store.html", biz=biz, groups=groups, products=products, config=config,
                           reward=core.reward_rule(biz), first_offer=core.first_order_offer(biz), zones=zs,
                           pay_options=core.payment_options(biz), sub_ok=core.subscription_ok(biz))


@bp.get("/")
def home():
    if g.host_biz:
        return _store_page(_biz())
    from routes.auth import signup_open
    demo = db.one("SELECT slug FROM businesses WHERE is_demo = 1 AND active = 1 ORDER BY id LIMIT 1")
    return render_template("landing.html", ps=core.platform_settings(), signup=signup_open(), demo=demo)


@bp.get("/s/<slug>/")
def store_slug(slug):
    return _store_page(_biz(slug))


@bp.get("/s/<slug>")
def store_slug_noslash(slug):
    from flask import redirect
    return redirect(f"/s/{slug}/", 301)


def _place(biz):
    if not request.is_json:
        abort(400)
    ip = request.remote_addr or "?"
    if rate_limited(f"order:{biz['id']}:{ip}", 6, 600):
        return jsonify(ok=False, error="Kısa sürede çok fazla sipariş gönderildi. Birkaç dakika sonra tekrar dene."), 429
    data = request.get_json(silent=True) or {}
    phone = core.normalize_phone(data.get("phone"))
    if phone:
        waiting = db.one("SELECT COUNT(*) AS n FROM orders WHERE business_id = ? AND customer_phone = ? "
                         "AND status = 'pending'", (biz["id"], phone))
        if int(waiting["n"]) >= 5:
            return jsonify(ok=False, error="Onay bekleyen 5 siparişin var. Onaylanınca yenisini verebilirsin."), 429
    try:
        order = core.create_order(biz, data)
    except core.OrderError as e:
        return jsonify(ok=False, error=str(e)), 400
    n_lines = len(core.order_lines(order["id"]))
    webpush.notify_business(biz["id"], {
        "title": f"Yeni sipariş #{order['id']} · {core.tl(order['total'])}",
        "body": f"{order['customer_name']} · {n_lines} kalem · {core.PAYMENT[order['payment']]}. Onay bekliyor.",
        "url": f"/panel/orders/{order['id']}", "tag": f"order{order['id']}",
    }, push_subject())
    resp = {"ok": True, "id": order["id"], "url": f"{g.base}/o/{order['token']}"}
    if order["payment"] == "card":
        resp["pay_url"] = f"{g.base}/pay/{order['token']}"
    return jsonify(**resp)


@bp.post("/api/order")
def place_host():
    return _place(_biz())


@bp.post("/s/<slug>/api/order")
def place_slug(slug):
    return _place(_biz(slug))


def _status_data(biz, order):
    card = core.loyalty_card(biz, order["customer_id"])
    return {
        "cargo": {"company": order["cargo_company"], "no": order["cargo_tracking"], "url": order["cargo_url"]}
        if order["cargo_tracking"] or order["cargo_company"] else None,
        "id": order["id"], "status": order["status"], "label": core.STATUS[order["status"]],
        "message": order["owner_message"], "reason": order["reject_reason"], "paid": bool(order["paid"]),
        "paid_amount": core.tl(core.payment_state(order)["paid"]) if order["paid_cents"] else "",
        "remaining": core.tl(core.payment_state(order)["remaining"]),
        "card": None if not card else {
            "every": card["every"], "filled": card["filled"], "left": card["left"], "active": card["active"],
            "text": card["text"], "rewards": [r["text"] for r in card["rewards"]], "unit": card["unit"],
            "mode": card["mode"], "per": card["per"], "rule": card["rule"],
            "got": int(order["stamps"] or 0) if order["counts_for_reward"] and order["status"] in core.COUNTED else 0,
        },
    }


def _status(biz, token, as_json=False):
    order = db.one("SELECT * FROM orders WHERE token = ? AND business_id = ?", (token, biz["id"]))
    if not order:
        abort(404, "Sipariş bulunamadı.")
    data = _status_data(biz, order)
    if as_json:
        resp = jsonify(data)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    lines = core.order_lines(order["id"])
    customer = db.one("SELECT * FROM customers WHERE id = ?", (order["customer_id"],))
    invite = None
    if biz["referral_active"] and biz["referral_text"] and order["status"] in core.COUNTED:
        invite = public_store_url(biz) + "?ref=" + core.customer_ref_code(customer)
    reorder = [{"id": l["product_id"], "variant": l["variant_id"], "qty": l["qty"]} for l in lines if l["product_id"]]
    card_pay = None
    if order["payment"] == "card" and not order["paid"] and order["status"] not in ("rejected", "cancelled"):
        card_pay = f"{g.base}/pay/{order['token']}"
    return render_template("status.html", biz=biz, order=order, lines=lines, data=data, invite=invite,
                           reorder=reorder, card_pay=card_pay, cart_key=f"sepet:{biz['slug']}")


@bp.get("/o/<token>")
def status_host(token):
    return _status(_biz(), token)


@bp.get("/o/<token>.json")
def status_host_json(token):
    return _status(_biz(), token, True)


@bp.get("/s/<slug>/o/<token>")
def status_slug(slug, token):
    return _status(_biz(slug), token)


@bp.get("/s/<slug>/o/<token>.json")
def status_slug_json(slug, token):
    return _status(_biz(slug), token, True)


@bp.get("/img/<int:image_id>")
def image(image_id):
    img = db.one("SELECT mime, data FROM images WHERE id = ?", (image_id,))
    if not img:
        abort(404)
    resp = Response(img["data"], mimetype=img["mime"])
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@bp.get("/sw.js")
def service_worker():
    resp = send_from_directory("static/js", "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp



# ── Kartla ödeme (PayTR) ────────────────────────────────────────────────────

def _pay(biz, token):
    import paytr
    from flask import redirect
    order = db.one("SELECT * FROM orders WHERE token = ? AND business_id = ?", (token, biz["id"]))
    if not order:
        abort(404, "Sipariş bulunamadı.")
    status_url = f"{g.base}/o/{token}"
    if order["payment"] != "card" or order["paid"] or order["status"] in ("rejected", "cancelled"):
        return redirect(status_url)
    if not core.card_ready(biz):
        return render_template("pay.html", biz=biz, order=order, error="Kartla ödeme şu an kullanılamıyor.",
                               status_url=status_url)
    ip = request.remote_addr or "0.0.0.0"
    if rate_limited(f"pay:{order['id']}", 8, 600):
        return render_template("pay.html", biz=biz, order=order, error="Çok fazla deneme yapıldı, biraz sonra tekrar dene.",
                               status_url=status_url), 429
    oid = paytr.merchant_oid()
    root = request.host_url.rstrip("/")
    try:
        payload = paytr.build_payload(biz, order, core.order_lines(order["id"]), oid, ip,
                                      f"{root}{status_url}?odeme=tamam", f"{root}{status_url}?odeme=hata")
        db.insert("card_payments", {"business_id": biz["id"], "order_id": order["id"], "merchant_oid": oid,
                                    "amount_cents": int(payload["payment_amount"]), "status": "started",
                                    "created_at": core.now()})
        db.commit()
        iframe = paytr.get_token(payload)
    except Exception as e:  # PayTR'ye ulaşılamaz ya da bilgiler yanlış
        db.rollback()
        log_reason = str(e)[:300]
        db.execute("UPDATE card_payments SET status = 'failed', reason = ? WHERE merchant_oid = ?", (log_reason, oid))
        db.commit()
        return render_template("pay.html", biz=biz, order=order, status_url=status_url,
                               error="Ödeme sayfası açılamadı. Biraz sonra tekrar dene ya da işletmeyle iletişime geç.")
    return render_template("pay.html", biz=biz, order=order, iframe_token=iframe, status_url=status_url)


@bp.get("/pay/<token>")
def pay_host(token):
    return _pay(_biz(), token)


@bp.get("/s/<slug>/pay/<token>")
def pay_slug(slug, token):
    return _pay(_biz(slug), token)


@bp.post("/paytr/callback")
def paytr_callback():
    import paytr
    f = request.form
    oid = f.get("merchant_oid", "")
    cp = db.one("SELECT * FROM card_payments WHERE merchant_oid = ?", (oid,))
    if not cp:
        return "PAYTR notification failed", 400
    biz = db.one("SELECT * FROM businesses WHERE id = ?", (cp["business_id"],))
    if not biz or not paytr.verify_callback(biz, oid, f.get("status", ""), f.get("total_amount", ""), f.get("hash", "")):
        return "PAYTR notification failed", 400
    if cp["status"] == "success":
        return "OK"
    order = core.get_order(cp["order_id"])
    try:
        total = int(f.get("total_amount") or 0)
    except ValueError:
        total = 0
    if f.get("status") == "success" and total >= cp["amount_cents"]:
        db.execute("UPDATE card_payments SET status = 'success' WHERE id = ?", (cp["id"],))
        db.commit()
        core.add_payment(biz["id"], order, str(core.from_cents(cp["amount_cents"])), "card", "PayTR ile kartla ödendi")
        webpush.notify_business(biz["id"], {
            "title": f"💳 Kartla ödendi · #{order['id']}", "body": f"{order['customer_name']} {core.tl(core.from_cents(cp['amount_cents']))} ödedi.",
            "url": f"/panel/orders/{order['id']}", "tag": f"pay{order['id']}"}, push_subject())
    else:
        db.execute("UPDATE card_payments SET status = 'failed', reason = ? WHERE id = ?",
                   ((f.get("failed_reason_msg") or f.get("status") or "")[:300], cp["id"]))
        db.commit()
    return "OK"
