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
            "payment": "cash", "fulfilment": "delivery", "note": ""}
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
                                "address": "uzun bir adres yazdım", "payment": "cash", "fulfilment": "delivery"}))
    assert r.status_code == 400
    r = c.post("/api/order", headers={"Host": "egezeytin.com"}, content_type="application/json",
               data=json.dumps({"items": [{"id": pa, "qty": 1}], "name": "X Y", "phone": "05321112233",
                                "address": "uzun bir adres yazdım", "payment": "cash", "fulfilment": "delivery"}))
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
