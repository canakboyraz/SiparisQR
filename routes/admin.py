"""Platform yöneticisi: işletme açma, domain bağlama, hesabı açıp kapatma, şifre sıfırlama."""
import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

import core
import db
from app import admin_required, platform_hosts

bp = Blueprint("admin", __name__, url_prefix="/admin")

RESERVED = {"admin", "panel", "login", "logout", "api", "static", "img", "s", "o", "health", "www"}


def _validate(name, slug, domain, biz_id=None):
    errors = []
    if len(name) < 2:
        errors.append("İşletme adı eksik görünüyor.")
    if not core.SLUG_RE.match(slug) or slug in RESERVED:
        errors.append("Kısa ad 3-40 karakter olmalı ve yalnızca küçük harf, rakam ve tire içermeli (ör. ege-zeytin).")
    elif db.one("SELECT id FROM businesses WHERE slug = ? AND id <> ?", (slug, biz_id or 0)):
        errors.append("Bu kısa ad başka işletmede kullanılıyor.")
    if domain:
        if not core.valid_domain(domain):
            errors.append("Domain hatalı görünüyor, kontrol eder misin? (ör. egezeytin.com)")
        elif domain in platform_hosts():
            errors.append("Platformun kendi domaini bir işletmeye bağlanamaz.")
        elif db.one("SELECT id FROM businesses WHERE custom_domain = ? AND id <> ?", (domain, biz_id or 0)):
            errors.append("Bu domain başka işletmeye bağlı.")
    return errors


@bp.get("/")
@admin_required
def index():
    rows = db.query(
        "SELECT b.*, (SELECT COUNT(*) FROM orders o WHERE o.business_id = b.id) AS orders, "
        "(SELECT COUNT(*) FROM orders o WHERE o.business_id = b.id AND o.status = 'pending') AS pending, "
        "(SELECT email FROM users u WHERE u.business_id = b.id ORDER BY u.id LIMIT 1) AS owner_email "
        "FROM businesses b ORDER BY b.id DESC")
    for r in rows:
        r["sub"] = core.subscription(r)
    demo = db.one("SELECT id FROM businesses WHERE is_demo = 1 ORDER BY id LIMIT 1")
    return render_template("admin/index.html", rows=rows, demo=demo, now=core.now())


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new():
    form = {"name": "", "slug": "", "domain": "", "owner_name": "", "email": ""}
    password = None
    if request.method == "POST":
        f = request.form
        form = {"name": (f.get("name") or "").strip()[:80],
                "slug": (f.get("slug") or "").strip().lower() or core.slugify(f.get("name")),
                "domain": core.normalize_domain(f.get("domain")),
                "owner_name": (f.get("owner_name") or "").strip()[:80],
                "email": (f.get("email") or "").strip().lower()}
        errors = _validate(form["name"], form["slug"], form["domain"])
        if "@" not in form["email"] or "." not in form["email"].split("@")[-1]:
            errors.append("İşletme sahibinin e-postası eksik ya da hatalı görünüyor.")
        elif db.one("SELECT id FROM users WHERE email = ?", (form["email"],)):
            errors.append("Bu e-postayla zaten bir hesap var.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            password = (f.get("password") or "").strip() or secrets.token_urlsafe(7)
            try:
                biz_id = db.insert("businesses", {"slug": form["slug"], "name": form["name"],
                                                  "custom_domain": form["domain"] or None, "active": 1,
                                                  "plan": "trial", "paid_until": core.trial_until(), "pay_iban": 1, "pay_cash": 0,
                                                  "created_at": core.now()})
                db.insert("users", {"business_id": biz_id, "email": form["email"], "name": form["owner_name"],
                                    "password_hash": generate_password_hash(password), "is_admin": 0,
                                    "created_at": core.now()})
                db.commit()
            except Exception:
                db.rollback()
                raise
            return render_template("admin/created.html", biz=core.business_by_slug(form["slug"]),
                                   email=form["email"], password=password)
    return render_template("admin/new.html", form=form)


@bp.route("/<int:biz_id>", methods=["GET", "POST"])
@admin_required
def edit(biz_id):
    b = db.one("SELECT * FROM businesses WHERE id = ?", (biz_id,))
    if not b:
        abort(404)
    owner = db.one("SELECT * FROM users WHERE business_id = ? ORDER BY id LIMIT 1", (biz_id,))
    new_password = None
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "save":
            name = (f.get("name") or "").strip()[:80]
            slug = (f.get("slug") or "").strip().lower()
            domain = core.normalize_domain(f.get("domain"))
            errors = _validate(name, slug, domain, biz_id)
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                db.update("businesses", biz_id, {"name": name, "slug": slug, "custom_domain": domain or None,
                                                 "active": 1 if f.get("active") else 0})
                db.commit()
                flash("Kaydedildi.", "ok")
                return redirect(url_for("admin.edit", biz_id=biz_id))
        elif action == "plan":
            kind = f.get("plan")
            if kind == "monthly":
                core.extend_subscription(b, "monthly", 30)
                flash("1 ay eklendi.", "ok")
            elif kind == "yearly":
                core.extend_subscription(b, "yearly", 365)
                flash("1 yıl eklendi.", "ok")
            elif kind == "trial":
                core.extend_subscription(b, "trial", 7)
                flash("Deneme süresi 7 gün uzatıldı.", "ok")
            elif kind == "free":
                db.update("businesses", biz_id, {"plan": "free"}); db.commit()
                flash("Süresiz ücretsiz yapıldı.", "ok")
            elif kind == "date":
                from datetime import datetime
                try:
                    d = datetime.strptime(f.get("until") or "", "%Y-%m-%d")
                    db.update("businesses", biz_id, {"paid_until": d.strftime("%Y-%m-%dT20:59:59Z"),
                                                     "plan": f.get("plan_kind") if f.get("plan_kind") in core.PLANS else b["plan"]})
                    db.commit()
                    flash("Bitiş tarihi güncellendi.", "ok")
                except ValueError:
                    flash("Tarih seçilmemiş görünüyor.", "error")
            return redirect(url_for("admin.edit", biz_id=biz_id) + "#abonelik")
        elif action == "reset" and owner:
            new_password = secrets.token_urlsafe(7)
            db.update("users", owner["id"], {"password_hash": generate_password_hash(new_password)})
            db.commit()
        b = db.one("SELECT * FROM businesses WHERE id = ?", (biz_id,))
    return render_template("admin/edit.html", b=b, owner=owner, new_password=new_password, sub=core.subscription(b),
                           PLANS=core.PLANS)


@bp.post("/<int:biz_id>/enter")
@admin_required
def enter(biz_id):
    if not db.one("SELECT id FROM businesses WHERE id = ?", (biz_id,)):
        abort(404)
    session["as_biz"] = biz_id
    return redirect(url_for("panel.orders"))


@bp.post("/exit")
@admin_required
def exit_panel():
    biz_id = session.pop("as_biz", None)
    return redirect(url_for("admin.edit", biz_id=biz_id) if biz_id else url_for("admin.index"))


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def platform():
    if request.method == "POST":
        f = request.form
        values = {k: (f.get(k) or "").strip()[:200] for k in core.PLATFORM_DEFAULTS}
        try:
            values["trial_days"] = str(max(0, min(90, int(values["trial_days"] or 14))))
            for k in ("price_monthly", "price_yearly"):
                values[k] = str(core.money(values[k] or "0")).replace(".00", "")
        except ValueError:
            flash("Gün ve fiyatlar anlaşılamadı, rakamla yazar mısın?", "error")
            return redirect(url_for("admin.platform"))
        values["pay_iban"] = core.normalize_iban(values["pay_iban"])
        if values["pay_iban"] and not core.valid_iban(values["pay_iban"]):
            flash("IBAN hatalı görünüyor, kontrol eder misin?", "error")
            return redirect(url_for("admin.platform"))
        values["contact_whatsapp"] = core.normalize_phone(values["contact_whatsapp"])
        core.save_platform_settings(values)
        flash("Platform ayarları kaydedildi.", "ok")
        return redirect(url_for("admin.platform"))
    return render_template("admin/settings.html", s=core.platform_settings())


@bp.post("/demo")
@admin_required
def make_demo():
    import demo
    existing = db.one("SELECT id FROM businesses WHERE is_demo = 1 ORDER BY id LIMIT 1")
    if existing:
        flash("Demo mağaza zaten var.", "ok")
        return redirect(url_for("admin.edit", biz_id=existing["id"]))
    slug = "ornek-magaza" if not db.one("SELECT id FROM businesses WHERE slug = 'ornek-magaza'") else "ornek-magaza-2"
    bid = demo.create_demo(slug=slug, name="Ege Zeytin (Örnek)", owner_email=None, is_demo=1)
    flash("Demo mağaza hazır. Satış görüşmesinde bu linki açabilirsin.", "ok")
    return redirect(url_for("admin.edit", biz_id=bid))
