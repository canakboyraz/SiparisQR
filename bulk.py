"""Çok ürünlü işletmeler (market, bakkal) için toplu ürün girişi:

- Excel (.xlsx) / CSV dosyasından yükleme: sütunlar tahmin edilir, işletme önizlemede düzeltir.
  Barkod (yoksa ad) ile eşleşen ürünün fiyatı güncellenir, olmayanlar eklenir.
- Ortak katalog (catalog_items): bir işletmenin barkodla girdiği ürün adı/birimi/fotoğrafı
  sonraki işletmelere hazır gelir. Katalogda yoksa Open Food Facts'e bakılır.
- Hızlı ekleme ve hazır başlangıç listesi bu modüldeki save_simple() ile ürün oluşturur.

Ek kütüphane gerektirmez: xlsx dosyası zip + XML olarak okunur.
"""
import csv
import io
import json
import re
import unicodedata
import zipfile
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

import core
import db

MAX_ROWS = 5000
FIELDS = {
    "name": "Ürün adı",
    "price": "Satış fiyatı",
    "barcode": "Barkod",
    "category": "Kategori / grup",
    "unit": "Birim / gramaj",
    "stock": "Stok",
    "description": "Açıklama",
}
# Başlık tahmini: alan -> başlıkta geçebilecek kelimeler (öncelik sırasıyla)
_HINTS = {
    "barcode": ("barkod", "barcode", "ean", "gtin"),
    "name": ("urun adi", "urunadi", "stok adi", "stokadi", "mal adi", "malzeme adi", "urun ismi", "aciklama1", "adi",
             "urun", "isim", "name", "tanim"),
    "price": ("satis fiyati", "satisfiyati", "perakende", "satis fiyat", "fiyat", "price", "satis", "tutar"),
    "category": ("kategori", "grup", "grubu", "reyon", "category", "sinif", "ana grup"),
    "unit": ("birim", "gramaj", "olcu", "miktar birimi", "unit", "ambalaj"),
    "stock": ("stok miktari", "stok adedi", "mevcut", "miktar", "stock", "adet", "kalan"),
    "description": ("aciklama", "detay", "description", "not"),
}
_AVOID = {"price": ("alis", "maliyet", "kdv haric", "kdvsiz", "eski"), "name": ("kod",), "stock": ("kod", "birim")}


class ImportError_(ValueError):
    pass


def _fold(s):
    s = (s or "").strip().lower().replace("ı", "i").replace("İ", "i")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


# ── Dosya okuma ─────────────────────────────────────────────────────────────

def _plain_number(text):
    """Excel sayısı -> düz metin: '8.69001234567E12' -> '8690012345670', '64.900000000000006' -> '64.9'"""
    try:
        f = float(text)
    except ValueError:
        return text
    if f.is_integer() and abs(f) < 1e17:
        return str(int(f))
    t = repr(round(f, 6))
    if "e" in t.lower():
        t = format(Decimal(t), "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def _read_xlsx(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportError_("Dosya açılamadı. Excel'de 'Farklı kaydet → Excel Çalışma Kitabı (.xlsx)' "
                           "ya da 'CSV' olarak kaydedip tekrar dener misin?") from None
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
    # ilk sayfa
    sheet_path = "xl/worksheets/sheet1.xml"
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        first = wb.find("m:sheets/m:sheet", ns)
        rid = first.get(f"{{{ns['r']}}}id") if first is not None else None
        if rid:
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            for rel in rels:
                if rel.get("Id") == rid:
                    target = rel.get("Target").lstrip("/")
                    sheet_path = target if target.startswith("xl/") else "xl/" + target
    except (KeyError, ET.ParseError):
        pass
    if sheet_path not in z.namelist():
        cands = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))
        if not cands:
            raise ImportError_("Excel dosyasında sayfa bulunamadı.")
        sheet_path = cands[0]
    root = ET.fromstring(z.read(sheet_path))
    rows = []
    for row in root.iter(f"{{{ns['m']}}}row"):
        cells = {}
        for c in row.findall("m:c", ns):
            ref = c.get("r") or ""
            col = 0
            for ch in re.match(r"[A-Z]*", ref).group(0):
                col = col * 26 + (ord(ch) - 64)
            col = (col - 1) if col else len(cells)
            t = c.get("t")
            v = c.find("m:v", ns)
            if t == "s" and v is not None:
                val = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
            elif t == "b":
                val = "1" if v is not None and v.text == "1" else "0"
            elif v is not None and v.text is not None:
                val = v.text if t == "str" else _plain_number(v.text)
            else:
                val = ""
            cells[col] = val.strip()
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
        if len(rows) > MAX_ROWS + 1:
            break
    return rows


def _read_csv(data):
    for enc in ("utf-8-sig", "cp1254", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:5000]
    delim = max((";", ",", "\t", "|"), key=lambda d: sample.count(d))
    rows = []
    for r in csv.reader(io.StringIO(text), delimiter=delim):
        rows.append([c.strip() for c in r])
        if len(rows) > MAX_ROWS + 1:
            break
    return rows


def read_table(filename, data):
    name = (filename or "").lower()
    if not data:
        raise ImportError_("Dosya boş görünüyor.")
    if name.endswith(".xls") and not data[:2] == b"PK":
        raise ImportError_("Eski Excel biçimi (.xls) okunamıyor. Excel'de 'Farklı kaydet → Excel Çalışma Kitabı "
                           "(.xlsx)' seçip tekrar yükler misin?")
    rows = _read_xlsx(data) if data[:2] == b"PK" else _read_csv(data)
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ImportError_("Dosyada satır bulunamadı.")
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


def guess_mapping(rows):
    """Döner: (başlık satırı var mı, {alan: sütun no})"""
    head = [_fold(c) for c in rows[0]]
    numeric_head = sum(1 for c in rows[0] if re.fullmatch(r"[\d.,]+", c or "")) >= max(1, len(rows[0]) // 2)
    mapping = {}
    if not numeric_head:
        used = set()
        for field in ("barcode", "price", "name", "category", "unit", "stock", "description"):
            best = None
            for hint in _HINTS[field]:
                for i, h in enumerate(head):
                    if i in used or not h:
                        continue
                    if any(a in h for a in _AVOID.get(field, ())):
                        continue
                    if h == hint or hint in h:
                        best = i
                        break
                if best is not None:
                    break
            if best is not None:
                mapping[field] = best
                used.add(best)
    if "name" not in mapping or "price" not in mapping:
        # başlıktan bulunamadıysa içerikten tahmin: en uzun yazılı sütun = ad, ondalıklı sayı = fiyat, 8-14 hane = barkod
        body = rows[1:21] if not numeric_head else rows[:20]
        width = len(rows[0])
        for i in range(width):
            vals = [r[i] for r in body if i < len(r) and r[i]]
            if not vals:
                continue
            if "barcode" not in mapping and all(re.fullmatch(r"\d{8,14}", v) for v in vals):
                mapping["barcode"] = i
            elif "price" not in mapping and all(re.fullmatch(r"\d{1,6}([.,]\d{1,2})?", v) for v in vals) \
                    and i not in mapping.values():
                mapping["price"] = i
        if "name" not in mapping:
            textcols = [(sum(len(r[i]) for r in body if i < len(r)), i) for i in range(width)
                        if i not in mapping.values() and any(re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", r[i]) for r in body
                                                             if i < len(r))]
            if textcols:
                mapping["name"] = max(textcols)[1]
    return (not numeric_head), mapping


def save_job(biz_id, filename, rows):
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("DELETE FROM import_jobs WHERE business_id = ? OR created_at < ?", (biz_id, cutoff))
    jid = db.insert("import_jobs", {"business_id": biz_id, "filename": (filename or "")[:120],
                                    "data": json.dumps(rows, ensure_ascii=False), "created_at": core.now()})
    db.commit()
    return jid


def load_job(biz_id, job_id):
    j = db.one("SELECT * FROM import_jobs WHERE id = ? AND business_id = ?", (job_id, biz_id))
    if not j:
        return None, None
    return j, json.loads(j["data"])


# ── Ürün kaydetme ───────────────────────────────────────────────────────────

def _clean_barcode(raw):
    b = re.sub(r"\D", "", str(raw or ""))
    return b if 6 <= len(b) <= 14 else ""


def _price(raw):
    raw = str(raw or "").strip().replace("₺", "").replace("TL", "").replace("tl", "").strip()
    if not raw:
        return None
    # Excel'den gelen '12.5' (nokta ondalık) ile Türkçe '1.250,50' ayrımı
    if re.fullmatch(r"\d+\.\d{1,2}", raw):
        raw = raw.replace(".", ",")
    try:
        v = core.money(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def _category_cache(biz_id):
    cats = {c["name"].strip().lower(): c["id"] for c in
            db.query("SELECT id, name FROM categories WHERE business_id = ?", (biz_id,))}
    pos = db.one("SELECT COALESCE(MAX(position), 0) AS m FROM categories WHERE business_id = ?", (biz_id,))
    state = {"pos": int(pos["m"])}

    def get(name):
        name = " ".join((name or "").split())[:60]
        if not name:
            return None
        key = name.lower()
        if key not in cats:
            state["pos"] += 1
            cats[key] = db.insert("categories", {"business_id": biz_id, "name": name, "position": state["pos"]})
        return cats[key]
    return get


def _nk(name, unit=None):
    """Ad eşleştirme anahtarı; birim verilirse 'Süt 1 L' ile 'Süt 1/2 L' ayrı ürün sayılır."""
    k = " ".join((name or "").lower().split())
    return k if unit is None else k + "|" + " ".join((unit or "").lower().split())


def _existing(biz_id):
    by_bc, by_name = {}, {}
    for p in db.query("SELECT id, name, unit, barcode, price, stock FROM products WHERE business_id = ?", (biz_id,)):
        if p["barcode"]:
            by_bc[p["barcode"]] = p
        by_name.setdefault(_nk(p["name"]), p)
        by_name.setdefault(_nk(p["name"], p["unit"]), p)
    return by_bc, by_name


def import_rows(biz_id, rows, mapping, has_header=True, update_existing=True, hide_new=False):
    """Döner: {"added", "updated", "same", "skipped": [(satır no, neden)]}"""
    if "name" not in mapping and "barcode" not in mapping:
        raise ImportError_("Ürün adı sütununu seçmen gerekiyor.")
    if "price" not in mapping:
        raise ImportError_("Satış fiyatı sütununu seçmen gerekiyor.")
    body = rows[1:] if has_header else rows
    if len(body) > MAX_ROWS:
        raise ImportError_(f"Tek seferde en fazla {MAX_ROWS} ürün yüklenebilir; dosyayı bölüp yükler misin?")
    by_bc, by_name = _existing(biz_id)
    cat_of = _category_cache(biz_id)
    pos = int(db.one("SELECT COALESCE(MAX(position), 0) AS m FROM products WHERE business_id = ?", (biz_id,))["m"])
    res = {"added": 0, "updated": 0, "same": 0, "skipped": []}

    def col(r, f):
        i = mapping.get(f)
        return r[i].strip() if i is not None and i < len(r) else ""
    try:
        for n, r in enumerate(body, start=2 if has_header else 1):
            name = " ".join(col(r, "name").split())[:100]
            bc = _clean_barcode(col(r, "barcode"))
            price = _price(col(r, "price"))
            if not name and bc:
                cat = catalog_get(bc)
                name = cat["name"] if cat else ""
            if not name:
                res["skipped"].append((n, "ürün adı boş"))
                continue
            if price is None:
                res["skipped"].append((n, f"{name}: fiyat okunamadı"))
                continue
            stock = None
            if "stock" in mapping:
                st = col(r, "stock").replace(",", ".")
                try:
                    stock = max(0, int(Decimal(st))) if st else None
                except InvalidOperation:
                    stock = None
            hit = by_bc.get(bc) if bc else None
            if not hit and not bc:
                hit = by_name.get(_nk(name, col(r, "unit")) if "unit" in mapping else _nk(name))
            if hit:
                if not update_existing:
                    res["same"] += 1
                    continue
                changes = {}
                if core.money(hit["price"]) != price:
                    changes["price"] = str(price)
                if bc and not hit["barcode"]:
                    changes["barcode"] = bc
                if changes:
                    db.update("products", hit["id"], changes)
                    res["updated"] += 1
                else:
                    res["same"] += 1
                if "stock" in mapping and stock is not None and hit["stock"] != stock:
                    core.adjust_stock(biz_id, hit["id"], None, "set", stock, "Excel'den sayım", None, commit=False)
                continue
            pos += 1
            unit = col(r, "unit")[:40]
            cat = catalog_get(bc) if bc else None
            pid = db.insert("products", {
                "business_id": biz_id, "name": name, "price": str(price), "unit": unit or (cat["unit"] if cat else ""),
                "description": col(r, "description")[:400], "category_id": cat_of(col(r, "category") or
                                                                                   (cat["category"] if cat else "")),
                "barcode": bc or None, "image_id": cat["image_id"] if cat else None, "stock": stock,
                "available": 1, "visible": 0 if hide_new else 1, "position": pos, "created_at": core.now()})
            res["added"] += 1
            p = {"id": pid, "name": name, "barcode": bc, "price": str(price), "stock": stock}
            if bc:
                by_bc[bc] = p
                catalog_learn(bc, name, unit, col(r, "category"), None, commit=False)
            by_name.setdefault(_nk(name), p)
            by_name.setdefault(_nk(name, unit), p)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return res


def save_simple(biz_id, items, source="quick"):
    """items: [{name, price, unit, category, barcode, stock}] — hızlı ekleme / başlangıç listesi.
    Barkodu (yoksa adı) aynı olan ürün varsa fiyatı güncellenir. Döner: (eklenen, güncellenen, hatalar)"""
    by_bc, by_name = _existing(biz_id)
    cat_of = _category_cache(biz_id)
    pos = int(db.one("SELECT COALESCE(MAX(position), 0) AS m FROM products WHERE business_id = ?", (biz_id,))["m"])
    added = updated = 0
    errors = []
    try:
        for i, it in enumerate(items, start=1):
            name = " ".join(str(it.get("name") or "").split())[:100]
            bc = _clean_barcode(it.get("barcode"))
            raw_price = str(it.get("price") or "").strip()
            if not name and not raw_price and not bc:
                continue
            if not name:
                errors.append(f"{i}. satır: ürün adı eksik")
                continue
            price = _price(raw_price)
            if price is None:
                errors.append(f"{name}: fiyat eksik ya da hatalı")
                continue
            stock = None
            if str(it.get("stock") or "").strip():
                try:
                    stock = max(0, int(str(it["stock"]).strip()))
                except ValueError:
                    errors.append(f"{name}: stok tam sayı olmalı")
                    continue
            unit_in = str(it.get("unit") or "").strip()
            hit = (by_bc.get(bc) if bc else None) or (by_name.get(_nk(name, unit_in)) if not bc else None)
            if hit:
                ch = {"price": str(price)}
                if bc and not hit.get("barcode"):
                    ch["barcode"] = bc
                db.update("products", hit["id"], ch)
                if stock is not None:
                    core.adjust_stock(biz_id, hit["id"], None, "set", stock, "Hızlı ekleme", None, commit=False)
                updated += 1
                continue
            cat = catalog_get(bc) if bc else None
            unit = str(it.get("unit") or "").strip()[:40] or (cat["unit"] if cat else "")
            category = str(it.get("category") or "").strip() or (cat["category"] if cat else "")
            pos += 1
            pid = db.insert("products", {
                "business_id": biz_id, "name": name, "price": str(price), "unit": unit,
                "category_id": cat_of(category), "barcode": bc or None,
                "image_id": it.get("image_id") or (cat["image_id"] if cat else None), "stock": stock,
                "available": 1, "visible": 1, "position": pos, "created_at": core.now()})
            p = {"id": pid, "name": name, "barcode": bc}
            if bc:
                by_bc[bc] = p
                catalog_learn(bc, name, unit, category, it.get("image_id"), commit=False)
            by_name[_nk(name, unit)] = p
            added += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return added, updated, errors


# ── Ortak katalog ───────────────────────────────────────────────────────────

def catalog_get(barcode):
    bc = _clean_barcode(barcode)
    return db.one("SELECT * FROM catalog_items WHERE barcode = ?", (bc,)) if bc else None


def catalog_learn(barcode, name, unit="", category="", image_id=None, source="shop", brand="", commit=True):
    """İşletmelerin girdiği barkodlu ürünleri ortak kataloğa ekler (varsa eksiklerini tamamlar)."""
    bc = _clean_barcode(barcode)
    name = " ".join((name or "").split())[:100]
    if not bc or len(name) < 2:
        return
    row = catalog_get(bc)
    if row:
        ch = {"uses": int(row["uses"]) + 1, "updated_at": core.now()}
        if not row["unit"] and unit:
            ch["unit"] = unit[:40]
        if not row["category"] and category:
            ch["category"] = category[:60]
        if not row["image_id"] and image_id:
            ch["image_id"] = image_id
        if row["source"] == "off" and source == "shop":     # işletmenin yazdığı Türkçe ad daha iyi
            ch["name"], ch["source"] = name, "shop"
        db.update("catalog_items", row["id"], ch)
    else:
        db.insert("catalog_items", {"barcode": bc, "name": name, "unit": (unit or "")[:40],
                                    "category": (category or "")[:60], "brand": (brand or "")[:60],
                                    "image_id": image_id, "source": source, "uses": 1, "updated_at": core.now()})
    if commit:
        db.commit()


def off_lookup(barcode, timeout=4):
    """Open Food Facts'te barkod ara (ücretsiz, açık veritabanı). Bulamazsa ya da erişilemezse None."""
    bc = _clean_barcode(barcode)
    if not bc:
        return None
    try:
        import requests
        r = requests.get(f"https://world.openfoodfacts.org/api/v2/product/{bc}.json",
                         params={"fields": "product_name,product_name_tr,brands,quantity,categories_tags"},
                         headers={"User-Agent": "SiparisQR/1.0 (market urun girisi)"}, timeout=timeout)
        if r.status_code != 200:
            return None
        js = r.json()
        if js.get("status") != 1:
            return None
        p = js.get("product") or {}
        name = (p.get("product_name_tr") or p.get("product_name") or "").strip()
        if not name:
            return None
        brand = (p.get("brands") or "").split(",")[0].strip()
        if brand and brand.lower() not in name.lower():
            name = f"{brand} {name}"
        return {"name": name[:100], "unit": (p.get("quantity") or "").strip()[:40], "brand": brand}
    except Exception:
        return None


def lookup(biz_id, barcode, remote=True):
    """Barkod okutulunca: bu işletmede var mı, yoksa ortak katalogda / Open Food Facts'te ne biliniyor."""
    bc = _clean_barcode(barcode)
    out = {"barcode": bc, "existing": None, "suggest": None}
    if not bc:
        return out
    p = db.one("SELECT * FROM products WHERE business_id = ? AND barcode = ?", (biz_id, bc))
    if p:
        out["existing"] = {"id": p["id"], "name": p["name"], "price": str(core.money(p["price"])), "unit": p["unit"],
                           "stock": p["stock"], "image_id": p["image_id"], "category_id": p["category_id"]}
        return out
    c = catalog_get(bc)
    if not c and remote:
        o = off_lookup(bc)
        if o:
            catalog_learn(bc, o["name"], o["unit"], "", None, source="off", brand=o["brand"])
            c = catalog_get(bc)
    if c:
        out["suggest"] = {"name": c["name"], "unit": c["unit"], "category": c["category"], "image_id": c["image_id"],
                          "source": c["source"]}
    return out
