import os
import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import core
import db
from app import current_user, login_required, rate_limited

bp = Blueprint("auth", __name__)


def _safe_next(url):
    return url if url and url.startswith("/") and not url.startswith("//") else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        ip = request.remote_addr or "?"
        if rate_limited(f"login:{ip}", 10, 600) or rate_limited(f"login:{email}", 8, 600):
            flash("Çok fazla deneme yapıldı. 10 dakika sonra tekrar dene.", "error")
            return render_template("login.html", email=email), 429
        u = db.one("SELECT * FROM users WHERE email = ?", (email,))
        if u and check_password_hash(u["password_hash"], password):
            if u["business_id"]:
                biz = db.one("SELECT active FROM businesses WHERE id = ?", (u["business_id"],))
                if not biz or not biz["active"]:
                    flash("Bu işletmenin hesabı kapalı. Platform yöneticisiyle görüş.", "error")
                    return render_template("login.html", email=email), 403
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            dest = _safe_next(request.args.get("next"))
            if not dest:
                dest = url_for("panel.orders") if u["business_id"] else url_for("admin.index")
            return redirect(dest)
        flash("E-posta ya da şifre hatalı.", "error")
        return render_template("login.html", email=email), 401
    if current_user():
        return redirect(url_for("panel.orders") if current_user()["business_id"] else url_for("admin.index"))
    return render_template("login.html", email="")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/panel/password", methods=["GET", "POST"])
@login_required
def password():
    if request.method == "POST":
        u = current_user()
        old, new, again = (request.form.get(k) or "" for k in ("old", "new", "again"))
        if not check_password_hash(u["password_hash"], old):
            flash("Mevcut şifre hatalı.", "error")
        elif len(new) < 8:
            flash("Yeni şifre en az 8 karakter olmalı.", "error")
        elif new != again:
            flash("Yeni şifreler aynı değil.", "error")
        else:
            db.update("users", u["id"], {"password_hash": generate_password_hash(new)})
            db.commit()
            flash("Şifren değişti.", "ok")
            return redirect(url_for("panel.settings"))
    return render_template("panel/password.html", active="settings")



def signup_open():
    return os.environ.get("SIGNUP_OPEN", "1") == "1"


@bp.route("/kayit", methods=["GET", "POST"])
def signup():
    if not signup_open():
        abort(404)
    form = {k: "" for k in ("name", "owner_name", "email", "phone")}
    if request.method == "POST":
        f = request.form
        form = {k: (f.get(k) or "").strip() for k in form}
        form["email"] = form["email"].lower()
        password = f.get("password") or ""
        errors = []
        if f.get("website"):                       # bot tuzağı
            abort(400)
        if rate_limited(f"signup:{request.remote_addr}", 4, 3600):
            errors.append("Çok fazla kayıt denemesi. Bir saat sonra tekrar dene.")
        if len(form["name"]) < 2:
            errors.append("İşletme adını yaz.")
        if len(form["owner_name"]) < 2:
            errors.append("Adını ve soyadını yaz.")
        if "@" not in form["email"] or "." not in form["email"].split("@")[-1]:
            errors.append("Geçerli bir e-posta yaz.")
        elif db.one("SELECT id FROM users WHERE email = ?", (form["email"],)):
            errors.append("Bu e-postayla zaten hesap var. Giriş yap ya da şifreni yöneticiden iste.")
        phone = core.normalize_phone(form["phone"])
        if not phone:
            errors.append("Cep telefonunu 05XX XXX XX XX şeklinde yaz.")
        if len(password) < 8:
            errors.append("Şifre en az 8 karakter olmalı.")
        if not f.get("kvkk"):
            errors.append("Kullanım koşullarını onayla.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("signup.html", form=form), 400
        base = core.slugify(form["name"]) or "magaza"
        if len(base) < 3:
            base = (base + "-magaza")[:40]
        slug, i = base, 2
        from routes.admin import RESERVED
        while slug in RESERVED or db.one("SELECT id FROM businesses WHERE slug = ?", (slug,)):
            slug = f"{base[:36]}-{i}"
            i += 1
        try:
            bid = db.insert("businesses", {"slug": slug, "name": form["name"][:80], "phone": phone, "whatsapp": phone,
                                           "active": 1, "plan": "trial", "paid_until": core.trial_until(),
                                           "self_signup": 1, "pay_iban": 0, "created_at": core.now()})
            uid = db.insert("users", {"business_id": bid, "email": form["email"], "name": form["owner_name"][:80],
                                      "password_hash": generate_password_hash(password), "is_admin": 0,
                                      "created_at": core.now()})
            db.commit()
        except Exception:
            db.rollback()
            raise
        session.clear()
        session.permanent = True
        session["uid"] = uid
        flash(f"Hoş geldin! {core.trial_days()} günlük ücretsiz denemen başladı. Aşağıdaki adımlarla 10 dakikada hazırsın.", "ok")
        return redirect(url_for("panel.orders"))
    return render_template("signup.html", form=form)
