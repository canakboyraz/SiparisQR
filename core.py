"""İş kuralları: para, telefon, sipariş oluşturma, onay akışı ve ödül (sadakat) hesabı.

Kurallar
- Fiyatlar her zaman sunucuda, ürünün güncel fiyatından hesaplanır; telefondan gelen fiyata güvenilmez.
- Müşteri telefon numarasıyla tanınır (işletme başına ayrı müşteri kaydı).
- Sipariş "Onay bekliyor" düşer; işletme sahibi onaylar ya da reddeder.
- Ödül (damga kartı): onaylanan sipariş damga kazandırır. Damga türü işletmece seçilir:
  order = sipariş başına 1 (en az `reward_min_total` tutarındaysa), item = her ürün adedi 1,
  amount = her `reward_amount` TL'ye 1. Damga sayısı `reward_every`'nin her katında 1 ödül yazılır.
  Onaylanmış sipariş iptal edilirse sayılmaz; o siparişle kazanılan, henüz verilmemiş ödül de silinir.
"""
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import db

TZ = ZoneInfo("Europe/Istanbul")
MAX_LINES, MAX_QTY = 40, 99

STATUS = {
    "pending": "Onay bekliyor",
    "approved": "Onaylandı",
    "on_the_way": "Yolda / hazır",
    "delivered": "Teslim edildi",
    "rejected": "Reddedildi",
    "cancelled": "İptal edildi",
}
COUNTED = ("approved", "on_the_way", "delivered")
TRANSITIONS = {
    "pending": {"approved", "rejected"},
    "approved": {"on_the_way", "delivered", "cancelled"},
    "on_the_way": {"delivered", "cancelled"},
    "delivered": set(),
    "rejected": set(),
    "cancelled": set(),
}
PAYMENT = {"iban": "IBAN / havale", "cash": "Kapıda nakit", "pos": "Kapıda kart", "card": "Kartla online"}
PAYMENT_SHORT = {"iban": ("IBAN", "havale / EFT"), "cash": ("Nakit", "kapıda"), "pos": ("Kart", "kapıda POS"),
                 "card": ("Kart", "online öde")}
FULFILMENT = {"delivery": "Adrese teslim", "pickup": "Gel al"}


PAY_METHODS = {"cash": "Nakit", "iban": "Havale / EFT", "card": "Kart", "other": "Diğer"}
PAY_STATE = {"none": "Ödenmedi", "partial": "Eksik", "paid": "Ödendi", "over": "Fazla ödendi"}
CLOSED = ("rejected", "cancelled")


class OrderError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_time(iso, fmt="%d.%m.%Y %H:%M"):
    if not iso:
        return ""
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime(fmt)


# ── Para ────────────────────────────────────────────────────────────────────

def money(value) -> Decimal:
    try:
        text = str(value if value is not None else "0").strip().replace(" ", "").replace("TL", "")
        if "," in text and "." in text:          # 1.250,50
            text = text.replace(".", "")
        elif "." in text and re.fullmatch(r"\d{1,3}(\.\d{3})+", text):   # 1.850 / 12.500 = binlik ayırıcı
            text = text.replace(".", "")
        text = text.replace(",", ".") or "0"
        d = Decimal(text).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("geçersiz tutar") from None
    if d < 0:
        raise ValueError("tutar negatif olamaz")
    return d


def tl(value) -> str:
    """85 -> '85 TL', 85.5 -> '85,50 TL', 1250 -> '1.250 TL'."""
    value = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    whole, frac = divmod(value, 1)
    text = f"{int(whole):,}".replace(",", ".")
    return f"{text},{int(frac * 100):02d} TL" if frac else f"{text} TL"


# ── Telefon / IBAN ──────────────────────────────────────────────────────────

def normalize_phone(raw) -> str:
    """Türk cep numarasını 5XXXXXXXXX biçimine getirir; geçersizse ''."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("90") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits if re.fullmatch(r"5\d{9}", digits) else ""


def pretty_phone(p) -> str:
    return f"0{p[:3]} {p[3:6]} {p[6:8]} {p[8:]}" if p and len(p) == 10 else (p or "")


def normalize_iban(raw) -> str:
    return re.sub(r"\s", "", str(raw or "")).upper()


def valid_iban(iban) -> bool:
    if not re.fullmatch(r"TR\d{24}", iban or ""):
        return False
    moved = iban[4:] + iban[:4]
    num = "".join(str(int(ch, 36)) for ch in moved)
    return int(num) % 97 == 1


def pretty_iban(iban) -> str:
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


# ── İşletme ─────────────────────────────────────────────────────────────────

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def slugify(text) -> str:
    s = str(text or "").translate(TR_MAP).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40].strip("-")


def normalize_domain(raw) -> str:
    d = str(raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def valid_domain(d) -> bool:
    return bool(re.fullmatch(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", d or ""))


def business_by_host(host):
    d = normalize_domain(host)
    if not d:
        return None
    return db.one("SELECT * FROM businesses WHERE custom_domain = ? AND active = 1", (d,))


def business_by_slug(slug):
    return db.one("SELECT * FROM businesses WHERE slug = ? AND active = 1", (slug,))


REWARD_MODES = {"order": "Sipariş başına", "item": "Ürün adedi başına", "amount": "Tutar başına"}


def reward_mode(biz):
    m = biz.get("reward_mode") or "order"
    return m if m in REWARD_MODES else "order"


def reward_rule(biz):
    """Mağazada gösterilecek kampanya metni; kampanya kapalıysa None."""
    if not biz["reward_active"] or not biz["reward_text"] or int(biz["reward_every"] or 0) < 2:
        return None
    every, gift, mode = biz["reward_every"], biz["reward_text"], reward_mode(biz)
    if mode == "item":
        return f"Her {every} ürüne {gift} hediye"
    if mode == "amount":
        return f"Her {tl(money(biz['reward_amount']))} alışverişe 1 damga, {every} damgada {gift} hediye"
    text = f"Her {every} siparişe {gift} hediye"
    mt = money(biz["reward_min_total"])
    if mt > 0:
        text += f" ({tl(mt)} ve üzeri siparişler sayılır)"
    return text


def stamps_for(biz, order):
    """Bu siparişin kazandıracağı damga sayısı (kampanya kurallarına göre)."""
    if not reward_rule(biz):
        return 0
    mode = reward_mode(biz)
    base = money(order["total"]) - money(order["delivery_fee"])
    if mode == "item":
        n = sum(int(l["qty"]) for l in order_lines(order["id"]))
    elif mode == "amount":
        per = money(biz["reward_amount"])
        n = int(base // per) if per > 0 else 0
    else:
        n = 1 if base >= money(biz["reward_min_total"]) else 0
    cap = int(biz.get("reward_max_per_order") or 0)
    return min(n, cap) if cap > 0 else n


def catalog(biz_id, only_visible=True):
    cats = db.query("SELECT * FROM categories WHERE business_id = ? ORDER BY position, id", (biz_id,))
    sql = "SELECT * FROM products WHERE business_id = ?"
    if only_visible:
        sql += " AND visible = 1"
    prods = db.query(sql + " ORDER BY position, id", (biz_id,))
    groups, by_cat = [], {}
    for p in prods:
        by_cat.setdefault(p["category_id"], []).append(p)
    for c in cats:
        if by_cat.get(c["id"]):
            groups.append((c, by_cat.pop(c["id"])))
    rest = [p for items in by_cat.values() for p in items]
    if rest:
        groups.append(({"id": 0, "name": "Diğer" if groups else "Ürünler"}, rest))
    return groups


# ── Sipariş ─────────────────────────────────────────────────────────────────

def zones(biz_id):
    return db.query("SELECT * FROM shipping_zones WHERE business_id = ? ORDER BY position, id", (biz_id,))


def delivery_fee_for(biz, subtotal, fulfilment, zone=None):
    if fulfilment != "delivery":
        return Decimal("0.00")
    fee = money(zone["fee"] if zone else biz["delivery_fee"])
    free_over = money(zone["free_over"] if zone else biz["free_delivery_over"])
    if free_over > 0 and subtotal >= free_over:
        return Decimal("0.00")
    return fee


def variants_by_product(product_ids):
    if not product_ids:
        return {}
    marks = ",".join("?" for _ in product_ids)
    out = {}
    for v in db.query(f"SELECT * FROM product_variants WHERE product_id IN ({marks}) ORDER BY position, id",
                      tuple(product_ids)):
        out.setdefault(v["product_id"], []).append(v)
    return out


def in_stock(stock, qty=1):
    return stock is None or int(stock) >= qty


def product_available(p, variants=None):
    """Satışta mı? 'Satışta' işareti + stok (seçenekliyse en az bir seçenekte stok)."""
    if not p["available"]:
        return False
    if variants:
        return any(in_stock(v["stock"]) for v in variants)
    return in_stock(p["stock"])


def get_or_create_customer(biz_id, phone, name, address):
    c = db.one("SELECT * FROM customers WHERE business_id = ? AND phone = ?", (biz_id, phone))
    if c:
        changes = {"name": name}
        if address:
            changes["address"] = address
        db.update("customers", c["id"], changes)
        return c["id"]
    cid = db.insert("customers", {"business_id": biz_id, "phone": phone, "name": name,
                                  "address": address, "created_at": now()})
    db.update("customers", cid, {"ref_code": ref_code_for(cid)})
    return cid


REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def ref_code_for(customer_id):
    """Müşteriye özel kısa davet kodu (ör. K7P2X). Sırası tahmin edilemesin diye karıştırılır."""
    n = (int(customer_id) * 2654435761 + 97) % (32 ** 5)
    code = ""
    for _ in range(5):
        n, r = divmod(n, 32)
        code += REF_ALPHABET[r]
    return code


def customer_ref_code(customer):
    if customer.get("ref_code"):
        return customer["ref_code"]
    code = ref_code_for(customer["id"])
    db.update("customers", customer["id"], {"ref_code": code})
    db.commit()
    return code


def prior_orders(biz_id, phone):
    r = db.one("SELECT COUNT(*) AS n FROM orders WHERE business_id = ? AND customer_phone = ? "
               "AND status NOT IN ('rejected', 'cancelled')", (biz_id, phone))
    return int(r["n"])


def first_order_offer(biz):
    """Mağazada gösterilecek ilk sipariş indirimi metni ya da None."""
    if not biz["first_discount_active"]:
        return None
    val = money(biz["first_discount_value"])
    if val <= 0:
        return None
    if biz["first_discount_type"] == "percent":
        return f"İlk siparişine %{int(val) if val == int(val) else val} indirim"
    return f"İlk siparişine {tl(val)} indirim"


def first_order_discount(biz, subtotal):
    if not first_order_offer(biz):
        return Decimal("0.00")
    val = money(biz["first_discount_value"])
    if biz["first_discount_type"] == "percent":
        d = (subtotal * min(val, Decimal(90)) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    else:
        d = val
    return min(d, subtotal)


def card_ready(biz):
    return bool(biz["pay_card"] and biz["paytr_merchant_id"] and biz["paytr_key_enc"] and biz["paytr_salt_enc"])


def card_payments_enabled():
    """Kartla ödeme platform genelinde kapalı; CARD_PAYMENTS=1 ile açılır."""
    import os
    return os.environ.get("CARD_PAYMENTS", "0") == "1"


def iban_ready(biz):
    return bool(valid_iban(biz["iban"]) and (biz["iban_holder"] or "").strip())


def payment_options(biz):
    """İşletmenin açtığı ödeme yöntemleri: IBAN (bilgileri tamamsa), kapıda nakit, kapıda kart.
    Online kart (PayTR) yalnızca platformda CARD_PAYMENTS=1 ise."""
    opts = []
    if biz["pay_iban"] and iban_ready(biz):
        opts.append("iban")
    if biz["pay_cash"]:
        opts.append("cash")
    if biz.get("pay_pos"):
        opts.append("pos")
    if card_payments_enabled() and card_ready(biz):
        opts.append("card")
    return opts


def create_order(biz, data):
    """data: {items:[{id, variant, qty}], name, phone, address, note, payment, fulfilment, zone, ref,
    birth_day, birth_month, marketing_ok}. Döner: order dict."""
    if not biz["ordering_open"]:
        raise OrderError(biz["closed_message"] or "Şu an sipariş alınmıyor.")
    if not subscription_ok(biz):
        raise OrderError("Bu mağaza şu an sipariş almıyor.")
    name = " ".join(str(data.get("name") or "").split())[:80]
    if len(name) < 2:
        raise OrderError("Adını ve soyadını yaz.")
    phone = normalize_phone(data.get("phone"))
    if not phone:
        raise OrderError("Telefon numaranı 05XX XXX XX XX şeklinde yaz.")

    options = payment_options(biz)
    if not options:
        raise OrderError("Bu mağaza ödeme bilgilerini henüz tamamlamadı, şu an sipariş alınamıyor.")
    payment = data.get("payment") or options[0]
    if payment not in options:
        raise OrderError("Bu ödeme yöntemi bu mağazada kullanılamıyor. Bir ödeme yöntemi seç.")
    fulfilment = data.get("fulfilment")
    allowed_ful = [k for k in FULFILMENT if biz[k]]
    if fulfilment not in allowed_ful:
        raise OrderError("Teslimat şeklini seç.")
    address = str(data.get("address") or "").strip()[:400]
    if fulfilment == "delivery" and len(address) < 10:
        raise OrderError("Teslimat adresini açık yaz (mahalle, sokak, no).")
    zone = None
    if fulfilment == "delivery":
        zs = zones(biz["id"])
        if zs:
            try:
                zid = int(data.get("zone") or 0)
            except (TypeError, ValueError):
                zid = 0
            zone = next((z for z in zs if z["id"] == zid), None)
            if not zone:
                raise OrderError("Teslimat bölgeni seç.")
    note = str(data.get("note") or "").strip()[:400]

    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        raise OrderError("Sepetin boş.")
    if len(items) > MAX_LINES:
        raise OrderError("Sepette çok fazla kalem var.")
    wanted = {}
    for it in items:
        try:
            pid, qty = int(it.get("id")), int(it.get("qty"))
            vid = int(it["variant"]) if it.get("variant") not in (None, "", 0, "0") else None
        except (TypeError, ValueError, AttributeError):
            raise OrderError("Sepet okunamadı, sayfayı yenileyip tekrar dene.") from None
        if qty < 1 or qty > MAX_QTY:
            raise OrderError(f"Bir üründen en fazla {MAX_QTY} adet sipariş verilebilir.")
        wanted[(pid, vid)] = wanted.get((pid, vid), 0) + qty

    pids = sorted({k[0] for k in wanted})
    marks = ",".join("?" for _ in pids)
    prods = {p["id"]: p for p in db.query(
        f"SELECT * FROM products WHERE business_id = ? AND id IN ({marks})", (biz["id"], *pids))}
    vmap = variants_by_product(list(prods))
    lines, subtotal = [], Decimal("0.00")
    for (pid, vid), qty in wanted.items():
        p = prods.get(pid)
        if not p or not p["visible"]:
            raise OrderError("Sepetindeki bir ürün artık satışta değil. Sayfayı yenile.")
        if not p["available"]:
            raise OrderError(f"{p['name']} şu an tükendi, sepetten çıkar.")
        vs = vmap.get(pid, [])
        if vs:
            v = next((x for x in vs if x["id"] == vid), None)
            if not v:
                raise OrderError(f"{p['name']} için bir seçenek seç (sayfayı yenile).")
            price, unit, stock = money(v["price"]), v["name"], v["stock"]
        else:
            if vid:
                raise OrderError("Sepet okunamadı, sayfayı yenileyip tekrar dene.")
            v, price, unit, stock = None, money(p["price"]), p["unit"], p["stock"]
        label = f"{p['name']} ({unit})" if v else p["name"]
        if stock is not None and int(stock) < qty:
            raise OrderError(f"{label} için stokta {int(stock)} adet kaldı." if int(stock) > 0 else f"{label} tükendi.")
        lt = (price * qty).quantize(Decimal("0.01"))
        subtotal += lt
        lines.append({"product_id": pid, "variant_id": v["id"] if v else None, "name": p["name"], "unit": unit,
                      "price": str(price), "qty": qty, "line_total": str(lt), "_tracked": stock is not None})

    min_order = money(biz["min_order"])
    if min_order > 0 and subtotal < min_order:
        raise OrderError(f"En az sipariş tutarı {tl(min_order)}. Sepetine {tl(min_order - subtotal)} daha ekle.")

    is_first = prior_orders(biz["id"], phone) == 0
    discount, discount_note = Decimal("0.00"), ""
    if is_first and first_order_offer(biz):
        discount = first_order_discount(biz, subtotal)
        discount_note = first_order_offer(biz)
    fee = delivery_fee_for(biz, subtotal, fulfilment, zone)
    total = subtotal - discount + fee

    referrer_id = None
    ref = str(data.get("ref") or "").strip().upper()[:10]
    if ref and is_first and biz["referral_active"]:
        r = db.one("SELECT id, phone FROM customers WHERE business_id = ? AND ref_code = ?", (biz["id"], ref))
        if r and r["phone"] != phone:
            referrer_id = r["id"]

    try:
        cust_id = get_or_create_customer(biz["id"], phone, name, address if fulfilment == "delivery" else "")
        extra = {}
        try:
            bd, bm = int(data.get("birth_day") or 0), int(data.get("birth_month") or 0)
            if 1 <= bd <= 31 and 1 <= bm <= 12:
                extra.update(birth_day=bd, birth_month=bm)
        except (TypeError, ValueError):
            pass
        if data.get("marketing_ok"):
            extra["marketing_ok"] = 1
        if extra:
            db.update("customers", cust_id, extra)
        # Stok ayır (yarış durumuna karşı koşullu güncelleme)
        for ln in lines:
            if not ln["_tracked"]:
                continue
            table, rid = ("product_variants", ln["variant_id"]) if ln["variant_id"] else ("products", ln["product_id"])
            if db.execute_rc(f"UPDATE {table} SET stock = stock - ? WHERE id = ? AND stock >= ?",
                             (ln["qty"], rid, ln["qty"])) != 1:
                raise OrderError(f"{ln['name']} stoğu az önce tükendi. Sepetini güncelle.")
        ts = now()
        order_id = db.insert("orders", {
            "business_id": biz["id"], "customer_id": cust_id, "token": secrets.token_urlsafe(16),
            "status": "pending", "payment": payment, "paid": 0, "fulfilment": fulfilment,
            "customer_name": name, "customer_phone": phone,
            "address": address if fulfilment == "delivery" else "", "note": note,
            "subtotal": str(subtotal), "delivery_fee": str(fee), "total": str(total),
            "discount": str(discount), "discount_note": discount_note,
            "zone_name": zone["name"] if zone else "", "referred_by": referrer_id,
            "stock_reserved": 1 if any(ln["_tracked"] for ln in lines) else 0,
            "counts_for_reward": 0, "created_at": ts, "updated_at": ts,
        })
        for ln in lines:
            ln = {k: v for k, v in ln.items() if not k.startswith("_")}
            db.insert("order_lines", {"order_id": order_id, **ln})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_order(order_id)


def release_stock(order):
    if not order.get("stock_reserved"):
        return
    for ln in order_lines(order["id"]):
        if ln["variant_id"]:
            db.execute("UPDATE product_variants SET stock = stock + ? WHERE id = ? AND stock IS NOT NULL",
                       (ln["qty"], ln["variant_id"]))
        elif ln["product_id"]:
            db.execute("UPDATE products SET stock = stock + ? WHERE id = ? AND stock IS NOT NULL",
                       (ln["qty"], ln["product_id"]))
    db.update("orders", order["id"], {"stock_reserved": 0})


def get_order(order_id):
    return db.one("SELECT * FROM orders WHERE id = ?", (order_id,))


def order_lines(order_id):
    return db.query("SELECT * FROM order_lines WHERE order_id = ? ORDER BY id", (order_id,))


# ── Sadakat ─────────────────────────────────────────────────────────────────

def counted_orders(customer_id):
    """Müşterinin toplam damgası (adı geriye dönük uyum için kaldı)."""
    r = db.one(f"SELECT COALESCE(SUM(stamps), 0) AS n FROM orders WHERE customer_id = ? AND counts_for_reward = 1 "
               f"AND status IN ({','.join('?' for _ in COUNTED)})", (customer_id, *COUNTED))
    return int(r["n"])


def loyalty_card(biz, customer_id):
    """Müşteri sayfasındaki kart: {every, filled, left, text, rewards:[bekleyen ödüller]} ya da None."""
    pending = db.query("SELECT * FROM rewards WHERE customer_id = ? AND status = 'earned' ORDER BY id",
                       (customer_id,))
    if not reward_rule(biz) and not pending:
        return None
    every = max(int(biz["reward_every"] or 0), 2)
    n = counted_orders(customer_id)
    filled = n % every
    mode = reward_mode(biz)
    return {"active": bool(reward_rule(biz)), "every": every, "filled": filled, "left": every - filled,
            "total": n, "text": biz["reward_text"], "rule": reward_rule(biz), "rewards": pending, "mode": mode,
            "unit": {"order": "sipariş", "item": "ürün", "amount": "damga"}[mode],
            "per": tl(money(biz["reward_amount"])) if mode == "amount" else ""}


def _order_qualifies(biz, order):
    return stamps_for(biz, order) > 0


KIND_LABEL = {"stamp": "Damga ödülü", "referral": "Davet ödülü", "birthday": "Doğum günü hediyesi"}


def _add_reward(biz, customer_id, order_id, text, kind):
    rid = db.insert("rewards", {"business_id": biz["id"], "customer_id": customer_id, "earned_order_id": order_id,
                                "text": text, "kind": kind, "status": "earned", "created_at": now()})
    return db.one("SELECT * FROM rewards WHERE id = ?", (rid,))


def _birthday_due(biz, customer, today=None):
    if not biz["birthday_active"] or not biz["birthday_text"] or not customer.get("birth_month"):
        return False
    today = today or datetime.now(TZ).date()
    from datetime import date
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            bday = date(year, customer["birth_month"], customer["birth_day"])
        except ValueError:                                 # 29 Şubat
            bday = date(year, customer["birth_month"], 28)
        if abs((bday - today).days) <= 7:
            given = db.one("SELECT COUNT(*) AS n FROM rewards WHERE customer_id = ? AND kind = 'birthday' "
                           "AND created_at >= ?", (customer["id"], f"{today.year}-01-01T00:00:00Z"))
            return int(given["n"]) == 0
    return False


def set_status(biz, order, new_status, reason="", message=None):
    """Durum değişikliği + ödül hesabı. Döner: (order, kazanılan_ödüller listesi)."""
    cur = order["status"]
    if new_status not in TRANSITIONS.get(cur, set()):
        raise OrderError(f"Bu sipariş '{STATUS[cur]}' durumunda; '{STATUS.get(new_status, new_status)}' yapılamaz.")
    changes = {"status": new_status, "updated_at": now()}
    if message is not None:
        changes["owner_message"] = message.strip()[:300]
    earned = []
    try:
        stamps_before = counted_orders(order["customer_id"])
        if new_status == "approved":
            changes["approved_at"] = now()
            changes["stamps"] = stamps_for(biz, order)
            changes["counts_for_reward"] = 1 if changes["stamps"] > 0 else 0
        if new_status in ("rejected", "cancelled"):
            changes["reject_reason"] = (reason or "").strip()[:300]
            release_stock(order)
        if new_status == "cancelled":
            changes["counts_for_reward"] = 0
            db.execute("DELETE FROM rewards WHERE earned_order_id = ? AND status = 'earned'", (order["id"],))
            db.execute("UPDATE rewards SET status = 'earned', given_order_id = NULL, given_at = NULL "
                       "WHERE given_order_id = ?", (order["id"],))
            if order.get("referred_by"):
                db.execute("UPDATE customers SET referred_by = NULL WHERE id = ?", (order["customer_id"],))
        db.update("orders", order["id"], changes)
        if new_status == "approved":
            if changes["counts_for_reward"]:
                every = int(biz["reward_every"])
                after = counted_orders(order["customer_id"])
                for _ in range(after // every - stamps_before // every):
                    earned.append(_add_reward(biz, order["customer_id"], order["id"], biz["reward_text"], "stamp"))
            customer = db.one("SELECT * FROM customers WHERE id = ?", (order["customer_id"],))
            if order.get("referred_by") and biz["referral_active"] and biz["referral_text"] and not customer["referred_by"]:
                db.update("customers", customer["id"], {"referred_by": order["referred_by"]})
                earned.append(_add_reward(biz, customer["id"], order["id"], biz["referral_text"], "referral"))
                _add_reward(biz, order["referred_by"], order["id"],
                            f"{biz['referral_text']} (davet ettiğin {customer['name'].split()[0]} sipariş verdi)", "referral")
            if _birthday_due(biz, customer):
                earned.append(_add_reward(biz, customer["id"], order["id"], biz["birthday_text"], "birthday"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_order(order["id"]), earned


def give_reward(biz_id, reward_id, order_id=None):
    r = db.one("SELECT * FROM rewards WHERE id = ? AND business_id = ?", (reward_id, biz_id))
    if not r:
        raise OrderError("Ödül bulunamadı.")
    if r["status"] != "earned":
        raise OrderError("Bu ödül zaten verilmiş.")
    db.update("rewards", reward_id, {"status": "given", "given_order_id": order_id, "given_at": now()})
    db.commit()


def pending_rewards(customer_id):
    return db.query("SELECT * FROM rewards WHERE customer_id = ? AND status = 'earned' ORDER BY id", (customer_id,))


# ── Ödeme takibi ────────────────────────────────────────────────────────────

def cents(value) -> int:
    return int((money(value) * 100).to_integral_value())


def from_cents(c) -> Decimal:
    return (Decimal(int(c or 0)) / 100).quantize(Decimal("0.01"))


def payment_state(order):
    """Sipariş satırından ödeme özeti: {total, paid, remaining, state, label, pct}."""
    total = cents(order["total"])
    paid = int(order.get("paid_cents") or 0)
    if paid <= 0:
        state = "none"
    elif paid < total:
        state = "partial"
    elif paid == total:
        state = "paid"
    else:
        state = "over"
    remaining = total - paid
    label = PAY_STATE[state]
    if state == "partial":
        label = f"{tl(from_cents(remaining))} eksik"
    elif state == "over":
        label = f"{tl(from_cents(-remaining))} fazla"
    return {"total": from_cents(total), "paid": from_cents(paid), "remaining": from_cents(max(remaining, 0)),
            "over": from_cents(max(-remaining, 0)), "state": state, "label": label,
            "pct": 100 if total <= 0 else min(100, round(paid * 100 / total))}


def payments(order_id):
    return db.query("SELECT * FROM payments WHERE order_id = ? ORDER BY id", (order_id,))


def _recompute_paid(order_id):
    r = db.one("SELECT COALESCE(SUM(amount_cents), 0) AS s FROM payments WHERE order_id = ?", (order_id,))
    paid = int(r["s"])
    o = get_order(order_id)
    db.update("orders", order_id, {"paid_cents": paid, "paid": 1 if paid >= cents(o["total"]) and paid > 0 else 0,
                                   "updated_at": now()})


def add_payment(biz_id, order, amount, method, note="", user_id=None):
    try:
        c = cents(amount)
    except ValueError:
        raise OrderError("Tutarı rakamla yaz (ör. 500 ya da 499,90).") from None
    if c <= 0:
        raise OrderError("Tutar 0'dan büyük olmalı.")
    if c > cents(order["total"]) * 10 + 1_000_000:
        raise OrderError("Tutar çok yüksek görünüyor, kontrol et.")
    method = {"pos": "card"}.get(method, method)
    if method not in PAY_METHODS:
        raise OrderError("Ödeme yöntemini seç.")
    try:
        db.insert("payments", {"business_id": biz_id, "order_id": order["id"], "amount_cents": c, "method": method,
                               "note": (note or "").strip()[:200], "created_by": user_id, "created_at": now()})
        _recompute_paid(order["id"])
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_order(order["id"])


def delete_payment(biz_id, payment_id):
    p = db.one("SELECT * FROM payments WHERE id = ? AND business_id = ?", (payment_id, biz_id))
    if not p:
        raise OrderError("Ödeme kaydı bulunamadı.")
    try:
        db.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
        _recompute_paid(p["order_id"])
        db.commit()
    except Exception:
        db.rollback()
        raise
    return p


# ── Abonelik ────────────────────────────────────────────────────────────────

PLANS = {"trial": "Deneme", "monthly": "Aylık", "yearly": "Yıllık", "free": "Ücretsiz (süresiz)"}


def _parse(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if iso else None


def subscription(biz):
    """{plan, label, until, days_left, ok, warn}"""
    plan = biz.get("plan") or "trial"
    if plan == "free":
        return {"plan": plan, "label": PLANS[plan], "until": None, "days_left": None, "ok": True, "warn": False}
    until = _parse(biz.get("paid_until"))
    if until is None:                                   # eski kayıt: açılıştan itibaren deneme
        created = _parse(biz.get("created_at")) or datetime.now(timezone.utc)
        until = created + timedelta(days=trial_days())
    left = (until - datetime.now(timezone.utc)).total_seconds() / 86400
    days = int(left) if left >= 0 else -1
    return {"plan": plan, "label": PLANS.get(plan, plan), "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days_left": days, "ok": left >= -grace_days(), "warn": left < 5, "expired": left < 0}


def subscription_ok(biz):
    return subscription(biz)["ok"]


def trial_days():
    try:
        return int(db.get_setting("p_trial_days", db.get_setting("trial_days", "14")))
    except ValueError:
        return 14


def trial_until():
    return (datetime.now(timezone.utc) + timedelta(days=trial_days())).strftime("%Y-%m-%dT%H:%M:%SZ")


def grace_days():
    return 2


def extend_subscription(biz, plan, days):
    sub = subscription(biz)
    base = _parse(sub["until"]) if sub["until"] else datetime.now(timezone.utc)
    start = max(base, datetime.now(timezone.utc))
    until = (start + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.update("businesses", biz["id"], {"plan": plan, "paid_until": until})
    db.commit()
    return until


# ── Müşteriye WhatsApp mesajları ────────────────────────────────────────────

def wa_link(phone, text):
    from urllib.parse import quote
    return f"https://wa.me/90{phone}?text={quote(text)}"


def customer_messages(biz, order, url, pay=None, rewards=()):
    """Siparişin durumuna uygun hazır mesajlar: [(etiket, metin)]."""
    first = (order["customer_name"] or "").split()[0] if order["customer_name"] else ""
    hi = f"Merhaba {first}, {biz['name']} 👋"
    no = f"#{order['id']}"
    out = []
    st = order["status"]
    if st == "approved":
        txt = f"{hi}\n{no} numaralı siparişin onaylandı, hazırlanıyor."
        if order["owner_message"]:
            txt += f"\n{order['owner_message']}"
        if order["payment"] == "iban" and pay and pay["state"] in ("none", "partial"):
            txt += (f"\n\nÖdeme: {tl(pay['remaining'])}\nIBAN: {pretty_iban(biz['iban'])}\n{biz['iban_holder']}"
                    f"\nAçıklamaya 'Sipariş {order['id']}' yazmayı unutma.")
        out.append(("Onay mesajı", txt + f"\n\nSiparişini buradan takip edebilirsin: {url}"))
    elif st == "on_the_way":
        if order["fulfilment"] == "delivery":
            txt = f"{hi}\n{no} numaralı siparişin yola çıktı 🚚"
            if order.get("cargo_tracking"):
                txt += f"\nKargo: {order.get('cargo_company') or ''} {order['cargo_tracking']}".rstrip()
                if order.get("cargo_url"):
                    txt += f"\nTakip: {order['cargo_url']}"
        else:
            txt = f"{hi}\n{no} numaralı siparişin hazır, gelip alabilirsin."
            if biz["pickup_address"]:
                txt += f"\nAdres: {biz['pickup_address']}"
        out.append(("Yola çıktı / hazır", txt + f"\n\nTakip: {url}"))
    elif st == "delivered":
        out.append(("Teşekkür", f"{hi}\nSiparişin teslim edildi, afiyet olsun! Bizi tercih ettiğin için teşekkürler 🙏"
                                f"\nTekrar sipariş vermek için: {url}"))
    elif st == "rejected":
        txt = f"{hi}\nÜzgünüz, {no} numaralı siparişini bu sefer karşılayamıyoruz."
        if order["reject_reason"]:
            txt += f"\nSebep: {order['reject_reason']}"
        out.append(("Red bilgisi", txt))
    elif st == "pending":
        out.append(("Soru sor", f"{hi}\n{no} numaralı siparişinle ilgili yazıyorum: "))
    if pay and pay["state"] == "partial" and st not in ("rejected", "cancelled"):
        out.append(("Ödeme hatırlatma", f"{hi}\n{no} numaralı siparişin için {tl(pay['paid'])} ödemeni aldık, "
                                        f"kalan {tl(pay['remaining'])}.\nIBAN: {pretty_iban(biz['iban'])}\n{biz['iban_holder']}"))
    elif pay and pay["state"] == "none" and order["payment"] == "iban" and st in ("on_the_way", "delivered"):
        out.append(("Ödeme hatırlatma", f"{hi}\n{no} numaralı siparişin için {tl(pay['remaining'])} ödeme bekliyoruz."
                                        f"\nIBAN: {pretty_iban(biz['iban'])}\n{biz['iban_holder']}"))
    for r in rewards:
        out.append(("Ödül müjdesi", f"{hi}\n🎁 Tebrikler! {r['text']} kazandın. Bir sonraki siparişinde seninle olacak."))
    return out


# ── Platform ayarları (yönetici) ────────────────────────────────────────────

PLATFORM_DEFAULTS = {"trial_days": "14", "price_monthly": "399", "price_yearly": "3990", "pay_iban": "",
                     "pay_holder": "", "pay_bank": "", "contact_whatsapp": "", "contact_email": ""}


def platform_settings():
    return {k: db.get_setting("p_" + k, db.get_setting(k, v) if k == "trial_days" else v)
            for k, v in PLATFORM_DEFAULTS.items()}


def save_platform_settings(values):
    for k, v in values.items():
        if k in PLATFORM_DEFAULTS:
            db.set_setting("p_" + k, str(v))
            if k == "trial_days":
                db.set_setting("trial_days", str(v))
    db.commit()


def setup_steps(biz):
    """Yeni işletme için kurulum kontrol listesi: [(etiket, tamam_mı, link)]."""
    n_products = db.one("SELECT COUNT(*) AS n FROM products WHERE business_id = ?", (biz["id"],))["n"]
    n_push = db.one("SELECT COUNT(*) AS n FROM push_subscriptions WHERE business_id = ?", (biz["id"],))["n"]
    n_orders = db.one("SELECT COUNT(*) AS n FROM orders WHERE business_id = ?", (biz["id"],))["n"]
    pay_ok = bool(payment_options(biz))
    camp = biz["reward_active"] or biz["first_discount_active"] or biz["referral_active"] or biz["birthday_active"]
    return [
        ("İlk ürününü ekle", int(n_products) > 0, "/panel/products/new"),
        ("Ödeme, açıklama ve teslimat ayarları", bool(pay_ok and (biz["phone"] or biz["whatsapp"])), "/panel/settings"),
        ("Bir kampanya aç (ör. her 6 siparişe hediye)", bool(camp), "/panel/campaign"),
        ("Telefonunda bildirimleri aç", int(n_push) > 0, "/panel/notifications"),
        ("Broşürünü yazdır, linki Instagram'a ekle", int(n_orders) > 0, "/panel/qr"),
    ]
