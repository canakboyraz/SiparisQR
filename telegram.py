"""Telegram ile sesli sipariş bildirimi.

Neden: iPhone'da ana ekrana eklenen web uygulamalarının bildirimleri sessiz gelir (Apple kısıtı).
Telegram gerçek bir uygulama olduğu için bildirim sesi çalar; sohbete özel zil sesi de seçilebilir.

Kurulum (platform yöneticisi, bir kez):
  1) Telegram'da @BotFather -> /newbot -> bot adı ve kullanıcı adı ver -> token'ı al.
  2) Railway Variables: TELEGRAM_BOT_TOKEN=<token>, TELEGRAM_BOT_USERNAME=<botun kullanıcı adı>
  3) Yönetim -> Ayarlar -> "Telegram webhook'unu kur".
İşletme: Panel -> Bildirimler -> "Telegram'a bağla" -> Telegram açılır -> Başlat.
"""
import hashlib
import hmac
import logging
import os
import threading

import requests

import db

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot{token}/{method}"


def token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def bot_username():
    return os.environ.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")


def enabled():
    return bool(token() and bot_username())


def _secret(salt):
    return hmac.new(token().encode(), salt.encode(), hashlib.sha256).hexdigest()


def webhook_secret():
    return _secret("webhook")[:32]


def link_code(biz_id):
    """İşletmeye özel bağlantı kodu (tahmin edilemez, Telegram start parametresine uygun)."""
    return f"b{biz_id}_{_secret(f'biz:{biz_id}')[:16]}"


def biz_from_code(code):
    try:
        prefix, sig = code.split("_", 1)
        biz_id = int(prefix[1:])
    except (ValueError, AttributeError):
        return None
    return biz_id if hmac.compare_digest(link_code(biz_id), code) else None


def deep_link(biz_id):
    return f"https://t.me/{bot_username()}?start={link_code(biz_id)}"


def call(method, session=None, **params):
    r = (session or requests).post(API.format(token=token(), method=method), json=params, timeout=10)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "description": f"HTTP {r.status_code}"}


def set_webhook(base_url):
    return call("setWebhook", url=f"{base_url.rstrip('/')}/telegram/webhook/{webhook_secret()}",
                allowed_updates=["message"], drop_pending_updates=True)


def webhook_info():
    return call("getWebhookInfo")


def handle_update(update, biz_name_for):
    """Gelen mesajı işler. Döner: (chat_id, yanıt_metni) ya da None."""
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    text = (msg.get("text") or "").strip()
    chat_id = chat.get("id")
    if not chat_id or not text:
        return None
    title = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or ""
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        biz_id = biz_from_code(parts[1]) if len(parts) > 1 else None
        if not biz_id:
            return chat_id, "Merhaba! Bağlanmak için işletme panelindeki Bildirimler → \"Telegram'a bağla\" düğmesini kullanır mısın?"
        name = biz_name_for(biz_id)
        if not name:
            return chat_id, "Bu bağlantı geçersiz görünüyor. Panelden yeniden dener misin?"
        if not db.one("SELECT id FROM telegram_chats WHERE business_id = ? AND chat_id = ?", (biz_id, str(chat_id))):
            from core import now
            db.insert("telegram_chats", {"business_id": biz_id, "chat_id": str(chat_id), "title": title[:80],
                                         "created_at": now()})
            db.commit()
        return chat_id, (f"✅ Bağlandı! {name} siparişleri artık bu sohbete sesli bildirim olarak gelecek.\n\n"
                         "İpucu: Bu sohbete dokunup üstteki adı → Bildirimler → Ses ile özel bir zil sesi seçebilirsin.\n"
                         "Bildirimleri durdurmak için /durdur yaz.")
    if text in ("/durdur", "/stop"):
        db.execute("DELETE FROM telegram_chats WHERE chat_id = ?", (str(chat_id),))
        db.commit()
        return chat_id, "Bildirimler durduruldu. Tekrar açmak için panelden yeniden bağlanabilirsin."
    return chat_id, "Ben sadece sipariş bildirimi gönderiyorum. Durdurmak için /durdur yazabilirsin."


def send_to_business(business_id, text, url=None, *, background=True):
    if not enabled():
        return 0
    chats = db.query("SELECT * FROM telegram_chats WHERE business_id = ?", (business_id,))
    if not chats:
        return 0

    def run():
        dead = []
        for c in chats:
            params = {"chat_id": c["chat_id"], "text": text, "disable_web_page_preview": True}
            if url:
                params["reply_markup"] = {"inline_keyboard": [[{"text": "Siparişi aç", "url": url}]]}
            try:
                res = call("sendMessage", **params)
                desc = str(res.get("description", "")).lower()
                if not res.get("ok") and (res.get("error_code") == 403 or "chat not found" in desc):
                    dead.append(c["id"])        # bot engellendi ya da sohbet silindi
                elif not res.get("ok") and params.get("reply_markup"):
                    params.pop("reply_markup")  # buton linki kabul edilmediyse düz mesaj gönder
                    call("sendMessage", **params)
            except Exception as e:  # bildirim hatası siparişi bozmamalı
                log.warning("telegram gönderilemedi: %s", e)
        try:
            for cid in dead:
                db.execute("DELETE FROM telegram_chats WHERE id = ?", (cid,))
            if dead:
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
    return len(chats)
