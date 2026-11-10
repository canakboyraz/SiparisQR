"""PayTR iFrame API ile kartla ödeme. Kart bilgisi bu uygulamadan geçmez.

Her işletme kendi PayTR mağaza bilgilerini girer; anahtar ve salt veritabanına şifreli yazılır.
Akış: sipariş (payment=card) -> /pay/<token> -> PayTR iFrame -> PayTR /paytr/callback'e sonucu yollar
-> imza doğrulanır -> siparişe "Kart" ödemesi eklenir.
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

log = logging.getLogger("paytr")
TOKEN_URL = "https://www.paytr.com/odeme/api/get-token"


def _fernet():
    key = hashlib.sha256(("paytr:" + current_app.config["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(text):
    return _fernet().encrypt(text.encode()).decode() if text else ""


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def creds(biz):
    return {"id": biz["paytr_merchant_id"], "key": decrypt(biz["paytr_key_enc"]),
            "salt": decrypt(biz["paytr_salt_enc"]), "test": "1" if biz["paytr_test"] else "0"}


def _hmac(value, key):
    return base64.b64encode(hmac.new(key.encode(), value.encode(), hashlib.sha256).digest()).decode()


def merchant_oid():
    return "QR" + secrets.token_hex(16)          # PayTR: yalnızca harf/rakam


def build_payload(biz, order, lines, oid, ip, ok_url, fail_url):
    c = creds(biz)
    if not (c["id"] and c["key"] and c["salt"]):
        raise ValueError("PayTR bilgileri eksik.")
    from core import cents
    amount = str(cents(order["total"]) - int(order["paid_cents"] or 0))
    basket_items = [[f"{l['name']} {l['unit']}".strip()[:100], l["price"], int(l["qty"])] for l in lines]
    if order["delivery_fee"] and float(order["delivery_fee"]) > 0:
        basket_items.append(["Teslimat", order["delivery_fee"], 1])
    basket = base64.b64encode(json.dumps(basket_items, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    email = f"{order['customer_phone']}@musteri.siparisqr.com"
    payload = {
        "merchant_id": c["id"], "user_ip": (ip or "0.0.0.0")[:39], "merchant_oid": oid, "email": email,
        "payment_amount": amount, "user_basket": basket, "no_installment": "0", "max_installment": "0",
        "user_name": order["customer_name"][:60], "user_address": (order["address"] or biz["pickup_address"] or "Gel al")[:400],
        "user_phone": "0" + order["customer_phone"], "currency": "TL",
        "merchant_ok_url": ok_url, "merchant_fail_url": fail_url, "timeout_limit": "30",
        "debug_on": c["test"], "test_mode": c["test"], "lang": "tr",
    }
    sign = (payload["merchant_id"] + payload["user_ip"] + oid + email + amount + basket +
            payload["no_installment"] + payload["max_installment"] + payload["currency"] + payload["test_mode"] + c["salt"])
    payload["paytr_token"] = _hmac(sign, c["key"])
    return payload


def get_token(payload, session=None):
    r = (session or requests).post(TOKEN_URL, data=payload, timeout=(5, 20))
    try:
        data = r.json()
    except ValueError:
        raise ValueError(f"PayTR geçersiz yanıt (HTTP {r.status_code})") from None
    if data.get("status") != "success" or not data.get("token"):
        raise ValueError(str(data.get("reason") or data.get("message") or f"HTTP {r.status_code}")[:300])
    return data["token"]


def callback_hash(biz, oid, status, total_amount):
    c = creds(biz)
    return _hmac(oid + c["salt"] + status + total_amount, c["key"])


def verify_callback(biz, oid, status, total_amount, received):
    return hmac.compare_digest(callback_hash(biz, oid, status, total_amount), received or "")
