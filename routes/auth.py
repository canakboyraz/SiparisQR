from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

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
