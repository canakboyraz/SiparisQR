"""Bayiler: işletmenin kendi belirlediği kullanıcı kodu + şifreyle giren, bayi fiyatından sipariş veren
ve isterse kendi adına satış yaptığı ayrı bir mağazası olan kişiler.

- Bayi fiyatı ürün (ve seçenek) bazında girilir; boşsa normal fiyat kullanılır.
  Üstüne bayiye özel % indirim uygulanır.
- Bayi siparişlerinde damga / ödül / ilk sipariş indirimi / davet / teslimat ücreti yoktur.
- Bayinin kendi mağazası: işletme kataloğunun kopyasıyla açılan ayrı bir mağaza (parent_id = ana işletme).
  Bayi oraya, işletme sahibinin seçtiği yetkilerle (ürünler, kampanya, ayarlar ...) sınırlı bir panelle girer.
"""
import re
import secrets
from decimal import Decimal, ROUND_HALF_UP

from werkzeug.security import check_password_hash, generate_password_hash

import core
import db

CODE_RE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")

# Bayinin kendi mağaza panelinde açılabilecek bölümler: anahtar -> (etiket, açıklama, panel uç noktaları)
PERMS = {
    "products": ("Ürün ekleme / düzenleme", "Ürün, fiyat, fotoğraf, kategori ve stok", (
        "panel.products", "panel.product_form", "panel.product_stock", "panel.product_toggle",
        "panel.product_delete", "panel.product_move", "panel.categories", "panel.stock_quick")),
    "campaign": ("Kampanya ve duyuru", "Damga kartı, ilk sipariş indirimi, davet, WhatsApp duyurusu", (
        "panel.campaign", "panel.broadcast")),
    "customers": ("Müşteriler", "Müşteri listesi ve sadakat kartları", ("panel.customers",)),
    "payments": ("Ödeme takibi", "Tahsilat listesi ve kalan borçlar", ("panel.payments_page",)),
    "reports": ("Rapor", "Ciro, en çok satanlar, stok durumu", ("panel.reports",)),
    "settings": ("Mağaza ayarları", "Mağaza bilgisi, IBAN, ödeme ve teslimat, bölgeler", (
        "panel.settings", "panel.zones")),
    "qr": ("QR kod ve broşür", "Mağaza QR'ı, baskı ve broşür", (
        "panel.qr", "panel.flyer", "panel.qr_download", "panel.qr_print")),
}

# Bayi panelinde her zaman açık olanlar (kendi siparişlerini yönetmek için)
ALWAYS = {
    "panel.index", "panel.orders", "panel.poll", "panel.order", "panel.order_status", "panel.payment_add",
    "panel.payment_delete", "panel.order_note", "panel.order_message", "panel.order_cargo", "panel.give_reward",
    "panel.order_delete", "panel.orders_cleanup", "panel.notifications", "panel.push_subscribe",
    "panel.push_unsubscribe", "panel.push_test", "panel.manifest",
}

# Panel menüsündeki anahtar -> gereken yetki
NAV_PERM = {"payments": "payments", "products": "products", "campaign": "campaign", "customers": "customers",
            "settings": "settings", "reports": "reports", "qr": "qr"}


class DealerError(ValueError):
    pass


def perms_of(raw):
    return [p for p in (raw or "").split(",") if p in PERMS]


def endpoint_allowed(perms_raw, endpoint):
    """users.perms doluysa (bayi kullanıcısı) bu uç noktaya girebilir mi?"""
    if endpoint in ALWAYS:
        return True
    for key in perms_of(perms_raw):
        if endpoint in PERMS[key][2]:
            return True
    return False


# ── Fiyat ───────────────────────────────────────────────────────────────────

def discount_pct(dealer):
    try:
        d = Decimal(str(dealer.get("discount") or "0").replace(",", "."))
    except Exception:
        return Decimal("0")
    return max(Decimal("0"), min(d, Decimal("90")))


def pct_text(d):
    """Decimal('10.00') -> '10', Decimal('2.5') -> '2.5' (bilimsel gösterim olmadan)."""
    t = format(Decimal(d).normalize(), "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def price_for(item, dealer):
    """item: ürün ya da seçenek satırı. Bayi fiyatı (boşsa normal fiyat) − bayiye özel % indirim."""
    raw = item.get("dealer_price")
    base = core.money(raw) if raw not in (None, "") and core.money(raw) > 0 else core.money(item["price"])
    pct = discount_pct(dealer)
    if pct:
        base = (base * (Decimal("100") - pct) / Decimal("100")).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return base


def parse_dealer_price(raw, label=""):
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        v = core.money(raw)
    except ValueError:
        raise ValueError(f"{label}bayi fiyatı anlaşılamadı, kontrol eder misin? (boş = normal fiyat)") from None
    if v <= 0:
        raise ValueError(f"{label}bayi fiyatı 0'dan büyük olmalı (boş bırakırsan normal fiyat geçerli olur).")
    return str(v)


# ── Bayi kaydı ──────────────────────────────────────────────────────────────

def get(biz_id, dealer_id):
    return db.one("SELECT * FROM dealers WHERE id = ? AND business_id = ?", (dealer_id, biz_id))


def by_code(biz_id, code):
    return db.one("SELECT * FROM dealers WHERE business_id = ? AND LOWER(code) = ?", (biz_id, (code or "").strip().lower()))


def authenticate(biz_id, code, password):
    d = by_code(biz_id, code)
    if not d or not d["active"] or not check_password_hash(d["password_hash"], password or ""):
        return None
    db.update("dealers", d["id"], {"last_login": core.now()})
    db.commit()
    return d


def has_dealers(biz_id):
    return bool(db.one("SELECT id FROM dealers WHERE business_id = ? AND active = 1 LIMIT 1", (biz_id,)))


def validate(biz_id, form, dealer_id=None, new=False):
    """form: dict (name, code, password, phone, city, address, note, discount, perms list, active, store)."""
    errors = []
    name = " ".join(str(form.get("name") or "").split())[:80]
    code = str(form.get("code") or "").strip()
    if len(name) < 2:
        errors.append("Bayi adı eksik görünüyor.")
    if not CODE_RE.match(code):
        errors.append("Kullanıcı kodu 3-30 karakter olmalı; harf, rakam, nokta, tire içerebilir (ör. izmit01).")
    elif db.one("SELECT id FROM dealers WHERE business_id = ? AND LOWER(code) = ? AND id <> ?",
                (biz_id, code.lower(), dealer_id or 0)):
        errors.append("Bu kullanıcı kodu başka bir bayide kullanılıyor.")
    pw = str(form.get("password") or "")
    if new and len(pw) < 6:
        errors.append("Şifre en az 6 karakter olmalı.")
    elif pw and len(pw) < 6:
        errors.append("Yeni şifre en az 6 karakter olmalı.")
    phone = core.normalize_phone(form.get("phone"))
    if not phone:
        errors.append("Bayinin telefonu eksik ya da hatalı görünüyor (05XX XXX XX XX). Siparişler bu numarayla kaydedilir.")
    try:
        disc = Decimal(str(form.get("discount") or "0").replace(",", ".").replace("%", "").strip() or "0")
        if disc < 0 or disc > 90:
            raise ValueError
    except Exception:
        errors.append("Bayi indirimi 0 ile 90 arasında bir yüzde olmalı (ör. 5).")
        disc = Decimal("0")
    values = {"name": name, "code": code, "phone": phone or "",
              "city": str(form.get("city") or "").strip()[:60], "address": str(form.get("address") or "").strip()[:400],
              "note": str(form.get("note") or "").strip()[:400], "discount": pct_text(disc) if disc else "0",
              "active": 1 if form.get("active") else 0,
              "perms": ",".join(p for p in PERMS if p in (form.get("perms") or []))}
    if pw:
        values["password_hash"] = generate_password_hash(pw)
    return values, errors


def save(biz, values, dealer=None, want_store=False, store_name=""):
    """Bayiyi kaydeder; istenirse kendi mağazasını açar. Döner: dealer dict."""
    try:
        if dealer:
            db.update("dealers", dealer["id"], values)
            did = dealer["id"]
        else:
            did = db.insert("dealers", {"business_id": biz["id"], "created_at": core.now(), **values})
        d = db.one("SELECT * FROM dealers WHERE id = ?", (did,))
        if want_store and not d["store_business_id"]:
            _open_store(biz, d, store_name)
        d = db.one("SELECT * FROM dealers WHERE id = ?", (did,))
        if d["store_business_id"]:
            db.execute("UPDATE businesses SET active = ? WHERE id = ?", (d["active"], d["store_business_id"]))
            if d["user_id"]:
                db.update("users", d["user_id"], {"perms": d["perms"], "name": d["name"]})
            if store_name.strip():
                db.execute("UPDATE businesses SET name = ? WHERE id = ?", (store_name.strip()[:80], d["store_business_id"]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return db.one("SELECT * FROM dealers WHERE id = ?", (did,))


def _unique_slug(base):
    base = core.slugify(base)[:34].strip("-") or "bayi"
    if len(base) < 3:
        base = f"{base}-bayi"
    slug, n = base, 2
    while db.one("SELECT id FROM businesses WHERE slug = ?", (slug,)):
        slug = f"{base}-{n}"
        n += 1
    return slug


def _open_store(biz, dealer, store_name=""):
    name = (store_name or "").strip()[:80] or f"{biz['name']} · {dealer['name']}"
    bid = db.insert("businesses", {
        "slug": _unique_slug(f"{biz['slug']}-{dealer['code']}"), "name": name, "active": dealer["active"],
        "plan": "free", "parent_id": biz["id"], "dealer_id": dealer["id"],
        "tagline": biz["tagline"], "about": biz["about"], "accent": biz["accent"], "logo_id": biz["logo_id"],
        "phone": dealer["phone"], "whatsapp": dealer["phone"],
        "pay_iban": 0, "pay_cash": 1, "delivery": biz["delivery"], "pickup": biz["pickup"],
        "pickup_address": dealer["address"], "created_at": core.now()})
    uid = db.insert("users", {"business_id": bid, "email": f"bayi-{dealer['id']}-{secrets.token_hex(4)}@bayi.local",
                              "name": dealer["name"], "password_hash": generate_password_hash(secrets.token_urlsafe(32)),
                              "is_admin": 0, "perms": dealer["perms"], "dealer_id": dealer["id"],
                              "created_at": core.now()})
    db.update("dealers", dealer["id"], {"store_business_id": bid, "user_id": uid})
    sync_catalog(biz["id"], bid, commit=False)
    return bid


def sync_catalog(src_biz_id, dst_biz_id, commit=True):
    """Ana işletmenin ürünlerinden bayi mağazasında olmayanları (kategori, fotoğraf, seçenekle) ekler.
    Var olan ürünlere dokunmaz (bayi fiyatını ya da açıklamasını değiştirmiş olabilir). Döner: eklenen ürün sayısı."""
    cats = {c["name"]: c["id"] for c in db.query("SELECT id, name FROM categories WHERE business_id = ?", (dst_biz_id,))}
    src_cats = {c["id"]: c for c in db.query("SELECT * FROM categories WHERE business_id = ?", (src_biz_id,))}
    have = {r["source_id"] for r in db.query("SELECT source_id FROM products WHERE business_id = ? AND source_id IS NOT NULL",
                                             (dst_biz_id,))}
    added = 0
    for p in db.query("SELECT * FROM products WHERE business_id = ? ORDER BY position, id", (src_biz_id,)):
        if p["id"] in have:
            continue
        cid = None
        if p["category_id"] and p["category_id"] in src_cats:
            cname = src_cats[p["category_id"]]["name"]
            cid = cats.get(cname)
            if not cid:
                cid = db.insert("categories", {"business_id": dst_biz_id, "name": cname,
                                               "position": src_cats[p["category_id"]]["position"]})
                cats[cname] = cid
        npid = db.insert("products", {
            "business_id": dst_biz_id, "category_id": cid, "name": p["name"], "description": p["description"],
            "unit": p["unit"], "price": p["price"], "image_id": p["image_id"], "available": p["available"],
            "visible": p["visible"], "position": p["position"], "stock": None, "source_id": p["id"],
            "created_at": core.now()})
        for v in db.query("SELECT * FROM product_variants WHERE product_id = ? ORDER BY position, id", (p["id"],)):
            db.insert("product_variants", {"business_id": dst_biz_id, "product_id": npid, "name": v["name"],
                                           "price": v["price"], "stock": None, "position": v["position"],
                                           "source_id": v["id"]})
        added += 1
    if commit:
        db.commit()
    return added


def stats(biz_id):
    """Bayi başına: sipariş sayısı, toplam, kalan borç, son sipariş."""
    out = {}
    for o in db.query("SELECT dealer_id, total, paid_cents, status, created_at FROM orders "
                      "WHERE business_id = ? AND dealer_id IS NOT NULL", (biz_id,)):
        s = out.setdefault(o["dealer_id"], {"orders": 0, "total": Decimal("0"), "due": Decimal("0"), "pending": 0,
                                            "last": None})
        if o["status"] in ("rejected", "cancelled"):
            continue
        s["orders"] += 1
        s["total"] += core.money(o["total"])
        if o["status"] == "pending":
            s["pending"] += 1
        else:
            s["due"] += max(Decimal("0"), core.money(o["total"]) - core.from_cents(o["paid_cents"] or 0))
        if not s["last"] or o["created_at"] > s["last"]:
            s["last"] = o["created_at"]
    return out


def ensure_customer(biz_id, dealer):
    """Bayi siparişleri için müşteri kaydı (telefonla). Damga/ödül almaz; listede 'Bayi' olarak işaretlenir."""
    cid = core.get_or_create_customer(biz_id, dealer["phone"], dealer["name"], dealer["address"])
    db.update("customers", cid, {"dealer_id": dealer["id"]})
    return cid
