"""Bayi tarafı: mağazanın üstündeki 'Bayi girişi' -> kullanıcı kodu + şifre -> bayi fiyatlarıyla sipariş,
siparişlerim, ve (açıksa) kendi mağazasının paneline geçiş."""
from functools import wraps

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)

import core
import db
import dealers
from app import notify_owner, rate_limited, store_base

bp = Blueprint("dealer", __name__)


def _biz(slug=None):
    biz = core.business_by_slug(slug) if slug else g.host_biz
    if not biz or biz.get("parent_id"):
        abort(404, "Aradığın mağaza bulunamadı. Linki kontrol eder misin?")
    g.base = store_base(biz)
    return biz


def _route(rule, **opts):
    """Hem işletmenin kendi domaininde (/bayi/...) hem platformda (/s/<slug>/bayi/...) çalışsın."""
    def deco(fn):
        bp.add_url_rule(f"/bayi{rule}", fn.__name__ + "_host", fn, **opts)
        bp.add_url_rule(f"/s/<slug>/bayi{rule}", fn.__name__, fn, **opts)
        return fn
    return deco


def _current(biz):
    did = session.get("dealer_id")
    if not did:
        return None
    d = dealers.get(biz["id"], did)
    if not d or not d["active"]:
        session.pop("dealer_id", None)
        return None
    return d


def dealer_required(fn):
    @wraps(fn)
    def wrapped(slug=None, **kw):
        biz = _biz(slug)
        d = _current(biz)
        if not d:
            return redirect(f"{g.base}/bayi/giris")
        g.dealer = d
        return fn(biz, d, **kw)
    return wrapped


@_route("/giris", methods=["GET", "POST"])
def login(slug=None):
    biz = _biz(slug)
    if _current(biz) and request.method == "GET":
        return redirect(f"{g.base}/bayi/")
    code = ""
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()[:30]
        ip = request.remote_addr or "?"
        if rate_limited(f"bayi:{ip}", 10, 600) or rate_limited(f"bayi:{biz['id']}:{code.lower()}", 8, 600):
            flash("Çok fazla deneme yapıldı. Güvenliğin için 10 dakika sonra tekrar dener misin?", "error")
            return render_template("dealer/login.html", biz=biz, code=code), 429
        d = dealers.authenticate(biz["id"], code, request.form.get("password"))
        if d:
            keep_csrf = session.get("csrf")
            session.clear()                      # başka bir oturum (ör. panel) açıksa karışmasın
            session.permanent = True
            session["dealer_id"] = d["id"]
            if keep_csrf:
                session["csrf"] = keep_csrf
            return redirect(f"{g.base}/bayi/")
        flash("Kullanıcı kodu ya da şifre hatalı görünüyor, kontrol eder misin?", "error")
        return render_template("dealer/login.html", biz=biz, code=code), 401
    return render_template("dealer/login.html", biz=biz, code=code)


@_route("/cikis", methods=["POST"])
def logout(slug=None):
    biz = _biz(slug)
    session.clear()
    return redirect(f"{g.base}/")


def _catalog(biz, d):
    groups = core.catalog(biz["id"])
    vmap = core.variants_by_product([p["id"] for _, items in groups for p in items])
    products = {}
    for _, items in groups:
        for p in items:
            vs = vmap.get(p["id"], [])
            vlist = [{"id": v["id"], "name": v["name"], "price": str(dealers.price_for(v, d)),
                      "list": str(core.money(v["price"])),
                      "ok": bool(p["available"]) and core.in_stock(v["stock"]), "left": v["stock"]} for v in vs]
            price = min((core.money(v["price"]) for v in vlist), default=None) if vlist else dealers.price_for(p, d)
            products[p["id"]] = {
                "id": p["id"], "name": p["name"], "price": str(price), "list": str(core.money(p["price"])),
                "unit": p["unit"], "desc": p["description"],
                "img": url_for("store.image", image_id=p["image_id"]) if p["image_id"] else "",
                "ok": core.product_available(p, vs), "left": p["stock"] if p["stock"] is not None else None, "v": vlist}
            p["_ok"] = products[p["id"]]["ok"]
            p["_variants"] = vs
            p["_dprice"] = price
            p["_vprices"] = [x["price"] for x in vlist]
    return groups, products


@_route("/")
@dealer_required
def home(biz, d):
    groups, products = _catalog(biz, d)
    pay = core.payment_options(biz)
    config = {
        "orderUrl": f"{g.base}/bayi/api/order", "statusBase": f"{g.base}/o/", "dealer": True,
        "cartKey": f"bayisepet:{biz['slug']}:{d['id']}", "refKey": f"bayiref:{biz['slug']}", "minOrder": "0",
        "deliveryFee": "0", "freeOver": "0", "zones": [], "first": None,
        "open": core.subscription_ok(biz) and bool(pay),
    }
    store = db.one("SELECT * FROM businesses WHERE id = ?", (d["store_business_id"],)) if d["store_business_id"] else None
    return render_template("store.html", biz=biz, groups=groups, products=products, config=config, reward=None,
                           first_offer=None, zones=[], pay_options=pay, sub_ok=core.subscription_ok(biz),
                           dealer=d, dealer_store=store,
                           dealer_pct=dealers.pct_text(dealers.discount_pct(d)) if dealers.discount_pct(d) else "")


@_route("/api/order", methods=["POST"])
@dealer_required
def place(biz, d):
    if not request.is_json:
        abort(400)
    if rate_limited(f"bayiorder:{d['id']}", 20, 600):
        return jsonify(ok=False, error="Kısa sürede çok fazla sipariş gönderildi. Birkaç dakika sonra tekrar dener misin?"), 429
    try:
        order = core.create_order(biz, request.get_json(silent=True) or {}, dealer=d)
    except core.OrderError as e:
        return jsonify(ok=False, error=str(e)), 400
    lines = core.order_lines(order["id"])
    items = ", ".join(f"{l['qty']}× {l['name']}" + (f" ({l['unit']})" if l["unit"] else "") for l in lines[:4])
    if len(lines) > 4:
        items += f" +{len(lines) - 4} kalem"
    notify_owner(biz["id"], f"🏪 Bayi siparişi #{order['id']} · {core.tl(order['total'])}",
                 f"{d['name']}{' · ' + d['city'] if d['city'] else ''}\n{items}\nOnay bekliyor.",
                 f"/panel/orders/{order['id']}", tag=f"order{order['id']}")
    low = core.low_stock_after_sale(biz, order)
    if low:
        notify_owner(biz["id"], "📦 Stok azalıyor", "\n".join(f"{n}: {k} adet kaldı" for n, k in low),
                     "/panel/products", tag="stok")
    return jsonify(ok=True, id=order["id"], url=f"{g.base}/bayi/siparisler?yeni={order['id']}")


@_route("/siparisler")
@dealer_required
def orders(biz, d):
    rows = db.query("SELECT * FROM orders WHERE business_id = ? AND dealer_id = ? ORDER BY id DESC LIMIT 200",
                    (biz["id"], d["id"]))
    due = sum((core.payment_state(o)["remaining"] for o in rows if o["status"] in ("approved", "on_the_way", "delivered")),
              core.money("0"))
    lines = {}
    if rows:
        ids = [o["id"] for o in rows[:50]]
        marks = ",".join("?" for _ in ids)
        for ln in db.query(f"SELECT * FROM order_lines WHERE order_id IN ({marks}) ORDER BY id", ids):
            lines.setdefault(ln["order_id"], []).append(ln)
    store = db.one("SELECT * FROM businesses WHERE id = ?", (d["store_business_id"],)) if d["store_business_id"] else None
    return render_template("dealer/orders.html", biz=biz, dealer=d, rows=rows, lines=lines, due=due,
                           pay_state=core.payment_state, new_id=request.args.get("yeni", type=int), dealer_store=store)


@_route("/magazam", methods=["POST"])
@dealer_required
def my_store(biz, d):
    """Bayinin kendi mağaza paneline geçiş (işletme sahibinin açtığı yetkilerle)."""
    if not d["store_business_id"] or not d["user_id"]:
        flash("Senin için ayrı bir mağaza açılmamış. İşletme sahibiyle konuşabilirsin.", "error")
        return redirect(f"{g.base}/bayi/")
    session["uid"] = d["user_id"]
    return redirect(url_for("panel.orders"))
