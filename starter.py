"""Hazır başlangıç listeleri: işletme tik atıp fiyat yazarak ilk gün satışa başlar.
Markasız, genel adlar; işletme sonra kendi markasına göre adı düzenleyebilir."""

PACKS = {
    "market": {
        "title": "Market / bakkal",
        "desc": "Mahalle marketinde en çok sorulan ürünler",
        "items": [
            ("Fırın", [("Ekmek", "1 adet"), ("Tam buğday ekmeği", "1 adet"), ("Kepekli ekmek", "1 adet"),
                       ("Pide (Ramazan)", "1 adet"), ("Simit", "1 adet"), ("Lavaş", "paket"),
                       ("Tost ekmeği", "paket"), ("Galeta", "paket")]),
            ("Süt ve kahvaltılık", [("Süt", "1 L"), ("Süt", "1/2 L"), ("Laktozsuz süt", "1 L"), ("Ayran", "1 L"),
                                    ("Ayran", "200 ml"), ("Yoğurt", "1 kg"), ("Yoğurt", "500 g"), ("Süzme yoğurt", "500 g"),
                                    ("Beyaz peynir", "500 g"), ("Kaşar peyniri", "400 g"), ("Labne", "200 g"),
                                    ("Krem peynir", "200 g"), ("Tereyağı", "250 g"), ("Margarin", "250 g"),
                                    ("Yumurta", "10'lu"), ("Yumurta", "15'li"), ("Yumurta", "30'lu"),
                                    ("Siyah zeytin", "500 g"), ("Yeşil zeytin", "500 g"), ("Bal", "450 g"),
                                    ("Reçel", "380 g"), ("Tahin", "300 g"), ("Pekmez", "700 g"),
                                    ("Fındık kreması", "350 g"), ("Sucuk", "250 g"), ("Salam", "100 g"),
                                    ("Kaymak", "200 g")]),
            ("Temel gıda", [("Toz şeker", "1 kg"), ("Toz şeker", "3 kg"), ("Küp şeker", "500 g"), ("Tuz", "750 g"),
                            ("Un", "1 kg"), ("Un", "5 kg"), ("Pirinç", "1 kg"), ("Bulgur", "1 kg"),
                            ("Kırmızı mercimek", "1 kg"), ("Yeşil mercimek", "1 kg"), ("Nohut", "1 kg"),
                            ("Kuru fasulye", "1 kg"), ("Makarna", "500 g"), ("Erişte", "500 g"), ("Şehriye", "500 g"),
                            ("Ayçiçek yağı", "1 L"), ("Ayçiçek yağı", "5 L"), ("Zeytinyağı", "1 L"),
                            ("Domates salçası", "830 g"), ("Biber salçası", "650 g"), ("Ton balığı", "3x75 g"),
                            ("Konserve bezelye", "400 g"), ("Mısır konservesi", "220 g"), ("Turşu", "1 kg"),
                            ("Kırmızı pul biber", "100 g"), ("Karabiber", "50 g"), ("Kimyon", "50 g"),
                            ("Nane", "50 g"), ("Kabartma tozu", "5'li"), ("Vanilin", "5'li"), ("Maya", "3'lü"),
                            ("Hazır çorba", "1 paket"), ("Kek karışımı", "1 paket")]),
            ("Meyve ve sebze", [("Domates", "1 kg"), ("Salatalık", "1 kg"), ("Patates", "1 kg"), ("Kuru soğan", "1 kg"),
                                ("Sarımsak", "250 g"), ("Biber", "1 kg"), ("Limon", "1 kg"), ("Muz", "1 kg"),
                                ("Elma", "1 kg"), ("Portakal", "1 kg"), ("Mandalina", "1 kg"), ("Maydanoz", "1 bağ"),
                                ("Marul", "1 adet")]),
            ("İçecek", [("Su", "0,5 L"), ("Su", "1,5 L"), ("Su", "5 L"), ("Damacana su", "19 L"),
                        ("Maden suyu", "200 ml"), ("Maden suyu", "6'lı"), ("Kola", "1 L"), ("Kola", "2,5 L"),
                        ("Kola (kutu)", "330 ml"), ("Gazoz", "1 L"), ("Meyve suyu", "1 L"), ("Meyve suyu", "200 ml"),
                        ("Soğuk çay", "330 ml"), ("Enerji içeceği", "250 ml"), ("Siyah çay", "1 kg"),
                        ("Siyah çay", "500 g"), ("Bitki çayı", "20'li"), ("Türk kahvesi", "100 g"),
                        ("Hazır kahve (3'ü 1 arada)", "10'lu"), ("Granül kahve", "100 g")]),
            ("Atıştırmalık", [("Cips", "büyük boy"), ("Cips", "küçük boy"), ("Kraker", "1 paket"),
                              ("Bisküvi", "1 paket"), ("Gofret", "1 adet"), ("Çikolata", "80 g"),
                              ("Sütlü çikolata (tablet)", "80 g"), ("Kek", "1 adet"), ("Kuruyemiş karışık", "150 g"),
                              ("Çekirdek", "200 g"), ("Leblebi", "200 g"), ("Sakız", "1 paket"),
                              ("Şekerleme", "1 paket"), ("Dondurma", "1 adet"), ("Dondurma", "1 L")]),
            ("Dondurulmuş", [("Dondurulmuş patates", "1 kg"), ("Dondurulmuş pizza", "1 adet"),
                             ("Mantı", "500 g"), ("Sigara böreği", "500 g")]),
            ("Temizlik", [("Bulaşık deterjanı", "750 ml"), ("Bulaşık makinesi tableti", "40'lı"),
                          ("Çamaşır deterjanı (toz)", "4 kg"), ("Çamaşır deterjanı (sıvı)", "2,5 L"),
                          ("Yumuşatıcı", "1,4 L"), ("Çamaşır suyu", "1 L"), ("Yüzey temizleyici", "1 L"),
                          ("Cam sil", "500 ml"), ("Tuvalet kağıdı", "12'li"), ("Tuvalet kağıdı", "32'li"),
                          ("Kağıt havlu", "6'lı"), ("Peçete", "1 paket"), ("Islak mendil", "1 paket"),
                          ("Çöp poşeti", "1 rulo"), ("Sünger", "3'lü"), ("Streç film", "1 adet"),
                          ("Alüminyum folyo", "1 adet")]),
            ("Kişisel bakım", [("Şampuan", "500 ml"), ("Duş jeli", "500 ml"), ("Sabun", "4'lü"),
                               ("Sıvı sabun", "1 L"), ("Diş macunu", "75 ml"), ("Diş fırçası", "1 adet"),
                               ("Deodorant", "150 ml"), ("Tıraş bıçağı", "1 paket"), ("Hijyenik ped", "1 paket"),
                               ("Pamuk", "1 paket"), ("Kolonya", "400 ml")]),
            ("Bebek", [("Bebek bezi", "1 paket"), ("Bebek ıslak mendili", "1 paket"), ("Bebek maması", "1 kutu")]),
            ("Diğer", [("Pil (AA)", "4'lü"), ("Çakmak", "1 adet"), ("Kibrit", "1 paket"), ("Mum", "1 paket"),
                       ("Kedi maması", "1 kg"), ("Köpek maması", "1 kg"), ("Poşet", "1 adet")]),
        ],
    },
}


def pack_items(key):
    """[(kategori, [(sıra no, ad, birim)])] — formda her ürün sıra numarasıyla tanınır."""
    pack = PACKS.get(key)
    if not pack:
        return None
    out, n = [], 0
    for cat, items in pack["items"]:
        rows = []
        for name, unit in items:
            rows.append((n, name, unit))
            n += 1
        out.append((cat, rows))
    return out


def flat(key):
    return {n: (cat, name, unit) for cat, rows in (pack_items(key) or []) for n, name, unit in rows}
