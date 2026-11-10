# Kurulum (GitHub + Railway)

## 1) GitHub
Yeni, **boş** bir repo aç (ör. `qrsiparis`) ve bu klasörü gönder:
```bash
git init && git add . && git commit -m "QR Sipariş ilk sürüm"
git branch -M main
git remote add origin https://github.com/<KULLANICI>/qrsiparis.git
git push -u origin main
```

## 2) Railway
1. **New Project → Deploy from GitHub repo** → `qrsiparis`.
2. Aynı projede **New → Database → PostgreSQL**. Servisin Variables kısmına `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` ekle.
3. Servis **Variables**:

| Değişken | Örnek | Açıklama |
|---|---|---|
| `PLATFORM_NAME` | `QR Sipariş` | Ekranlarda görünen ürün adı |
| `PLATFORM_HOSTS` | `qrsiparis.com,www.qrsiparis.com,xxx.up.railway.app` | Platformun kendi adresleri (işletme domainleri **yazılmaz**) |
| `PUBLIC_BASE_URL` | `https://qrsiparis.com` | Domaini olmayan işletmelerin QR linki bununla başlar |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | | Senin yönetici girişin (ilk açılışta oluşur) |
| `PUSH_CONTACT_EMAIL` | `sen@...` | Bildirim servislerine iletişim adresi |

`SECRET_KEY` ve bildirim (VAPID) anahtarı girmezsen ilk açılışta üretilip veritabanına yazılır.

4. **Settings → Networking → Generate Domain** (ya da platform domainini bağla).
5. `https://<adres>/login` → admin girişi → **Yeni işletme**.

## 3) İşletmeye kendi domainini bağlama
1. Yönetim → işletme → **Kendi domaini** alanına `egezeytin.com` yaz, kaydet.
2. Railway → servis → **Settings → Networking → Custom Domain** → `egezeytin.com` ve `www.egezeytin.com` ekle.
3. Domain firmasının DNS panelinde: `www` → **CNAME** → Railway'in verdiği adres. Kök (`@`) için CNAME/ALIAS yoksa Cloudflare'e taşı ya da kökü `www`'ye yönlendir.
4. Railway'de yeşil tik çıkınca SSL otomatik açılır.

## 4) İşletme sahibine verilecek ilk adımlar
1. `/login` ile gir → **Ayarlar**: IBAN, teslimat ücreti, telefon.
2. **Ürünler**: ürün + fotoğraf ekle.
3. **Kampanya**: "Her 6 siparişe 1 kg zeytin" gibi kuralı aç.
4. **Bildirimler**: telefonda bildirimi aç. iPhone'da önce Safari → Paylaş → **Ana Ekrana Ekle**, sonra o simgeden açıp bildirimi aç (iOS 16.4+).
5. **QR kod**: afişi yazdır.

## Yerelde deneme
```bash
pip install -r requirements.txt
cp .env.example .env
python demo.py           # örnek "Ege Zeytin" mağazası
flask --app wsgi run     # http://localhost:5000/s/ege-zeytin/  · panel: zeytin@ornek.com / zeytin123
python -m pytest -q      # testler
```
