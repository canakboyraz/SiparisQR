"""Yerelde denemek için örnek işletme: python demo.py  (sonra: flask --app wsgi run)"""
import io
import os

from PIL import Image, ImageDraw, ImageFilter
from werkzeug.security import generate_password_hash

from dotenv_lite import load_env

load_env()
import core  # noqa: E402
import db  # noqa: E402
from app import create_app  # noqa: E402


def photo(c1, c2, dots):
    im = Image.new("RGB", (900, 900), c1)
    d = ImageDraw.Draw(im)
    for i in range(900):
        t = i / 900
        d.line([(0, i), (900, i)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    for x, y, r, col in dots:
        d.ellipse([x - r, y - r * 1.25, x + r, y + r * 1.25], fill=col)
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    out = io.BytesIO(); im.save(out, "JPEG", quality=85)
    return out.getvalue()


def create_demo(slug="ege-zeytin", name="Ege Zeytin", owner_email="zeytin@ornek.com", owner_password="zeytin123",
                is_demo=0):
    """Örnek ürünlü mağaza kurar. Döner: işletme id. Varsa dokunmaz."""
    existing = db.one("SELECT id FROM businesses WHERE slug = ?", (slug,))
    if existing:
        return existing["id"]
    bid = db.insert("businesses", {"slug": slug, "name": name, "created_at": core.now(),
                                   "tagline": "Ayvalık'tan sofranıza, bu yılın hasadı", "accent": "#4B5D2A",
                                   "iban": "TR330006100519786457841326", "iban_holder": "Örnek İşletme", "iban_bank": "Örnek Banka",
                                   "delivery_fee": "60.00", "free_delivery_over": "1500.00", "reward_active": 1,
                                   "reward_every": 6, "reward_text": "1 kg zeytin", "phone": "5321112233",
                                   "first_discount_active": 1, "first_discount_type": "percent", "first_discount_value": "10",
                                   "referral_active": 1, "referral_text": "250 g zeytin",
                                   "delivery_note": "Kocaeli içi aynı gün, diğer iller 2-3 gün kargo",
                                   "pickup_address": "Örnek Mah. Zeytin Sok. No: 5", "is_demo": is_demo,
                                   "plan": "free" if is_demo else "trial", "paid_until": None if is_demo else core.trial_until()})
    if owner_email:
        db.insert("users", {"business_id": bid, "email": owner_email, "name": "Örnek",
                            "password_hash": generate_password_hash(owner_password), "created_at": core.now()})
    cats = {n: db.insert("categories", {"business_id": bid, "name": n, "position": i})
            for i, n in enumerate(["Zeytinler", "Zeytinyağları", "Kahvaltılık"])}
    black, green = (40, 34, 38), (110, 128, 50)
    items = [
        ("Zeytinler", "Gemlik Siyah Zeytin", "", "380", "Az tuzlu, salamura. Bu yılın hasadı.", ((236, 226, 204), (214, 198, 166), [(300, 360, 70, black), (480, 420, 74, black), (620, 330, 68, black), (420, 600, 72, black)]),
         [("500 g", "200", None), ("1 kg", "380", None), ("5 kg", "1750", 4)]),
        ("Zeytinler", "Kırma Yeşil Zeytin", "1 kg", "320", "Limon ve kekikle.", ((240, 236, 214), (210, 214, 170), [(320, 380, 70, green), (520, 440, 76, green), (420, 620, 70, green)]), []),
        ("Zeytinler", "Çizik Yeşil Zeytin", "500 g", "190", "", ((240, 236, 214), (214, 222, 180), [(380, 420, 80, green), (560, 520, 70, green)]), []),
        ("Zeytinyağları", "Natürel Sızma Zeytinyağı", "", "1.850", "Soğuk sıkım, asit oranı 0,4.", ((236, 222, 160), (198, 170, 70), [(450, 470, 150, (170, 150, 40))]),
         [("1 lt", "420", None), ("5 lt teneke", "1850", 12)]),
        ("Zeytinyağları", "Erken Hasat Zeytinyağı", "500 ml", "420", "", ((230, 232, 170), (150, 170, 60), [(450, 470, 120, (120, 140, 30))]), []),
        ("Kahvaltılık", "Zeytin Ezmesi", "300 g", "160", "", ((210, 196, 180), (120, 100, 90), [(450, 470, 160, (60, 50, 48))]), []),
    ]
    for i, (cat, pname, unit, price, desc, (c1, c2, dots), variants) in enumerate(items):
        img = db.insert("images", {"business_id": bid, "mime": "image/jpeg", "data": db.blob(photo(c1, c2, dots)), "created_at": core.now()})
        pid = db.insert("products", {"business_id": bid, "category_id": cats[cat], "name": pname, "unit": unit,
                                     "price": str(core.money(variants[0][1] if variants else price)), "description": desc,
                                     "image_id": img, "position": i, "stock": 3 if pname == "Erken Hasat Zeytinyağı" else None,
                                     "available": 0 if pname == "Çizik Yeşil Zeytin" else 1, "created_at": core.now()})
        for pos, (vn, vp, vs) in enumerate(variants):
            db.insert("product_variants", {"business_id": bid, "product_id": pid, "name": vn, "price": str(core.money(vp)),
                                           "stock": vs, "position": pos})
    db.commit()
    return bid


def main():
    create_app()
    create_demo()
    print("Hazır: /s/ege-zeytin/  · panel girişi: zeytin@ornek.com / zeytin123")


if __name__ == "__main__":
    main()
