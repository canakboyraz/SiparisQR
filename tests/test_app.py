import io
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import db  # noqa: E402
import webpush  # noqa: E402

IBAN = "TR330006100519786457841326"   # geçerli örnek IBAN (mod 97)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@x.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpass123")
    monkeypatch.setenv("PLATFORM_HOSTS", "platform.test,localhost")
    db.close()
    from app import create_app, reset_rate_limits
    reset_rate_limits()
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path}/t.db", "TESTING": True})
    sent = []
    monkeypatch.setattr(webpush, "notify_business", lambda biz_id, data, subject, **kw: sent.append((biz_id, data)) or 1)
    a.sent = sent
    yield a
    db.close()


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, email, password):
    t = csrf(client)
    return client.post("/login", data={"csrf": t, "email": email, "password": password})


def make_biz(client, name="Ege Zeytin", slug="ege-zeytin", domain="", email="sahip@ege.com"):
    login(client, "admin@x.com", "adminpass123")
    t = csrf(client, "/admin/new")
    r = client.post("/admin/new", data={"csrf": t, "name": name, "slug": slug, "domain": domain,
                                        "owner_name": "Ali", "email": email, "password": "sahip12345"})
    assert r.status_code == 200, r.get_data(as_text=True)
    client.post("/logout", data={"csrf": t})
    biz = db.one("SELECT * FROM businesses WHERE slug = ?", (slug,))
    return biz


def setup_shop(biz, **extra):
    vals = {"iban": IBAN, "iban_holder": "Ali Veli", "reward_active": 1, "reward_every": 3,
            "reward_text": "1 kg zeytin", "delivery_fee": "50.00", "free_delivery_over": "1000.00"}
    vals.update(extra)
    db.update("businesses", biz["id"], vals)
    p1 = db.insert("products", {"business_id": biz["id"], "name": "Siyah Zeytin", "unit": "1 kg", "price": "350.00",
                                "created_at": core.now()})
    p2 = db.insert("products", {"business_id": biz["id"], "name": "Zeytinyağı", "unit": "5 lt", "price": "1800.00",
                                "created_at": core.now()})
    p3 = db.insert("products", {"business_id": biz["id"], "name": "Tükenen", "price": "10", "available": 0,
                                "created_at": core.now()})
    db.commit()
    return p1, p2, p3


def order(client, base, items, **kw):
    body = {"items": items, "name": "Ayşe Yılmaz", "phone": "0532 111 22 33", "address": "Atatürk Mah. 1. Sok. No 5 İzmit",
            "payment": "iban", "fulfilment": "delivery", "note": ""}
    body.update(kw)
    return client.post(f"{base}/api/order", data=json.dumps(body), content_type="application/json")


# ── Birim testleri ──────────────────────────────────────────────────────────

def test_money_and_tl():
    assert core.money("349,90") == core.money("349.90")
    assert str(core.money("1.250,50")) == "1250.50"
    assert str(core.money("1.850")) == "1850.00" and str(core.money("12.500")) == "12500.00"
    assert str(core.money("349.90")) == "349.90" and str(core.money("1.5")) == "1.50"
    assert core.tl("1250") == "1.250 TL" and core.tl("85.5") == "85,50 TL"
    with pytest.raises(ValueError):
        core.money("-5")


def test_phone_and_iban():
    assert core.normalize_phone("0532 111 22 33") == "5321112233"
    assert core.normalize_phone("+90 532 111 2233") == "5321112233"
    assert core.normalize_phone("212 111 22 33") == ""
    assert core.valid_iban(IBAN)
    assert not core.valid_iban(IBAN[:-1] + "7")
    assert core.normalize_domain("https://www.EgeZeytin.com/abc") == "egezeytin.com"


def test_webpush_encrypt_roundtrip(app):
    from cryptography.hazmat.primitives.asymmetric import ec
    ua = ec.generate_private_key(ec.SECP256R1())
    pub = webpush.b64u(webpush._pub_bytes(ua.public_key()))
    auth = webpush.b64u(os.urandom(16))
    body = webpush.encrypt('{"title":"Yeni sipariş #5"}'.encode(), pub, auth)
    assert webpush.decrypt(body, ua, auth).decode() == '{"title":"Yeni sipariş #5"}'
    h = webpush.vapid_headers("https://fcm.googleapis.com/fcm/send/abc", "mailto:a@b.com")
    assert h["Authorization"].startswith("vapid t=") and ", k=B" in h["Authorization"]
    import jwt
    token = h["Authorization"].split("t=")[1].split(",")[0]
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["aud"] == "https://fcm.googleapis.com"


def test_rfc8291_vector():
    """RFC 8291 Ek A'daki resmi test vektörüyle birebir aynı çıktı."""
    from cryptography.hazmat.primitives.asymmetric import ec
    d = webpush.b64u_dec
    as_priv = ec.derive_private_key(int.from_bytes(d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    out = webpush.encrypt(b"When I grow up, I want to be a watermelon",
                          "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                          "BTBZMqHH6r4Tts7J_aSIgg", salt=d("DGv6ra1nlYgDCS1FRnbzlw"), server_key=as_priv)
    assert webpush.b64u(out) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


# ── Akış testleri ───────────────────────────────────────────────────────────

def test_full_flow_with_reward(app):
    c = app.test_client()
    biz = make_biz(c)
    p1, p2, p3 = setup_shop(biz)

    page = c.get("/s/ege-zeytin/").get_data(as_text=True)
    assert "Siyah Zeytin" in page and "Her 3 siparişe 1 kg zeytin hediye" in page and "Tükendi" in page

    # Fiyat sunucuda hesaplanır: 2×350 + teslimat 50
    r = order(c, "/s/ege-zeytin", [{"id": p1, "qty": 2, "price": 1}])
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    o = core.get_order(data["id"])
    assert o["total"] == "750.00" and o["delivery_fee"] == "50.00" and o["status"] == "pending"
    assert app.sent and "Yeni sipariş" in app.sent[-1][1]["title"]

    st = c.get(data["url"]).get_data(as_text=True)
    assert "Onay bekliyor" in st
    assert c.get(data["url"] + ".json").get_json()["card"]["filled"] == 0

    # Ücretsiz teslimat eşiği
    r2 = order(c, "/s/ege-zeytin", [{"id": p2, "qty": 1}], payment="iban")
    o2 = core.get_order(r2.get_json()["id"])
    assert o2["delivery_fee"] == "0.00" and o2["total"] == "1800.00"
    assert IBAN[:4] in c.get(r2.get_json()["url"]).get_data(as_text=True)

    # Tükenen ürün ve boş sepet reddedilir
    assert order(c, "/s/ege-zeytin", [{"id": p3, "qty": 1}]).status_code == 400
    assert order(c, "/s/ege-zeytin", []).status_code == 400

    # Sahip girişi ve onay
    login(c, "sahip@ege.com", "sahip12345")
    lst = c.get("/panel/orders").get_data(as_text=True)
    assert f"#{o['id']}" in lst and "Ayşe Yılmaz" in lst
    t = csrf(c, f"/panel/orders/{o['id']}")
    c.post(f"/panel/orders/{o['id']}/status", data={"csrf": t, "status": "approved", "message": "Yarın kargoda"})
    assert core.get_order(o["id"])["status"] == "approved"
    j = c.get(data["url"] + ".json").get_json()
    assert j["status"] == "approved" and j["message"] == "Yarın kargoda" and j["card"]["filled"] == 1

    # Onaysız bekleyen sipariş geçersiz geçişe izin vermez
    r = c.post(f"/panel/orders/{o['id']}/status", data={"csrf": t, "status": "rejected"}, follow_redirects=True)
    assert "yapılamaz" in r.get_data(as_text=True)

    # 2. ve 3. onay -> 3. onayda ödül
    c.post(f"/panel/orders/{o2['id']}/status", data={"csrf": t, "status": "approved"})
    r3 = order(c, "/s/ege-zeytin", [{"id": p1, "qty": 1}], phone="+90 532 111 2233")
    o3 = core.get_order(r3.get_json()["id"])
    assert o3["customer_id"] == o["customer_id"]           # aynı müşteri, farklı yazım
    page = c.post(f"/panel/orders/{o3['id']}/status", data={"csrf": t, "status": "approved"},
                  follow_redirects=True).get_data(as_text=True)
    assert "ödül kazandı" in page
    rewards = core.pending_rewards(o["customer_id"])
    assert len(rewards) == 1 and rewards[0]["text"] == "1 kg zeytin"
    j = c.get(r3.get_json()["url"] + ".json").get_json()
    assert j["card"]["filled"] == 0 and j["card"]["rewards"] == ["1 kg zeytin"]

    # Ödülü yeni siparişle ver
    r4 = order(c, "/s/ege-zeytin", [{"id": p1, "qty": 1}])
    o4 = core.get_order(r4.get_json()["id"])
    detail = c.get(f"/panel/orders/{o4['id']}").get_data(as_text=True)
    assert "bekleyen ödülü var" in detail
    c.post(f"/panel/rewards/{rewards[0]['id']}/give", data={"csrf": t, "order_id": o4["id"]})
    assert core.pending_rewards(o["customer_id"]) == []

    # İptal: o3 iptal edilince sayım düşer, ama verilmiş ödül geri alınmaz (o3 ödülü kazandıran)
    c.post(f"/panel/orders/{o3['id']}/status", data={"csrf": t, "status": "cancelled"})
    assert core.counted_orders(o["customer_id"]) == 2

    # Ödeme: kalanın tamamı tek dokunuşla
    c.post(f"/panel/orders/{o2['id']}/payments", data={"csrf": t, "full": "1", "method": "iban"})
    assert core.get_order(o2["id"])["paid"] == 1
    assert c.get(r2.get_json()["url"] + ".json").get_json()["paid"] is True


def test_cancel_removes_unused_reward(app):
    c = app.test_client()
    biz = make_biz(c)
    p1, _, _ = setup_shop(biz, reward_every=2)
    biz = db.one("SELECT * FROM businesses WHERE id = ?", (biz["id"],))
    ids = [order(c, "/s/ege-zeytin", [{"id": p1, "qty": 1}]).get_json()["id"] for _ in range(2)]
    for i in ids:
        core.set_status(biz, core.get_order(i), "approved")
    cust = core.get_order(ids[0])["customer_id"]
    assert len(core.pending_rewards(cust)) == 1
    core.set_status(biz, core.get_order(ids[1]), "cancelled")
    assert core.pending_rewards(cust) == []


def test_min_total_for_reward(app):
    c = app.test_client()
    biz = make_biz(c)
    p1, p2, _ = setup_shop(biz, reward_min_total="500.00")
    biz = db.one("SELECT * FROM businesses WHERE id = ?", (biz["id"],))
    small = order(c, "/s/ege-zeytin", [{"id": p1, "qty": 1}]).get_json()["id"]     # 350 < 500
    big = order(c, "/s/ege-zeytin", [{"id": p2, "qty": 1}]).get_json()["id"]
    o, _ = core.set_status(biz, core.get_order(small), "approved")
    assert o["counts_for_reward"] == 0
    o, _ = core.set_status(biz, core.get_order(big), "approved")
    assert o["counts_for_reward"] == 1


def test_custom_domain_and_isolation(app):
    c = app.test_client()
    a = make_biz(c, "Ege Zeytin", "ege-zeytin", "egezeytin.com", "a@a.com")
    b = make_biz(c, "Bal Evi", "bal-evi", "", "b@b.com")
    pa, _, _ = setup_shop(a)
    pb = db.insert("products", {"business_id": b["id"], "name": "Çiçek Balı", "price": "400", "created_at": core.now()})
    db.commit()

    r = c.get("/", headers={"Host": "www.egezeytin.com"})
    html = r.get_data(as_text=True)
    assert "Siyah Zeytin" in html and "Çiçek Balı" not in html and '"orderUrl": "/api/order"' in html.replace("\\u002f", "/")
    assert "QR Sipariş" in c.get("/", headers={"Host": "platform.test"}).get_data(as_text=True)

    # Kendi domaininde sipariş; başka işletmenin ürünü kabul edilmez
    r = c.post("/api/order", headers={"Host": "egezeytin.com"}, content_type="application/json",
               data=json.dumps({"items": [{"id": pb, "qty": 1}], "name": "X Y", "phone": "05321112233",
                                "address": "uzun bir adres yazdım", "payment": "iban", "fulfilment": "delivery"}))
    assert r.status_code == 400
    r = c.post("/api/order", headers={"Host": "egezeytin.com"}, content_type="application/json",
               data=json.dumps({"items": [{"id": pa, "qty": 1}], "name": "X Y", "phone": "05321112233",
                                "address": "uzun bir adres yazdım", "payment": "iban", "fulfilment": "delivery"}))
    assert r.status_code == 200 and r.get_json()["url"].startswith("/o/")
    a_order = r.get_json()

    # B'nin sahibi A'nın siparişini göremez, değiştiremez
    login(c, "b@b.com", "sahip12345")
    assert c.get(f"/panel/orders/{a_order['id']}").status_code == 404
    t = csrf(c, "/panel/orders")
    assert c.post(f"/panel/orders/{a_order['id']}/status", data={"csrf": t, "status": "approved"}).status_code == 404
    assert core.get_order(a_order["id"])["status"] == "pending"
    # A'nın sipariş sayfası B'nin adresinde açılmaz
    assert c.get(f"/s/bal-evi{a_order['url']}").status_code == 404


def test_security_basics(app):
    c = app.test_client()
    make_biz(c)
    assert c.get("/panel/orders").status_code == 302
    assert c.get("/admin/").status_code == 302
    login(c, "sahip@ege.com", "sahip12345")
    assert c.get("/admin/").status_code == 403
    # CSRF yoksa POST reddedilir
    assert c.post("/panel/settings", data={"name": "x"}).status_code == 400
    # Yanlış şifre ve hız sınırı
    c2 = app.test_client()
    for _ in range(8):
        login(c2, "sahip@ege.com", "yanlis")
    assert login(c2, "sahip@ege.com", "yanlis").status_code == 429
    # JSON olmayan sipariş isteği reddedilir
    assert c.post("/s/ege-zeytin/api/order", data={"a": 1}).status_code == 400


def test_pending_limit_per_phone(app):
    c = app.test_client()
    biz = make_biz(c)
    p1, _, _ = setup_shop(biz)
    from app import reset_rate_limits
    codes = []
    for _ in range(6):
        reset_rate_limits()
        codes.append(order(c, "/s/ege-zeytin", [{"id": p1, "qty": 1}]).status_code)
    assert codes == [200] * 5 + [429]


def test_closed_shop_and_deactivated(app):
    c = app.test_client()
    biz = make_biz(c)
    p1, _, _ = setup_shop(biz, ordering_open=0, closed_message="Yarın açığız")
    assert "Yarın açığız" in c.get("/s/ege-zeytin/").get_data(as_text=True)
    r = order(c, "/s/ege-zeytin", [{"id": p1, "qty": 1}])
    assert r.status_code == 400 and "Yarın açığız" in r.get_json()["error"]
    db.update("businesses", biz["id"], {"active": 0}); db.commit()
    assert c.get("/s/ege-zeytin/").status_code == 404
    assert login(c, "sahip@ege.com", "sahip12345").status_code == 403


def test_panel_pages_render_and_settings(app):
    c = app.test_client()
    biz = make_biz(c)
    setup_shop(biz)
    login(c, "sahip@ege.com", "sahip12345")
    for path in ["/panel/orders", "/panel/orders?tab=active", "/panel/orders?tab=done", "/panel/products",
                 "/panel/products/new", "/panel/campaign", "/panel/customers", "/panel/settings", "/panel/qr",
                 "/panel/qr/print", "/panel/notifications", "/panel/password", "/panel/manifest.webmanifest"]:
        assert c.get(path).status_code == 200, path
    t = csrf(c, "/panel/settings")
    form = {"csrf": t, "name": "Ege Zeytin", "pay_cash": "1", "pay_iban": "1", "iban": "TR12 3456", "iban_holder": "A",
            "delivery": "1", "delivery_fee": "40", "free_delivery_over": "0", "min_order": "0", "ordering_open": "1"}
    assert "IBAN geçersiz" in c.post("/panel/settings", data=form, follow_redirects=True).get_data(as_text=True)
    form["iban"] = "TR33 0006 1005 1978 6457 8413 26"
    assert "Ayarlar kaydedildi" in c.post("/panel/settings", data=form, follow_redirects=True).get_data(as_text=True)
    b = db.one("SELECT * FROM businesses WHERE id = ?", (biz["id"],))
    assert b["iban"] == IBAN and b["delivery_fee"] == "40.00" and b["pickup"] == 0

    # Kampanya
    r = c.post("/panel/campaign", data={"csrf": t, "reward_active": "1", "reward_every": "8", "reward_text": "1 lt zeytinyağı",
                                        "reward_min_total": "0"}, follow_redirects=True)
    assert "Her 8 siparişe 1 lt zeytinyağı hediye" in r.get_data(as_text=True)

    # Fotoğraflı ürün ekleme
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (2400, 1600), (90, 120, 60)).save(buf, "JPEG"); buf.seek(0)
    r = c.post("/panel/products/new", data={"csrf": t, "name": "Yeşil Zeytin", "price": "299,90", "unit": "1 kg",
                                            "new_category": "Zeytinler", "available": "1", "visible": "1",
                                            "photo": (buf, "z.jpg")}, content_type="multipart/form-data", follow_redirects=True)
    assert "Ürün kaydedildi" in r.get_data(as_text=True)
    p = db.one("SELECT * FROM products WHERE name = 'Yeşil Zeytin'")
    assert p["price"] == "299.90" and p["image_id"] and p["category_id"]
    img = c.get(f"/img/{p['image_id']}")
    assert img.status_code == 200 and img.mimetype == "image/jpeg"
    assert max(Image.open(io.BytesIO(img.data)).size) <= 1100

    # Push aboneliği kaydı
    r = c.post("/panel/push/subscribe", headers={"X-CSRF-Token": t}, json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/xyz", "keys": {"p256dh": "abc", "auth": "def"}})
    assert r.get_json()["ok"] and db.one("SELECT COUNT(*) AS n FROM push_subscriptions")["n"] == 1


def test_admin_validation(app):
    c = app.test_client()
    make_biz(c)
    login(c, "admin@x.com", "adminpass123")
    t = csrf(c, "/admin/new")
    r = c.post("/admin/new", data={"csrf": t, "name": "Başka", "slug": "ege-zeytin", "email": "z@z.com"})
    assert "başka işletmede" in r.get_data(as_text=True)
    r = c.post("/admin/new", data={"csrf": t, "name": "Başka", "slug": "admin", "email": "z@z.com"})
    assert "Kısa ad" in r.get_data(as_text=True)
    r = c.post("/admin/new", data={"csrf": t, "name": "Platform", "slug": "pp", "domain": "platform.test", "email": "z@z.com"})
    assert "Kısa ad" in r.get_data(as_text=True) or "Platformun" in r.get_data(as_text=True)
    assert "Ege Zeytin" in c.get("/admin/").get_data(as_text=True)


def test_payment_tracking(app):
    c = app.test_client()
    biz = make_biz(c)
    p1, p2, _ = setup_shop(biz)
    r = order(c, "/s/ege-zeytin", [{"id": p1, "qty": 2}, {"id": p2, "qty": 0 + 1}], payment="iban")   # 700+1800, teslimat yok
    oid, url = r.get_json()["id"], r.get_json()["url"]
    login(c, "sahip@ege.com", "sahip12345")
    t = csrf(c, f"/panel/orders/{oid}")
    c.post(f"/panel/orders/{oid}/status", data={"csrf": t, "status": "approved"})
    o = core.get_order(oid)
    assert core.payment_state(o)["state"] == "none" and o["total"] == "2500.00"

    # Kısmi ödeme + not
    page = c.post(f"/panel/orders/{oid}/payments", data={"csrf": t, "amount": "1.000", "method": "iban",
                                                         "note": "Kalanı cuma"}, follow_redirects=True).get_data(as_text=True)
    assert "Kalan: 1.500 TL" in page and "Kalanı cuma" in page and "1.500 TL eksik" in page
    st = core.payment_state(core.get_order(oid))
    assert st["state"] == "partial" and str(st["paid"]) == "1000.00" and st["pct"] == 40
    j = c.get(url + ".json").get_json()
    assert j["paid"] is False and j["paid_amount"] == "1.000 TL" and j["remaining"] == "1.500 TL"
    assert "1.000 TL" in c.get(url).get_data(as_text=True)

    # Ödemeler sayfası: eksik listesinde, toplam alacak doğru
    pp = c.get("/panel/payments").get_data(as_text=True)
    assert f"#{oid} ·" in pp and "1.500 TL alındı" in pp

    # Geçersiz tutarlar
    for bad in ("0", "-5", "abc", ""):
        r = c.post(f"/panel/orders/{oid}/payments", data={"csrf": t, "amount": bad, "method": "cash"}, follow_redirects=True)
        assert core.payment_state(core.get_order(oid))["state"] == "partial", bad

    # Fazla ödeme, sonra kaydı silme
    c.post(f"/panel/orders/{oid}/payments", data={"csrf": t, "amount": "1600", "method": "cash"})
    st = core.payment_state(core.get_order(oid))
    assert st["state"] == "over" and str(st["over"]) == "100.00" and core.get_order(oid)["paid"] == 1
    last = core.payments(oid)[-1]
    c.post(f"/panel/payments/{last['id']}/delete", data={"csrf": t})
    assert core.payment_state(core.get_order(oid))["state"] == "partial"

    # Kalanı tek dokunuşla
    c.post(f"/panel/orders/{oid}/payments", data={"csrf": t, "full": "1", "method": "iban"})
    o = core.get_order(oid)
    assert core.payment_state(o)["state"] == "paid" and o["paid"] == 1 and len(core.payments(oid)) == 2
    assert c.get(url + ".json").get_json()["paid"] is True
    assert f"#{oid} ·" not in c.get("/panel/payments").get_data(as_text=True)
    assert f"#{oid} ·" in c.get("/panel/payments?tab=paid").get_data(as_text=True)

    # İç not
    c.post(f"/panel/orders/{oid}/note", data={"csrf": t, "internal_note": "Ahmet Bey'in tavsiyesi"})
    assert core.get_order(oid)["internal_note"] == "Ahmet Bey'in tavsiyesi"
    assert "Ahmet Bey" not in c.get(url).get_data(as_text=True)     # müşteri görmez

    # Başka işletme bu ödemeyi silemez / ekleyemez
    c.post("/logout", data={"csrf": t})
    make_biz(c, "Bal Evi", "bal-evi", "", "b@b.com")
    login(c, "b@b.com", "sahip12345")
    t2 = csrf(c, "/panel/orders")
    assert c.post(f"/panel/orders/{oid}/payments", data={"csrf": t2, "amount": "5", "method": "cash"}).status_code == 404
    c.post(f"/panel/payments/{core.payments(oid)[0]['id']}/delete", data={"csrf": t2})
    assert len(core.payments(oid)) == 2


def test_migration_adds_columns(tmp_path, monkeypatch):
    """Yeni sütunlar olmadan oluşturulmuş eski veritabanına sütunlar eklenir; ikinci açılış da sorunsuz."""
    import sqlite3
    path = tmp_path / "old.db"
    old_schema = db.SCHEMA.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT", blob="BLOB")
    con = sqlite3.connect(path); con.executescript(old_schema); con.commit()
    assert "paid_cents" not in {r[1] for r in con.execute("PRAGMA table_info(orders)")}
    con.close()
    db.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    db._initialized.discard(f"sqlite:///{path}")
    db.init()
    db._initialized.discard(f"sqlite:///{path}")
    db.init()
    cols = {r["name"] for r in db.query("PRAGMA table_info(orders)")}
    assert {"internal_note", "paid_cents"} <= cols
    db.close()


# ── 1. aşama ────────────────────────────────────────────────────────────────

def _owner(app, **shop):
    c = app.test_client()
    biz = make_biz(c)
    ps = setup_shop(biz, **shop)
    login(c, "sahip@ege.com", "sahip12345")
    return c, biz, ps, csrf(c, "/panel/orders")


def test_variants_and_stock(app):
    c, biz, (p1, p2, _), t = _owner(app)
    # Ürünü seçenekli yap: 500 g (stok 2), 1 kg (sınırsız)
    r = c.post(f"/panel/products/{p1}", data={"csrf": t, "name": "Siyah Zeytin", "available": "1", "visible": "1",
               "v_id": ["", ""], "v_name": ["500 g", "1 kg"], "v_price": ["200", "350"], "v_stock": ["2", ""]},
               follow_redirects=True)
    assert "Ürün kaydedildi" in r.get_data(as_text=True)
    vs = core.variants_by_product([p1])[p1]
    small, big = vs[0], vs[1]
    assert small["stock"] == 2 and big["stock"] is None
    assert db.one("SELECT price FROM products WHERE id = ?", (p1,))["price"] == "200.00"     # en düşük fiyat
    page = c.get("/s/ege-zeytin/").get_data(as_text=True)
    assert "500 g · 1 kg" in page and "200 TL&#39;den" in page or "200 TL'den" in page

    c2 = app.test_client()
    # seçenek seçmeden sipariş reddedilir
    assert order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 1}]).status_code == 400
    # stoktan fazlası reddedilir
    r = order(c2, "/s/ege-zeytin", [{"id": p1, "variant": small["id"], "qty": 3}])
    assert r.status_code == 400 and "2 adet kaldı" in r.get_json()["error"]
    # geçerli sipariş: 2×500g + 1×1kg; stok ayrılır
    r = order(c2, "/s/ege-zeytin", [{"id": p1, "variant": small["id"], "qty": 2}, {"id": p1, "variant": big["id"], "qty": 1}])
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()["id"]
    o = core.get_order(oid)
    assert o["subtotal"] == "750.00" and o["stock_reserved"] == 1
    lines = core.order_lines(oid)
    assert {(l["unit"], l["qty"]) for l in lines} == {("500 g", 2), ("1 kg", 1)}
    assert core.variants_by_product([p1])[p1][0]["stock"] == 0
    assert "Tükendi" not in c2.get("/s/ege-zeytin/").get_data(as_text=True).split("Siyah Zeytin")[1][:400]  # 1 kg hâlâ var
    # stok bitti -> yeni sipariş reddedilir
    assert order(c2, "/s/ege-zeytin", [{"id": p1, "variant": small["id"], "qty": 1}]).status_code == 400
    # reddedilince stok geri gelir
    c.post(f"/panel/orders/{oid}/status", data={"csrf": t, "status": "rejected"})
    assert core.variants_by_product([p1])[p1][0]["stock"] == 2
    assert core.get_order(oid)["stock_reserved"] == 0
    # başka ürünün seçeneği kullanılamaz
    assert order(c2, "/s/ege-zeytin", [{"id": p2, "variant": small["id"], "qty": 1}]).status_code == 400


def test_simple_stock_and_cancel(app):
    c, biz, (p1, p2, _), t = _owner(app)
    c.post(f"/panel/products/{p2}", data={"csrf": t, "name": "Zeytinyağı", "price": "1800", "unit": "5 lt",
                                          "stock": "1", "available": "1", "visible": "1"})
    assert db.one("SELECT stock FROM products WHERE id = ?", (p2,))["stock"] == 1
    page = c.get("/s/ege-zeytin/").get_data(as_text=True)
    assert "Son 1 adet" in page
    c2 = app.test_client()
    oid = order(c2, "/s/ege-zeytin", [{"id": p2, "qty": 1}]).get_json()["id"]
    assert db.one("SELECT stock FROM products WHERE id = ?", (p2,))["stock"] == 0
    assert order(c2, "/s/ege-zeytin", [{"id": p2, "qty": 1}]).status_code == 400
    c.post(f"/panel/orders/{oid}/status", data={"csrf": t, "status": "approved"})
    c.post(f"/panel/orders/{oid}/status", data={"csrf": t, "status": "cancelled"})
    assert db.one("SELECT stock FROM products WHERE id = ?", (p2,))["stock"] == 1
    # geçersiz stok değeri
    r = c.post(f"/panel/products/{p2}", data={"csrf": t, "name": "Zeytinyağı", "price": "1800", "stock": "abc",
                                              "available": "1", "visible": "1"}, follow_redirects=True)
    assert "tam sayı" in r.get_data(as_text=True)


def test_whatsapp_messages(app):
    c, biz, (p1, _, _), t = _owner(app)
    oid = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], payment="iban").get_json()["id"]
    c.post(f"/panel/orders/{oid}/status", data={"csrf": t, "status": "approved", "message": "Yarın kargoda"})
    page = c.get(f"/panel/orders/{oid}").get_data(as_text=True)
    assert "Müşteriye haber ver" in page and "https://wa.me/905321112233?text=" in page
    link = re.search(r'href="(https://wa\.me/905321112233\?text=[^"]+)"', page).group(1)
    from urllib.parse import unquote
    text = unquote(link.split("text=")[1].replace("&amp;", "&"))
    assert "Merhaba Ayşe" in text and "onaylandı" in text and "Yarın kargoda" in text
    assert "TR33 0006" in text and f"/s/ege-zeytin/o/" in text
    # kargo + yola çıkar
    r = c.post(f"/panel/orders/{oid}/cargo", data={"csrf": t, "cargo_company": "Aras Kargo", "cargo_tracking": "123456789",
                                                   "cargo_url": "https://kargotakip.example/123", "and_ship": "1"},
               follow_redirects=True)
    assert core.get_order(oid)["status"] == "on_the_way"
    text = unquote(r.get_data(as_text=True))
    assert "yola çıktı" in text and "Aras Kargo 123456789" in text
    tok = core.get_order(oid)["token"]
    j = c.get(f"/s/ege-zeytin/o/{tok}.json").get_json()
    assert j["cargo"]["no"] == "123456789" and j["cargo"]["url"].startswith("https://")
    assert c.post(f"/panel/orders/{oid}/cargo", data={"csrf": t, "cargo_url": "javascript:alert(1)"},
                  follow_redirects=True).status_code == 200
    assert core.get_order(oid)["cargo_url"] == "https://kargotakip.example/123"


def test_admin_impersonation(app):
    c = app.test_client()
    biz = make_biz(c)
    setup_shop(biz)
    login(c, "admin@x.com", "adminpass123")
    t = csrf(c, "/admin/")
    assert "Paneline gir" in c.get(f"/admin/{biz['id']}").get_data(as_text=True)
    r = c.post(f"/admin/{biz['id']}/enter", data={"csrf": t}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Yönetici olarak" in html and "Ege Zeytin" in html
    assert c.get("/panel/products").status_code == 200
    r = c.post("/panel/products/new", data={"csrf": t, "name": "Kurulum ürünü", "price": "100", "available": "1",
                                            "visible": "1"}, follow_redirects=True)
    assert db.one("SELECT business_id FROM products WHERE name = 'Kurulum ürünü'")["business_id"] == biz["id"]
    assert c.post("/panel/push/subscribe", headers={"X-CSRF-Token": t}, json={
        "endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": "a", "auth": "b"}}).status_code == 400
    c.post("/admin/exit", data={"csrf": t})
    assert c.get("/panel/orders").status_code == 302      # yönetici kendi başına işletmesiz
    # işletme sahibi bu uçları kullanamaz
    c3 = app.test_client()
    login(c3, "sahip@ege.com", "sahip12345")
    t3 = csrf(c3, "/panel/orders")
    assert c3.post(f"/admin/{biz['id']}/enter", data={"csrf": t3}).status_code == 403


# ── 2. aşama ────────────────────────────────────────────────────────────────

def test_shipping_zones(app):
    c, biz, (p1, p2, _), t = _owner(app)
    c.post("/panel/zones", data={"csrf": t, "action": "add", "name": "Kocaeli içi", "fee": "40", "free_over": "500"})
    c.post("/panel/zones", data={"csrf": t, "action": "add", "name": "Diğer iller", "fee": "150", "free_over": "0"})
    zs = core.zones(biz["id"])
    assert [z["name"] for z in zs] == ["Kocaeli içi", "Diğer iller"]
    assert "Kocaeli içi" in c.get("/s/ege-zeytin/").get_data(as_text=True)
    c2 = app.test_client()
    r = order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 1}])
    assert r.status_code == 400 and "bölgeni" in r.get_json()["error"]
    o = core.get_order(order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 1}], zone=zs[0]["id"]).get_json()["id"])
    assert o["delivery_fee"] == "40.00" and o["zone_name"] == "Kocaeli içi"
    o = core.get_order(order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 2}], zone=zs[0]["id"]).get_json()["id"])
    assert o["delivery_fee"] == "0.00"                   # 700 > 500 ücretsiz
    o = core.get_order(order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 2}], zone=zs[1]["id"]).get_json()["id"])
    assert o["delivery_fee"] == "150.00"
    # gel al bölge istemez
    o = core.get_order(order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 1}], fulfilment="pickup", phone="05440000000").get_json()["id"])
    assert o["delivery_fee"] == "0.00"
    c.post("/panel/zones", data={"csrf": t, "action": "delete", "id": zs[1]["id"]})
    assert len(core.zones(biz["id"])) == 1
    # başka işletme silemez
    make_biz(app.test_client(), "Bal Evi", "bal-evi", "", "b@b.com")
    c3 = app.test_client(); login(c3, "b@b.com", "sahip12345")
    c3.post("/panel/zones", data={"csrf": csrf(c3, "/panel/orders"), "action": "delete", "id": zs[0]["id"]})
    assert len(core.zones(biz["id"])) == 1


def test_reports(app):
    c, biz, (p1, p2, _), t = _owner(app)
    c2 = app.test_client()
    a = order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 2}]).get_json()["id"]                       # 750
    b = order(c2, "/s/ege-zeytin", [{"id": p2, "qty": 1}], phone="05440000000", name="Ali Kaya").get_json()["id"]   # 1800
    x = order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 1}]).get_json()["id"]
    for oid in (a, b):
        c.post(f"/panel/orders/{oid}/status", data={"csrf": t, "status": "approved"})
    c.post(f"/panel/orders/{x}/status", data={"csrf": t, "status": "rejected"})
    c.post(f"/panel/orders/{a}/payments", data={"csrf": t, "full": "1", "method": "cash"})
    for p in ("today", "7", "30", "month", "last"):
        assert c.get(f"/panel/reports?p={p}").status_code == 200
    html = c.get("/panel/reports?p=today").get_data(as_text=True)
    assert "2.550 TL" in html and "1.275 TL" in html          # ciro ve ortalama
    assert "750 TL" in html and "1.800 TL alacak" in html     # tahsil / alacak
    assert "Zeytinyağı (5 lt)" in html and "Siyah Zeytin (1 kg)" in html
    assert "1 sipariş" in html                                # red/iptal


def test_reorder_button(app):
    c, biz, (p1, _, _), t = _owner(app)
    r = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 3}]).get_json()
    c.post(f"/panel/orders/{r['id']}/status", data={"csrf": t, "status": "approved"})
    html = c.get(r["url"]).get_data(as_text=True)
    assert "Aynı siparişi tekrar ver" in html
    items = json.loads(re.search(r"data-items='([^']+)'", html).group(1))
    assert items == [{"id": p1, "variant": None, "qty": 3}]


# ── 3. aşama ────────────────────────────────────────────────────────────────

def test_first_order_discount(app):
    c, biz, (p1, p2, _), t = _owner(app)
    r = c.post("/panel/campaign", data={"csrf": t, "section": "first", "first_discount_active": "1",
                                        "first_discount_type": "percent", "first_discount_value": "10"}, follow_redirects=True)
    assert "İlk siparişine %10 indirim" in r.get_data(as_text=True)
    assert "İlk siparişine %10 indirim" in c.get("/s/ege-zeytin/").get_data(as_text=True)
    c2 = app.test_client()
    o1 = core.get_order(order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 2}]).get_json()["id"])   # 700 → −70 + 50
    assert o1["discount"] == "70.00" and o1["total"] == "680.00" and "%10" in o1["discount_note"]
    o2 = core.get_order(order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 2}]).get_json()["id"])
    assert o2["discount"] == "0.00"                         # aynı numara 2. sipariş
    # reddedilen ilk sipariş sonrası yeni numara gibi davranır
    c.post(f"/panel/orders/{o1['id']}/status", data={"csrf": t, "status": "rejected"})
    c.post(f"/panel/orders/{o2['id']}/status", data={"csrf": t, "status": "rejected"})
    o3 = core.get_order(order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 1}]).get_json()["id"])
    assert o3["discount"] == "35.00"
    # tutar indirimi, ara toplamı aşamaz
    c.post("/panel/campaign", data={"csrf": t, "section": "first", "first_discount_active": "1",
                                    "first_discount_type": "amount", "first_discount_value": "500"})
    o4 = core.get_order(order(c2, "/s/ege-zeytin", [{"id": p1, "qty": 1}], phone="05330000001").get_json()["id"])
    assert o4["discount"] == "350.00" and o4["total"] == "50.00"
    assert "en fazla %50" in c.post("/panel/campaign", data={"csrf": t, "section": "first", "first_discount_active": "1",
        "first_discount_type": "percent", "first_discount_value": "80"}, follow_redirects=True).get_data(as_text=True)


def test_referral(app):
    c, biz, (p1, _, _), t = _owner(app)
    c.post("/panel/campaign", data={"csrf": t, "section": "referral", "referral_active": "1", "referral_text": "250 g zeytin"})
    a = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}]).get_json()
    c.post(f"/panel/orders/{a['id']}/status", data={"csrf": t, "status": "approved"})
    html = c.get(a["url"]).get_data(as_text=True)
    assert "Arkadaşını davet et" in html
    code = re.search(r"\?ref=([A-Z0-9]+)", html).group(1)
    referrer = core.get_order(a["id"])["customer_id"]
    # kendi kodu işe yaramaz
    self_o = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], ref=code).get_json()
    assert core.get_order(self_o["id"])["referred_by"] is None
    # arkadaş
    b = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], ref=code.lower(), phone="05441112233",
              name="Zeynep Ak").get_json()
    ob = core.get_order(b["id"])
    assert ob["referred_by"] == referrer
    page = c.post(f"/panel/orders/{b['id']}/status", data={"csrf": t, "status": "approved"},
                  follow_redirects=True).get_data(as_text=True)
    assert "250 g zeytin" in page
    assert [r["kind"] for r in core.pending_rewards(ob["customer_id"])] == ["referral"]
    rr = core.pending_rewards(referrer)
    assert len(rr) == 1 and "Zeynep" in rr[0]["text"]
    # aynı arkadaş ikinci kez ödül kazandırmaz
    b2 = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], ref=code, phone="05441112233", name="Zeynep Ak").get_json()
    assert core.get_order(b2["id"])["referred_by"] is None


def test_birthday_gift(app):
    from datetime import datetime, timedelta
    c, biz, (p1, _, _), t = _owner(app)
    c.post("/panel/campaign", data={"csrf": t, "section": "birthday", "birthday_active": "1", "birthday_text": "Zeytin ezmesi"})
    assert "Doğum günün" in c.get("/s/ege-zeytin/").get_data(as_text=True)
    soon = datetime.now(core.TZ).date() + timedelta(days=3)
    far = datetime.now(core.TZ).date() + timedelta(days=60)
    a = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], birth_day=soon.day, birth_month=soon.month).get_json()
    b = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], phone="05449998877",
              birth_day=far.day, birth_month=far.month).get_json()
    for x in (a, b):
        c.post(f"/panel/orders/{x['id']}/status", data={"csrf": t, "status": "approved"})
    ca, cb = core.get_order(a["id"])["customer_id"], core.get_order(b["id"])["customer_id"]
    assert [r["kind"] for r in core.pending_rewards(ca)] == ["birthday"]
    assert core.pending_rewards(cb) == []
    # aynı yıl ikinci kez verilmez
    a2 = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}]).get_json()
    c.post(f"/panel/orders/{a2['id']}/status", data={"csrf": t, "status": "approved"})
    assert len(core.pending_rewards(ca)) == 1


def test_broadcast(app):
    c, biz, (p1, _, _), t = _owner(app)
    order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], marketing_ok=True)
    order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], phone="05441112233", name="İzinsiz Kişi")
    html = c.get("/panel/broadcast").get_data(as_text=True)
    assert "Ayşe Yılmaz" in html and "İzinsiz Kişi" not in html
    r = c.post("/panel/broadcast", data={"csrf": t, "message": "Merhaba {ad}, yeni hasat geldi: {link}"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    from urllib.parse import unquote
    link = re.search(r'href="(https://wa\.me/905321112233\?text=[^"]+)"', html).group(1)
    text = unquote(link.split("text=")[1])
    assert text.startswith("Merhaba Ayşe, yeni hasat geldi: ") and "/s/ege-zeytin/" in text and "İPTAL" in text
    cid = db.one("SELECT id FROM customers WHERE phone = '5321112233'")["id"]
    c.post("/panel/broadcast", data={"csrf": t, "action": "optout", "customer_id": cid})
    assert "Ayşe Yılmaz" not in c.get("/panel/broadcast").get_data(as_text=True)


def test_card_payment_flow(app, monkeypatch):
    import paytr
    monkeypatch.setenv("CARD_PAYMENTS", "1")
    c, biz, (p1, _, _), t = _owner(app, pay_cash=1)
    # Kart kapalıyken seçilemez
    assert order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], payment="card").status_code == 400
    form = {"csrf": t, "name": "Ege Zeytin", "pay_cash": "1", "pay_iban": "1", "iban": IBAN, "iban_holder": "Ali Veli",
            "delivery": "1", "delivery_fee": "50", "free_delivery_over": "0", "min_order": "0", "ordering_open": "1",
            "pay_card": "1", "paytr_merchant_id": "123456", "paytr_key": "KEYKEYKEY", "paytr_salt": "SALTSALT", "paytr_test": "1"}
    assert "Ayarlar kaydedildi" in c.post("/panel/settings", data=form, follow_redirects=True).get_data(as_text=True)
    b = db.one("SELECT * FROM businesses WHERE id = ?", (biz["id"],))
    assert b["paytr_key_enc"] and "KEYKEYKEY" not in b["paytr_key_enc"]
    with app.app_context():
        assert paytr.creds(b)["key"] == "KEYKEYKEY"
    # anahtar boş bırakılınca eskisi korunur
    form.update(paytr_key="", paytr_salt="")
    c.post("/panel/settings", data=form)
    assert db.one("SELECT paytr_key_enc FROM businesses WHERE id = ?", (biz["id"],))["paytr_key_enc"] == b["paytr_key_enc"]
    assert "Kart" in c.get("/s/ege-zeytin/").get_data(as_text=True)

    r = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 2}], payment="card").get_json()
    assert r["pay_url"].endswith("/pay/" + core.get_order(r["id"])["token"])
    captured = {}
    monkeypatch.setattr(paytr, "get_token", lambda payload, session=None: captured.update(payload) or "TOK123")
    html = app.test_client().get(r["pay_url"]).get_data(as_text=True)
    assert "paytr.com/odeme/guvenli/TOK123" in html
    assert captured["payment_amount"] == "75000" and captured["test_mode"] == "1" and captured["merchant_id"] == "123456"
    oid = captured["merchant_oid"]
    with app.app_context():
        good = paytr.callback_hash(b, oid, "success", "75000")
    cl = app.test_client()
    assert cl.post("/paytr/callback", data={"merchant_oid": oid, "status": "success", "total_amount": "75000",
                                            "hash": "yanlis"}).status_code == 400
    assert core.get_order(r["id"])["paid"] == 0
    assert cl.post("/paytr/callback", data={"merchant_oid": oid, "status": "success", "total_amount": "75000",
                                            "hash": good}).get_data(as_text=True) == "OK"
    o = core.get_order(r["id"])
    assert o["paid"] == 1 and core.payments(o["id"])[0]["method"] == "card"
    # tekrar gelen bildirim çift ödeme yazmaz
    cl.post("/paytr/callback", data={"merchant_oid": oid, "status": "success", "total_amount": "75000", "hash": good})
    assert len(core.payments(o["id"])) == 1
    # ödenmiş siparişin ödeme sayfası siparişe yönlendirir
    assert app.test_client().get(r["pay_url"]).status_code == 302


def test_share_guide_and_instagram(app):
    c, biz, _, t = _owner(app)
    html = c.get("/panel/qr").get_data(as_text=True)
    assert "Instagram profil linki" in html and "Google İşletme Profili" in html


# ── 4. aşama ────────────────────────────────────────────────────────────────

def test_subscription_expiry_and_extend(app):
    c, biz, (p1, _, _), t = _owner(app)
    b = db.one("SELECT * FROM businesses WHERE id = ?", (biz["id"],))
    sub = core.subscription(b)
    assert b["plan"] == "trial" and 13 <= sub["days_left"] <= 14 and sub["ok"]
    # 4 gün kaldı -> uyarı
    db.update("businesses", biz["id"], {"paid_until": (core.datetime.now(core.timezone.utc) + core.timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")}); db.commit()
    assert "gün sonra bitiyor" in c.get("/panel/orders").get_data(as_text=True)
    # 3 gün önce bitti (2 gün tolerans aşıldı) -> mağaza kapanır
    db.update("businesses", biz["id"], {"paid_until": (core.datetime.now(core.timezone.utc) - core.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}); db.commit()
    assert "şu an sipariş almıyor" in c.get("/s/ege-zeytin/").get_data(as_text=True)
    r = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}])
    assert r.status_code == 400
    assert "süren doldu" in c.get("/panel/orders").get_data(as_text=True)
    assert c.get("/panel/products").status_code == 200       # veriler erişilebilir kalır
    assert c.get("/panel/subscription").status_code == 200
    # yönetici 1 ay ekler -> bugünden itibaren 30 gün
    a = app.test_client(); login(a, "admin@x.com", "adminpass123")
    ta = csrf(a, "/admin/")
    a.post(f"/admin/{biz['id']}", data={"csrf": ta, "action": "plan", "plan": "monthly"})
    b = db.one("SELECT * FROM businesses WHERE id = ?", (biz["id"],))
    assert b["plan"] == "monthly" and 29 <= core.subscription(b)["days_left"] <= 30
    assert order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}]).status_code == 200
    a.post(f"/admin/{biz['id']}", data={"csrf": ta, "action": "plan", "plan": "yearly"})
    assert 393 <= core.subscription(db.one("SELECT * FROM businesses WHERE id = ?", (biz["id"],)))["days_left"] <= 395
    a.post(f"/admin/{biz['id']}", data={"csrf": ta, "action": "plan", "plan": "free"})
    assert core.subscription(db.one("SELECT * FROM businesses WHERE id = ?", (biz["id"],)))["ok"]


def test_platform_settings_and_landing(app):
    a = app.test_client(); login(a, "admin@x.com", "adminpass123")
    t = csrf(a, "/admin/settings")
    a.post("/admin/settings", data={"csrf": t, "trial_days": "30", "price_monthly": "499", "price_yearly": "4990",
                                    "pay_iban": IBAN, "pay_holder": "Can A.", "pay_bank": "Banka", "contact_whatsapp": "0532 999 88 77"})
    ps = core.platform_settings()
    assert ps["trial_days"] == "30" and ps["price_monthly"] == "499" and ps["contact_whatsapp"] == "5329998877"
    assert core.trial_days() == 30
    html = app.test_client().get("/", headers={"Host": "platform.test"}).get_data(as_text=True)
    assert "499 TL" in html and "30 gün ücretsiz" in html and "Ücretsiz dene" in html
    # demo mağaza
    a.post("/admin/demo", data={"csrf": t}, follow_redirects=True)
    demo = db.one("SELECT * FROM businesses WHERE is_demo = 1")
    assert demo and core.subscription(demo)["plan"] == "free"
    assert "Örnek mağazayı gör" in app.test_client().get("/", headers={"Host": "platform.test"}).get_data(as_text=True)
    page = app.test_client().get(f"/s/{demo['slug']}/").get_data(as_text=True)
    assert "Gemlik Siyah Zeytin" in page and "500 g · 1 kg · 5 kg" in page
    a.post("/admin/demo", data={"csrf": t})
    assert db.one("SELECT COUNT(*) AS n FROM businesses WHERE is_demo = 1")["n"] == 1


def test_self_signup(app):
    c = app.test_client()
    assert c.get("/kayit").status_code == 200
    bad = c.post("/kayit", data={"name": "X", "owner_name": "", "email": "nope", "phone": "123", "password": "kisa"})
    assert bad.status_code == 400
    r = c.post("/kayit", data={"name": "Bal Evi Çiçek", "owner_name": "Mehmet Bal", "email": "Bal@Ornek.com",
                               "phone": "0532 444 55 66", "password": "guclusifre1", "kvkk": "1"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Hoş geldin" in html and "Kurulum" in html and "İlk ürününü ekle" in html
    b = db.one("SELECT * FROM businesses WHERE slug = 'bal-evi-cicek'")
    assert b and b["self_signup"] == 1 and b["plan"] == "trial" and b["phone"] == "5324445566"
    u = db.one("SELECT * FROM users WHERE email = 'bal@ornek.com'")
    assert u["business_id"] == b["id"] and not u["is_admin"]
    # aynı e-posta tekrar kaydolamaz, aynı isim yeni kısa ad alır
    c2 = app.test_client()
    assert c2.post("/kayit", data={"name": "Bal Evi Çiçek", "owner_name": "Ali", "email": "bal@ornek.com",
                                   "phone": "05324445566", "password": "guclusifre1", "kvkk": "1"}).status_code == 400
    c2.post("/kayit", data={"name": "Bal Evi Çiçek", "owner_name": "Ali", "email": "ali@ornek.com",
                            "phone": "05324445567", "password": "guclusifre1", "kvkk": "1"})
    assert db.one("SELECT id FROM businesses WHERE slug = 'bal-evi-cicek-2'")
    # bot tuzağı
    assert app.test_client().post("/kayit", data={"name": "Bot", "website": "http://spam"}).status_code == 400
    # yeni kayıt yönetici listesinde görünür
    a = app.test_client(); login(a, "admin@x.com", "adminpass123")
    assert "yeni kayıt" in a.get("/admin/").get_data(as_text=True)


def test_signup_closed(app, monkeypatch):
    monkeypatch.setenv("SIGNUP_OPEN", "0")
    assert app.test_client().get("/kayit").status_code == 404


def test_setup_steps_progress(app):
    c, biz, _, t = _owner(app, phone="5321112233")
    html = c.get("/panel/orders").get_data(as_text=True)
    assert "Kurulum · 3/5" in html            # ürün, ayar, kampanya tamam; bildirim ve ilk sipariş yok



# ── Sadece IBAN + açıklama ──────────────────────────────────────────────────

def test_payment_options_per_business(app):
    c, biz, (p1, _, _), t = _owner(app)
    base = {"csrf": t, "name": "Ege Zeytin", "delivery": "1", "delivery_fee": "50", "free_delivery_over": "0",
            "min_order": "0", "ordering_open": "1", "iban": IBAN, "iban_holder": "Ali Veli"}
    # Yalnız IBAN: seçim kutusu yok, bilgi kutusu var; nakit reddedilir
    c.post("/panel/settings", data={**base, "pay_iban": "1"})
    html = c.get("/s/ege-zeytin/").get_data(as_text=True)
    assert 'name="payment" value="iban"' in html and 'value="cash"' not in html and "Ödeme: IBAN / havale" in html
    assert order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], payment="cash").status_code == 400
    # IBAN + kapıda nakit + kapıda kart: üçü de görünür
    c.post("/panel/settings", data={**base, "pay_iban": "1", "pay_cash": "1", "pay_pos": "1"})
    html = c.get("/s/ege-zeytin/").get_data(as_text=True)
    assert html.count('name="payment"') == 3 and "kapıda POS" in html
    for m in ("iban", "cash", "pos"):
        r = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], payment=m, phone="0532111220" + str("icp".index(m[0]) if m[0] in "icp" else 0))
        assert r.status_code == 200, (m, r.get_json())
        assert core.get_order(r.get_json()["id"])["payment"] == m
    # kapıda kart siparişinde "tamamı alındı" kart olarak kaydedilir
    pos_id = db.one("SELECT id FROM orders WHERE payment = 'pos'")["id"]
    c.post(f"/panel/orders/{pos_id}/payments", data={"csrf": t, "full": "1", "method": "pos"})
    assert core.payments(pos_id)[0]["method"] == "card" and core.get_order(pos_id)["paid"] == 1
    st = c.get(f"/s/ege-zeytin/o/{core.get_order(pos_id)['token']}").get_data(as_text=True)
    assert "Ödemen alındı" in st
    # Sadece kapıda nakit: IBAN'sız da sipariş alır
    c.post("/panel/settings", data={**base, "pay_cash": "1", "iban": "", "iban_holder": ""})
    html = c.get("/s/ege-zeytin/").get_data(as_text=True)
    assert "Ödeme: Kapıda nakit" in html and 'value="iban"' not in html
    r = order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], payment=None, phone="05445556677")
    assert r.status_code == 200 and core.get_order(r.get_json()["id"])["payment"] == "cash"
    # IBAN açık ama IBAN girilmemiş: uyarı + müşteriye görünmez
    r = c.post("/panel/settings", data={**base, "pay_iban": "1", "pay_cash": "1", "iban": "", "iban_holder": ""}, follow_redirects=True)
    assert "IBAN seçeneği müşterilere görünmez" in r.get_data(as_text=True)
    assert 'value="iban"' not in c.get("/s/ege-zeytin/").get_data(as_text=True)
    # hiçbir yöntem seçilmezse kaydedilmez
    r = c.post("/panel/settings", data=base, follow_redirects=True)
    assert "En az bir ödeme yöntemi seç" in r.get_data(as_text=True)
    # online kart platformda kapalı: PayTR alanı görünmez, seçilemez
    assert "PayTR" not in c.get("/panel/settings").get_data(as_text=True)
    assert order(app.test_client(), "/s/ege-zeytin", [{"id": p1, "qty": 1}], payment="card").status_code == 400


def test_new_business_defaults_to_iban_only(app):
    c = app.test_client()
    biz = make_biz(c)
    b = db.one("SELECT pay_iban, pay_cash, pay_pos FROM businesses WHERE id = ?", (biz["id"],))
    assert (b["pay_iban"], b["pay_cash"], b["pay_pos"]) == (1, 0, 0)
    c.post("/kayit", data={"name": "Bal Evi", "owner_name": "Mehmet Bal", "email": "bal@ornek.com",
                           "phone": "05324445566", "password": "guclusifre1", "kvkk": "1"})
    b = db.one("SELECT pay_iban, pay_cash, pay_pos FROM businesses WHERE slug = 'bal-evi'")
    assert (b["pay_iban"], b["pay_cash"], b["pay_pos"]) == (1, 0, 0)


def test_slogan_and_description(app):
    c, biz, _, t = _owner(app)
    about = "1985'ten beri kendi bahçelerimizden zeytin üretiyoruz.\nKatkısız, doğal salamura."
    form = {"csrf": t, "name": "Ege Zeytin", "tagline": "Ayvalık'tan sofranıza", "about": about, "delivery": "1",
            "delivery_fee": "50", "free_delivery_over": "0", "min_order": "0", "ordering_open": "1",
            "iban": IBAN, "iban_holder": "Ali Veli", "pay_iban": "1"}
    assert "Ayarlar kaydedildi." in c.post("/panel/settings", data=form, follow_redirects=True).get_data(as_text=True)
    b = db.one("SELECT tagline, about FROM businesses WHERE id = ?", (biz["id"],))
    assert b["tagline"] == "Ayvalık'tan sofranıza" and b["about"] == about.replace("\r", "")
    html = c.get("/s/ege-zeytin/").get_data(as_text=True)
    head = html.split('class="st-cats"')[0] if 'class="st-cats"' in html else html.split("st-list")[0]
    assert "st-slogan" in head and "Ayvalık&#39;tan sofranıza" in head
    assert "1985&#39;ten beri kendi bahçelerimizden" in head and "Devamını oku" in head
    # uzunluk sınırı ve HTML kaçışı
    form["about"] = "<script>alert(1)</script>" + "x" * 900
    c.post("/panel/settings", data=form)
    b = db.one("SELECT about FROM businesses WHERE id = ?", (biz["id"],))
    assert len(b["about"]) == 800
    assert "<script>alert(1)</script>" not in c.get("/s/ege-zeytin/").get_data(as_text=True)


def test_flyer(app):
    c, biz, (p1, p2, p3), t = _owner(app)
    db.update("businesses", biz["id"], {"tagline": "Ayvalık'tan sofranıza", "about": "Kendi bahçemizden.",
                                        "first_discount_active": 1, "first_discount_type": "percent",
                                        "first_discount_value": "10", "instagram": "egezeytin"}); db.commit()
    for size in ("a4", "a5", "a6"):
        r = c.get(f"/panel/flyer?size={size}")
        assert r.status_code == 200
    html = c.get("/panel/flyer?size=a5").get_data(as_text=True)
    assert html.count('class="fl"') == 2 and html.count('aria-label="QR kod"') == 2 and "cdn.jsdelivr" not in html
    assert "Ayvalık&#39;tan sofranıza" in html and "Kendi bahçemizden." in html
    assert "İlk siparişine %10 indirim" in html and "Her 3 siparişe 1 kg zeytin hediye" in html
    assert "Siyah Zeytin" in html and "Tükenen" not in html and "@egezeytin" in html
    assert c.get("/panel/flyer?size=a6").get_data(as_text=True).count('class="fl"') == 4
    assert "Siyah Zeytin" not in c.get("/panel/flyer?urun=0").get_data(as_text=True)
    assert "Broşür hazırla" in c.get("/panel/qr").get_data(as_text=True)
    assert app.test_client().get("/panel/flyer").status_code == 302



def test_qr_codes_decode(app):
    """Üretilen QR'lar gerçekten okunuyor mu (OpenCV ile)."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    import qr
    dets = [cv2.QRCodeDetector()] + ([cv2.QRCodeDetectorAruco()] if hasattr(cv2, "QRCodeDetectorAruco") else [])
    for text in ["https://siparisqr-production.up.railway.app/s/ege-zeytin/", "https://egezeytin.com/",
                 "https://siparisqr-production.up.railway.app/s/bal-evi-cicek-2/?ref=K7P2X" + "x" * 80]:
        m = qr.matrix(text)
        n = len(m) + 8
        img = np.full((n, n), 255, np.uint8)
        for y, row in enumerate(m):
            for x, d in enumerate(row):
                if d:
                    img[y + 4, x + 4] = 0
        img = cv2.resize(img, (n * 8, n * 8), interpolation=cv2.INTER_NEAREST)
        assert any(d.detectAndDecode(img)[0] == text for d in dets), text
    c, biz, _, t = _owner(app)
    r = c.get("/panel/qr.svg")
    assert r.status_code == 200 and r.mimetype == "image/svg+xml" and b"<path" in r.data
    assert "cdn.jsdelivr" not in c.get("/panel/qr").get_data(as_text=True)
    assert "cdn.jsdelivr" not in c.get("/panel/qr/print").get_data(as_text=True)
