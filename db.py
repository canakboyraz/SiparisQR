"""Küçük veritabanı katmanı: yerelde SQLite, Railway'de PostgreSQL.

Sorgular `?` yer tutucusuyla yazılır; PostgreSQL'de otomatik `%s`'e çevrilir.
Satırlar her zaman dict olarak döner. SQL'lerde `%` karakteri kullanılmaz.
"""
import os
import sqlite3
import threading

_local = threading.local()
_init_lock = threading.Lock()
_initialized = set()


def _url():
    return os.environ.get("DATABASE_URL", "sqlite:///qrsiparis.db")


def is_pg(url=None):
    url = url or _url()
    return url.startswith("postgres://") or url.startswith("postgresql://")


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS businesses (
  id {pk},
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  custom_domain TEXT UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  tagline TEXT NOT NULL DEFAULT '',
  accent TEXT NOT NULL DEFAULT '#2F5D50',
  logo_id INTEGER,
  phone TEXT NOT NULL DEFAULT '',
  whatsapp TEXT NOT NULL DEFAULT '',
  ordering_open INTEGER NOT NULL DEFAULT 1,
  closed_message TEXT NOT NULL DEFAULT '',
  pay_cash INTEGER NOT NULL DEFAULT 1,
  pay_iban INTEGER NOT NULL DEFAULT 1,
  iban TEXT NOT NULL DEFAULT '',
  iban_holder TEXT NOT NULL DEFAULT '',
  iban_bank TEXT NOT NULL DEFAULT '',
  delivery INTEGER NOT NULL DEFAULT 1,
  pickup INTEGER NOT NULL DEFAULT 1,
  delivery_fee TEXT NOT NULL DEFAULT '0',
  free_delivery_over TEXT NOT NULL DEFAULT '0',
  min_order TEXT NOT NULL DEFAULT '0',
  delivery_note TEXT NOT NULL DEFAULT '',
  pickup_address TEXT NOT NULL DEFAULT '',
  reward_active INTEGER NOT NULL DEFAULT 0,
  reward_every INTEGER NOT NULL DEFAULT 6,
  reward_text TEXT NOT NULL DEFAULT '',
  reward_min_total TEXT NOT NULL DEFAULT '0',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id {pk},
  business_id INTEGER,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
  id {pk},
  business_id INTEGER NOT NULL,
  mime TEXT NOT NULL,
  data {blob} NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
  id {pk},
  business_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS products (
  id {pk},
  business_id INTEGER NOT NULL,
  category_id INTEGER,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL DEFAULT '',
  price TEXT NOT NULL,
  image_id INTEGER,
  available INTEGER NOT NULL DEFAULT 1,
  visible INTEGER NOT NULL DEFAULT 1,
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
  id {pk},
  business_id INTEGER NOT NULL,
  phone TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (business_id, phone)
);
CREATE TABLE IF NOT EXISTS orders (
  id {pk},
  business_id INTEGER NOT NULL,
  customer_id INTEGER NOT NULL,
  token TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  payment TEXT NOT NULL,
  paid INTEGER NOT NULL DEFAULT 0,
  fulfilment TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  customer_phone TEXT NOT NULL,
  address TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  subtotal TEXT NOT NULL,
  delivery_fee TEXT NOT NULL DEFAULT '0',
  total TEXT NOT NULL,
  counts_for_reward INTEGER NOT NULL DEFAULT 0,
  owner_message TEXT NOT NULL DEFAULT '',
  reject_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  approved_at TEXT
);
CREATE TABLE IF NOT EXISTS order_lines (
  id {pk},
  order_id INTEGER NOT NULL,
  product_id INTEGER,
  name TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '',
  price TEXT NOT NULL,
  qty INTEGER NOT NULL,
  line_total TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rewards (
  id {pk},
  business_id INTEGER NOT NULL,
  customer_id INTEGER NOT NULL,
  earned_order_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'earned',
  given_order_id INTEGER,
  created_at TEXT NOT NULL,
  given_at TEXT
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id {pk},
  user_id INTEGER NOT NULL,
  business_id INTEGER NOT NULL,
  endpoint TEXT NOT NULL UNIQUE,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  user_agent TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  id {pk},
  business_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  method TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_by INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_payments_order ON payments (order_id);
CREATE INDEX IF NOT EXISTS ix_orders_biz ON orders (business_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_cust ON orders (customer_id);
CREATE INDEX IF NOT EXISTS ix_products_biz ON products (business_id);
CREATE INDEX IF NOT EXISTS ix_rewards_cust ON rewards (customer_id, status);
"""


# Sonradan eklenen sütunlar (eski veritabanlarına otomatik eklenir)
MIGRATIONS = [
    ("orders", "internal_note", "TEXT NOT NULL DEFAULT ''"),
    ("orders", "paid_cents", "INTEGER NOT NULL DEFAULT 0"),
]


def _connect():
    url = _url()
    if is_pg(url):
        import psycopg2
        import psycopg2.extras
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        return conn
    path = url.replace("sqlite:///", "", 1)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def conn():
    key = _url()
    c = getattr(_local, "conn", None)
    if c is not None and getattr(c, "closed", 0):   # psycopg2: bağlantı koptuysa yeniden aç
        c = None
    if c is None or getattr(_local, "key", None) != key:
        c = _connect()
        _local.conn, _local.key = c, key
    return c


def close():
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.close()
        finally:
            _local.conn = None


def _sql(sql):
    return sql.replace("?", "%s") if is_pg() else sql


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k, v in d.items():
        if isinstance(v, memoryview):
            d[k] = bytes(v)
    return d


def query(sql, params=()):
    cur = conn().cursor()
    cur.execute(_sql(sql), tuple(params))
    rows = cur.fetchall() if cur.description else []
    return [_row(r) for r in rows]


def one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    """INSERT ... RETURNING id dahil her türlü yazma. Döner: RETURNING varsa ilk satır, yoksa None."""
    cur = conn().cursor()
    cur.execute(_sql(sql), tuple(params))
    row = cur.fetchone() if cur.description else None
    return _row(row)


def insert(table, values):
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    row = execute(f"INSERT INTO {table} ({cols}) VALUES ({marks}) RETURNING id", list(values.values()))
    return row["id"]


def update(table, row_id, values):
    if not values:
        return
    sets = ", ".join(f"{k} = ?" for k in values)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id])


def commit():
    conn().commit()


def rollback():
    try:
        conn().rollback()
    except Exception:
        pass


def blob(data):
    if is_pg():
        import psycopg2
        return psycopg2.Binary(data)
    return sqlite3.Binary(data)


def init():
    key = _url()
    with _init_lock:
        if key in _initialized:
            return
        pg = is_pg(key)
        ddl = SCHEMA.format(pk="SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT",
                            blob="BYTEA" if pg else "BLOB")
        c = conn()
        cur = c.cursor()
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            cur.execute(stmt)
        c.commit()
        for table, col, ddl_col in MIGRATIONS:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_col}")
                c.commit()
            except Exception:      # sütun zaten var
                c.rollback()
                cur = c.cursor()
        _initialized.add(key)


def get_setting(key, default=None):
    r = one("SELECT value FROM settings WHERE key = ?", (key,))
    return r["value"] if r else default


def set_setting(key, value):
    if get_setting(key) is None:
        execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    else:
        execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
