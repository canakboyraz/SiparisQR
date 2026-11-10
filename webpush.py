"""Web Push bildirimi (RFC 8291 şifreleme + RFC 8292 VAPID), harici kütüphane olmadan.

VAPID anahtarı ilk açılışta üretilip veritabanına yazılır; elle ayar gerekmez.
İstersen VAPID_PRIVATE_KEY ortam değişkeniyle (PEM ya da base64url ham anahtar) sabitleyebilirsin.
"""
import base64
import json
import logging
import os
import struct
import threading
import time
from urllib.parse import urlparse

import jwt
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hmac import HMAC

import db

log = logging.getLogger("webpush")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf_extract(salt, ikm):
    h = HMAC(salt, hashes.SHA256())
    h.update(ikm)
    return h.finalize()


def _hkdf_expand(prk, info, length):
    h = HMAC(prk, hashes.SHA256())
    h.update(info + b"\x01")
    return h.finalize()[:length]


def _pub_bytes(pub):
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ── VAPID anahtarı ──────────────────────────────────────────────────────────

_key_cache = {}


def vapid_private_key():
    k = _key_cache.get(db._url())
    if k:
        return k
    env = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if env:
        if "BEGIN" in env:
            k = serialization.load_pem_private_key(env.encode(), password=None)
        else:
            k = ec.derive_private_key(int.from_bytes(b64u_dec(env), "big"), ec.SECP256R1())
    else:
        stored = db.get_setting("vapid_private_pem")
        if stored:
            k = serialization.load_pem_private_key(stored.encode(), password=None)
        else:
            k = ec.generate_private_key(ec.SECP256R1())
            pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode()
            db.set_setting("vapid_private_pem", pem)
            db.commit()
    _key_cache[db._url()] = k
    return k


def vapid_public_key_b64() -> str:
    return b64u(_pub_bytes(vapid_private_key().public_key()))


def vapid_headers(endpoint, subject):
    u = urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": subject}
    pem = vapid_private_key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption())
    token = jwt.encode(claims, pem, algorithm="ES256")
    return {"Authorization": f"vapid t={token}, k={vapid_public_key_b64()}"}


# ── Şifreleme (aes128gcm) ───────────────────────────────────────────────────

def encrypt(payload: bytes, ua_public_b64: str, auth_b64: str, *, salt=None, server_key=None) -> bytes:
    ua_pub_bytes = b64u_dec(ua_public_b64)
    auth_secret = b64u_dec(auth_b64)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_bytes)
    as_priv = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub_bytes = _pub_bytes(as_priv.public_key())
    salt = salt or os.urandom(16)

    ecdh = as_priv.exchange(ec.ECDH(), ua_pub)
    prk_key = _hkdf_extract(auth_secret, ecdh)
    ikm = _hkdf_expand(prk_key, b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes, 32)
    prk = _hkdf_extract(salt, ikm)
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)

    record_size = 4096
    if len(payload) + 17 > record_size:
        raise ValueError("bildirim çok uzun")
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!L", record_size) + bytes([len(as_pub_bytes)]) + as_pub_bytes
    return header + body


def decrypt(body: bytes, ua_private, auth_b64: str) -> bytes:
    """Yalnızca testler için: tarayıcının yapacağı çözmeyi taklit eder."""
    salt, rs, idlen = body[:16], struct.unpack("!L", body[16:20])[0], body[20]
    as_pub_bytes = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]
    ua_pub_bytes = _pub_bytes(ua_private.public_key())
    as_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub_bytes)
    ecdh = ua_private.exchange(ec.ECDH(), as_pub)
    prk_key = _hkdf_extract(b64u_dec(auth_b64), ecdh)
    ikm = _hkdf_expand(prk_key, b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes, 32)
    prk = _hkdf_extract(salt, ikm)
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert rs == 4096
    return plain.rstrip(b"\x00")[:-1]


# ── Gönderim ────────────────────────────────────────────────────────────────

def send_one(sub, data: dict, subject: str, session=None):
    """Döner: HTTP durum kodu (201 başarılı; 404/410 abonelik ölmüş)."""
    body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
               "TTL": "86400", "Urgency": "high", **vapid_headers(sub["endpoint"], subject)}
    if data.get("tag"):
        headers["Topic"] = "".join(ch for ch in data["tag"] if ch.isalnum())[:32]
    r = (session or requests).post(sub["endpoint"], data=body, headers=headers, timeout=10)
    return r.status_code


def notify_business(business_id, data: dict, subject: str, *, background=True):
    """İşletmenin tüm kayıtlı cihazlarına bildirim gönderir. Ölü abonelikleri siler."""
    subs = db.query("SELECT * FROM push_subscriptions WHERE business_id = ?", (business_id,))
    if not subs:
        return 0
    def run():
        dead = []
        for s in subs:
            try:
                code = send_one(s, data, subject)
                if code in (404, 410):
                    dead.append(s["id"])
                elif code >= 400:
                    log.warning("push %s -> %s", s["endpoint"][:60], code)
            except Exception as e:  # ağ hatası siparişi asla bozmamalı
                log.warning("push gönderilemedi: %s", e)
        try:
            if dead:
                for sid in dead:
                    db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sid,))
                db.commit()
        except Exception:
            db.rollback()
        finally:
            if background:
                db.close()

    if background:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()
    return len(subs)
