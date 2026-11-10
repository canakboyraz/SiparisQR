"""İş kuralları: para, telefon, sipariş oluşturma, onay akışı ve ödül (sadakat) hesabı.

Kurallar
- Fiyatlar her zaman sunucuda, ürünün güncel fiyatından hesaplanır; telefondan gelen fiyata güvenilmez.
- Müşteri telefon numarasıyla tanınır (işletme başına ayrı müşteri kaydı).
- Sipariş "Onay bekliyor" düşer; işletme sahibi onaylar ya da reddeder.
- Ödül: kampanya açıksa, onaylanan ve en az `reward_min_total` tutarındaki her sipariş 1 sayılır.
  Sayılan sipariş sayısı `reward_every`'nin katına ulaşınca müşteriye ödül yazılır.
  Onaylanmış sipariş iptal edilirse sayılmaz; o siparişle kazanılan, henüz verilmemiş ödül de silinir.
"""
import re
import secrets
from datetime import datetime, timezone
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
PAYMENT = {"cash": "Nakit (teslimde)", "iban": "IBAN / havale"}
FULFILMENT = {"delivery": "Adrese teslim", "pickup": "Gel al"}


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


def reward_rule(biz):
    """Mağazada gösterilecek kampanya metni; kampanya kapalıysa None."""
    if not biz["reward_active"] or not biz["reward_text"] or int(biz["reward_every"] or 0) < 2:
        return None
    text = f"Her {biz['reward_every']} siparişe {biz['reward_text']} hediye"
    mt = money(biz["reward_min_total"])
    if mt > 0:
        text += f" ({tl(mt)} ve üzeri siparişler sayılır)"
    return text


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

def delivery_fee_for(biz, subtotal, fulfilment):
    if fulfilment != "delivery":
        return Decimal("0.00")
    fee = money(biz["delivery_fee"])
    free_over = money(biz["free_delivery_over"])
    if free_over > 0 and subtotal >= free_over:
        return Decimal("0.00")
    return fee


def get_or_create_customer(biz_id, phone, name, address):
    c = db.one("SELECT * FROM customers WHERE business_id = ? AND phone = ?", (biz_id, phone))
    if c:
        changes = {"name": name}
        if address:
            changes["address"] = address
        db.update("customers", c["id"], changes)
        return c["id"]
    return db.insert("customers", {"business_id": biz_id, "phone": phone, "name": name,
                                   "address": address, "created_at": now()})


def create_order(biz, data):
    """data: {items:[{id, qty}], name, phone, address, note, payment, fulfilment}. Döner: order dict."""
    if not biz["ordering_open"]:
        raise OrderError(biz["closed_message"] or "Şu an sipariş alınmıyor.")
    name = " ".join(str(data.get("name") or "").split())[:80]
    if len(name) < 2:
        raise OrderError("Adını ve soyadını yaz.")
    phone = normalize_phone(data.get("phone"))
    if not phone:
        raise OrderError("Telefon numaranı 05XX XXX XX XX şeklinde yaz.")

    payment = data.get("payment")
    allowed_pay = [k for k in PAYMENT if biz["pay_" + k]]
    if payment not in allowed_pay:
        raise OrderError("Bir ödeme yöntemi seç.")
    fulfilment = data.get("fulfilment")
    allowed_ful = [k for k in FULFILMENT if biz[k]]
    if fulfilment not in allowed_ful:
        raise OrderError("Teslimat şeklini seç.")
    address = str(data.get("address") or "").strip()[:400]
    if fulfilment == "delivery" and len(address) < 10:
        raise OrderError("Teslimat adresini açık yaz (mahalle, sokak, no).")
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
        except (TypeError, ValueError, AttributeError):
            raise OrderError("Sepet okunamadı, sayfayı yenileyip tekrar dene.") from None
        if qty < 1 or qty > MAX_QTY:
            raise OrderError(f"Bir üründen en fazla {MAX_QTY} adet sipariş verilebilir.")
        wanted[pid] = wanted.get(pid, 0) + qty

    marks = ",".join("?" for _ in wanted)
    prods = {p["id"]: p for p in db.query(
        f"SELECT * FROM products WHERE business_id = ? AND id IN ({marks})", (biz["id"], *wanted))}
    lines, subtotal = [], Decimal("0.00")
    for pid, qty in wanted.items():
        p = prods.get(pid)
        if not p or not p["visible"]:
            raise OrderError("Sepetindeki bir ürün artık satışta değil. Sayfayı yenile.")
        if not p["available"]:
            raise OrderError(f"{p['name']} şu an tükendi, sepetten çıkar.")
        price = money(p["price"])
        lt = (price * qty).quantize(Decimal("0.01"))
        subtotal += lt
        lines.append({"product_id": pid, "name": p["name"], "unit": p["unit"], "price": str(price),
                      "qty": qty, "line_total": str(lt)})

    min_order = money(biz["min_order"])
    if min_order > 0 and subtotal < min_order:
        raise OrderError(f"En az sipariş tutarı {tl(min_order)}. Sepetine {tl(min_order - subtotal)} daha ekle.")
    fee = delivery_fee_for(biz, subtotal, fulfilment)
    total = subtotal + fee

    try:
        cust_id = get_or_create_customer(biz["id"], phone, name, address if fulfilment == "delivery" else "")
        ts = now()
        order_id = db.insert("orders", {
            "business_id": biz["id"], "customer_id": cust_id, "token": secrets.token_urlsafe(16),
            "status": "pending", "payment": payment, "paid": 0, "fulfilment": fulfilment,
            "customer_name": name, "customer_phone": phone,
            "address": address if fulfilment == "delivery" else "", "note": note,
            "subtotal": str(subtotal), "delivery_fee": str(fee), "total": str(total),
            "counts_for_reward": 0, "created_at": ts, "updated_at": ts,
        })
        for ln in lines:
            db.insert("order_lines", {"order_id": order_id, **ln})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_order(order_id)


def get_order(order_id):
    return db.one("SELECT * FROM orders WHERE id = ?", (order_id,))


def order_lines(order_id):
    return db.query("SELECT * FROM order_lines WHERE order_id = ? ORDER BY id", (order_id,))


# ── Sadakat ─────────────────────────────────────────────────────────────────

def counted_orders(customer_id):
    r = db.one(f"SELECT COUNT(*) AS n FROM orders WHERE customer_id = ? AND counts_for_reward = 1 "
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
    return {"active": bool(reward_rule(biz)), "every": every, "filled": filled, "left": every - filled,
            "total": n, "text": biz["reward_text"], "rule": reward_rule(biz), "rewards": pending}


def _order_qualifies(biz, order):
    if not reward_rule(biz):
        return False
    return money(order["total"]) - money(order["delivery_fee"]) >= money(biz["reward_min_total"])


def set_status(biz, order, new_status, reason="", message=None):
    """Durum değişikliği + ödül hesabı. Döner: (order, kazanılan_ödül ya da None)."""
    cur = order["status"]
    if new_status not in TRANSITIONS.get(cur, set()):
        raise OrderError(f"Bu sipariş '{STATUS[cur]}' durumunda; '{STATUS.get(new_status, new_status)}' yapılamaz.")
    changes = {"status": new_status, "updated_at": now()}
    if message is not None:
        changes["owner_message"] = message.strip()[:300]
    earned = None
    try:
        if new_status == "approved":
            changes["approved_at"] = now()
            changes["counts_for_reward"] = 1 if _order_qualifies(biz, order) else 0
        if new_status == "rejected":
            changes["reject_reason"] = (reason or "").strip()[:300]
        if new_status == "cancelled":
            changes["counts_for_reward"] = 0
            changes["reject_reason"] = (reason or "").strip()[:300]
            db.execute("DELETE FROM rewards WHERE earned_order_id = ? AND status = 'earned'", (order["id"],))
            db.execute("UPDATE rewards SET status = 'earned', given_order_id = NULL, given_at = NULL "
                       "WHERE given_order_id = ?", (order["id"],))
        db.update("orders", order["id"], changes)
        if new_status == "approved" and changes["counts_for_reward"]:
            every = int(biz["reward_every"])
            n = counted_orders(order["customer_id"])
            if n > 0 and n % every == 0:
                rid = db.insert("rewards", {"business_id": biz["id"], "customer_id": order["customer_id"],
                                            "earned_order_id": order["id"], "text": biz["reward_text"],
                                            "status": "earned", "created_at": now()})
                earned = db.one("SELECT * FROM rewards WHERE id = ?", (rid,))
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
