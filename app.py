"""QR Sipariş — çok işletmeli QR / online sipariş ve sadakat sistemi.

Adresler
- İşletmenin kendi domaini (ör. zeytinci.com) -> doğrudan o işletmenin mağazası
- Platform domaini/s/<kısa-ad>             -> domaini olmayan işletmelerin mağazası
- /panel                                  -> işletme sahibi paneli (sipariş onayı, ürünler, kampanya...)
- /admin                                  -> platform yöneticisi (işletme açma, domain bağlama)
"""
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from functools import wraps

from flask import Flask, abort, g, redirect, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash

import core
import db
import webpush

log = logging.getLogger("app")


def platform_name():
    return os.environ.get("PLATFORM_NAME", "QR Sipariş")


def platform_hosts():
    raw = os.environ.get("PLATFORM_HOSTS", "")
    return {core.normalize_domain(h) for h in raw.split(",") if h.strip()}


# ── Basit hız sınırı (tek worker) ───────────────────────────────────────────

_hits = defaultdict(deque)


def rate_limited(key, limit, window):
    q, t = _hits[key], time.monotonic()
    while q and q[0] < t - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


def reset_rate_limits():
    _hits.clear()


# ── Oturum / yetki ──────────────────────────────────────────────────────────

def current_user():
    if "user" not in g:
        uid = session.get("uid")
        g.user = db.one("SELECT * FROM users WHERE id = ?", (uid,)) if uid else None
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("auth.login", next=request.path))
        g.impersonating = False
        biz_id = u["business_id"]
        if u["is_admin"] and session.get("as_biz"):
            biz_id = session["as_biz"]
            g.impersonating = True
        if not biz_id:
            if u["is_admin"]:
                return redirect(url_for("admin.index"))
            abort(403)
        biz = db.one("SELECT * FROM businesses WHERE id = ?", (biz_id,))
        if not biz:
            session.pop("as_biz", None)
            abort(404)
        if not biz["active"] and not g.impersonating:
            session.clear()
            abort(403, "Bu işletmenin hesabı kapalı.")
        g.biz = biz
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("auth.login", next=request.path))
        if not u["is_admin"]:
            abort(403)
        return view(*a, **kw)
    return wrapped


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def store_base(biz):
    """Mağaza linklerinin başı: kendi domainindeysek '', değilse '/s/<slug>'."""
    host_biz = getattr(g, "host_biz", None)
    if host_biz and host_biz["id"] == biz["id"]:
        return ""
    return f"/s/{biz['slug']}"


def public_store_url(biz):
    """QR'a basılacak tam adres."""
    if biz["custom_domain"]:
        return f"https://{biz['custom_domain']}/"
    return f"{public_base()}/s/{biz['slug']}/"


def public_base():
    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or request.host_url.rstrip("/")


def public_order_url(biz, order):
    if biz["custom_domain"]:
        return f"https://{biz['custom_domain']}/o/{order['token']}"
    return f"{public_base()}/s/{biz['slug']}/o/{order['token']}"


def push_subject():
    return "mailto:" + os.environ.get("PUSH_CONTACT_EMAIL", os.environ.get("ADMIN_EMAIL", "admin@example.com"))


def _secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    stored = db.get_setting("secret_key")
    if not stored:
        stored = secrets.token_urlsafe(48)
        db.set_setting("secret_key", stored)
        db.commit()
    return stored


def _bootstrap_admin():
    email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD") or ""
    if not email or not password:
        return
    u = db.one("SELECT * FROM users WHERE email = ?", (email,))
    if not u:
        db.insert("users", {"business_id": None, "email": email, "name": "Yönetici",
                            "password_hash": generate_password_hash(password), "is_admin": 1,
                            "created_at": core.now()})
        db.commit()
    elif not u["is_admin"]:
        db.update("users", u["id"], {"is_admin": 1})
        db.commit()


def create_app(test_config=None):
    app = Flask(__name__)
    if test_config:
        for k, v in test_config.items():
            if k == "DATABASE_URL":
                os.environ["DATABASE_URL"] = v
    db.init()
    app.config.update(
        SECRET_KEY=_secret_key(),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1" if os.environ.get("RAILWAY_ENVIRONMENT") else "0") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    )
    if test_config:
        app.config.update({k: v for k, v in test_config.items() if k != "DATABASE_URL"})
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    _bootstrap_admin()
    webpush.vapid_private_key()

    @app.before_request
    def resolve_host():
        host = core.normalize_domain(request.host)
        g.host_biz = None
        if host not in platform_hosts():
            g.host_biz = core.business_by_host(host)
        if request.method == "POST" and request.path.startswith(("/panel", "/admin", "/login", "/logout")):
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
            if not sent or sent != session.get("csrf"):
                abort(400, "Oturum süresi doldu, sayfayı yenileyip tekrar dene.")

    @app.teardown_appcontext
    def _end(exc):
        db.rollback()   # okuma sorgularının açık bıraktığı işlemi kapat (PostgreSQL)

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith(("/panel", "/admin", "/login")):
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.context_processor
    def inject():
        return {"csrf_token": csrf_token, "tl": core.tl, "local_time": core.local_time,
                "pretty_phone": core.pretty_phone, "pretty_iban": core.pretty_iban,
                "STATUS": core.STATUS, "PAYMENT": core.PAYMENT, "FULFILMENT": core.FULFILMENT,
                "platform_name": platform_name(), "current_user": current_user}

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def error(e):
        from flask import render_template
        return render_template("error.html", code=e.code, message=getattr(e, "description", "")), e.code

    @app.errorhandler(413)
    def too_big(e):
        from flask import render_template
        return render_template("error.html", code=413, message="Dosya çok büyük (en fazla 12 MB)."), 413

    from routes import admin, auth, panel, store
    app.register_blueprint(auth.bp)
    app.register_blueprint(panel.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(store.bp)

    @app.get("/health")
    def health():
        db.one("SELECT 1 AS ok")
        return {"ok": True}

    return app
