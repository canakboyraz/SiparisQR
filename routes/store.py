"""Müşteri tarafı: QR ile açılan mağaza, sipariş gönderme, sipariş durumu ve sadakat kartı."""
import json

from flask import Blueprint, Response, abort, g, jsonify, render_template, request, send_from_directory, url_for

import core
import db
import webpush
from app import push_subject, rate_limited, store_base

bp = Blueprint("store", __name__)


def _biz(slug=None):
    biz = core.business_by_slug(slug) if slug else g.host_biz
    if not biz:
        abort(404, "Mağaza bulunamadı.")
    g.base = store_base(biz)
    return biz


def _store_page(biz):
    groups = core.catalog(biz["id"])
    products = {p["id"]: {"id": p["id"], "name": p["name"], "price": p["price"], "unit": p["unit"],
                          "desc": p["description"], "img": url_for("store.image", image_id=p["image_id"]) if p["image_id"] else "",
                          "ok": bool(p["available"])}
                for _, items in groups for p in items}
    config = {
        "orderUrl": f"{g.base}/api/order", "statusBase": f"{g.base}/o/",
        "cartKey": f"sepet:{biz['slug']}", "minOrder": str(core.money(biz["min_order"])),
        "deliveryFee": str(core.money(biz["delivery_fee"])),
        "freeOver": str(core.money(biz["free_delivery_over"])),
        "open": bool(biz["ordering_open"]),
    }
    return render_template("store.html", biz=biz, groups=groups, products=products, config=config, reward=core.reward_rule(biz))


@bp.get("/")
def home():
    if g.host_biz:
        return _store_page(_biz())
    return render_template("landing.html")


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
    return jsonify(ok=True, id=order["id"], url=f"{g.base}/o/{order['token']}")


@bp.post("/api/order")
def place_host():
    return _place(_biz())


@bp.post("/s/<slug>/api/order")
def place_slug(slug):
    return _place(_biz(slug))


def _status_data(biz, order):
    card = core.loyalty_card(biz, order["customer_id"])
    return {
        "id": order["id"], "status": order["status"], "label": core.STATUS[order["status"]],
        "message": order["owner_message"], "reason": order["reject_reason"], "paid": bool(order["paid"]),
        "paid_amount": core.tl(core.payment_state(order)["paid"]) if order["paid_cents"] else "",
        "remaining": core.tl(core.payment_state(order)["remaining"]),
        "card": None if not card else {
            "every": card["every"], "filled": card["filled"], "left": card["left"], "active": card["active"],
            "text": card["text"], "rewards": [r["text"] for r in card["rewards"]],
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
    return render_template("status.html", biz=biz, order=order, lines=core.order_lines(order["id"]),
                           data=data)


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
