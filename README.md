# QR Sipariş

Çok işletmeli QR / online sipariş sistemi. Her işletmenin kendi ürünleri, IBAN'ı, kampanyası ve (isteğe bağlı) kendi domaini olur.

- **Müşteri:** QR'ı okutur → ürünleri seçer → ad, telefon, adres → nakit ya da IBAN → sipariş. Durum sayfasında onay, IBAN bilgisi ve sadakat kartı (damgalar) canlı güncellenir.
- **İşletme:** Sipariş gelince telefona bildirim (Web Push) düşer → onayla / reddet → yolda → teslim. Ödeme alındı işareti, müşteriye not, ödül verme.
- **Kampanya:** "Her N siparişe X hediye". Müşteri telefon numarasıyla tanınır; onaylanan siparişler sayılır, iptal/red sayılmaz, isteğe bağlı en az tutar.
- **Yönetici (sen):** İşletme aç, domain bağla, hesabı aç/kapat, şifre sıfırla.

Teknik: Flask, PostgreSQL (yerelde SQLite), harici kütüphanesiz Web Push (RFC 8291/8292). Kurulum için `DEPLOY.md`.
