# ================================================================
# app.py --- Gear Swamp（Supabase/Postgres版・高速化フルセット＋改善版）
#
# ✅ 機能は維持（在庫/貸出/予約/掲示板/履歴/メンバー/CSV/バックアップ/QR）
# ✅ 改善点（最小侵襲でスリープ復帰に強くする）
# - 起動時にDB ping（軽い）→OKの時だけ重い初期化/修復を実行
# - ping NG なら「修復ボタン」を表示して待機（白画面で死なない）
# - スマホでも「修復ボタン」を押せば復帰できる（PC必須ではない）
# - SERIAL/IDENTITY のシーケンス自己修復（duplicate key対策）
# - reservations.position 焼き直し（崩れ復旧）
# - members席は generate_series + ON CONFLICT（高速・安定）
# - 在庫登録はフォーム化＋画像アップロード堅牢化（getvalue→BytesIO→PIL）
# - CSSはライト/ダーク両対応（読めない問題を改善）
# - cache_dataにBYTEA(photo=memoryview)を入れない（従来の方針維持）
# ================================================================

import csv
import html
import base64
import traceback
from io import BytesIO, StringIO
from contextlib import contextmanager
from datetime import date, timedelta, datetime
from urllib.parse import quote

import qrcode
import streamlit as st
from PIL import Image, UnidentifiedImageError
from dateutil.parser import parse as dt_parse
import psycopg2
from psycopg2.pool import SimpleConnectionPool


# ================================================================
# 設定
# ================================================================
MAX_MEMBERS = int(st.secrets.get("max_members", 6))
SHARED_PASSCODE = st.secrets.get("passcode", "1234")
INVITE_CODE = st.secrets.get("invite_code", "join-123")
ADMIN_USERS = set(st.secrets.get("admin_users") or [])

MAX_RESERVATIONS_PER_ITEM = 3

TTL_ITEMS = 10
TTL_RESERVATIONS = 10
TTL_POSTS = 10
TTL_LOANS = 10
TTL_MEMBERS = 30
TTL_PHOTOS = 30

CATEGORIES = [
    "フレーム/フォーク", "ヘッドセット", "ハンドル/ステム", "グリップ/バーテープ",
    "サドル/シートポスト", "ホイール", "ハブ/リム/スポーク", "タイヤ/チューブ",
    "ブレーキ（リム/ディスク）", "ローター/パッド", "シフター/ブレーキレバー",
    "ディレイラー（F/R）", "クランク/BB", "スプロケット/コグ",
    "チェーン/チェーンリング", "ペダル", "ケーブル/アウター", "小物/ツール", "その他"
]
POST_TYPES = ["試乗希望", "貸してほしい", "貸します", "譲ります", "雑談"]

st.set_page_config(page_title="Gear Swamp", page_icon="icon_gearswamp.png", layout="wide")


# ================================================================
# SAFE BOOT（白画面回避：最初に必ず見える表示）
# ================================================================
st.markdown(
    """
    <div style="padding:12px;border-radius:10px;border:1px solid rgba(0,0,0,0.15);
                background:rgba(255,255,255,0.92);color:#111;">
      <div style="font-weight:800;font-size:16px;">Gear Swamp 起動中…</div>
      <div style="font-size:12px;opacity:0.75;">スリープ復帰直後は重い処理を遅延します</div>
    </div>
    """,
    unsafe_allow_html=True,
)
st.caption(f"boot_ts: {datetime.now().isoformat(timespec='seconds')}")


# ================================================================
# DB: 接続プール
# ================================================================
@st.cache_resource(show_spinner=False)
def get_pool() -> SimpleConnectionPool:
    cfg = st.secrets["postgres"]
    minconn = int(st.secrets.get("pg_pool_minconn", 1))
    maxconn = int(st.secrets.get("pg_pool_maxconn", 8))
    return SimpleConnectionPool(
        minconn=minconn,
        maxconn=maxconn,
        host=cfg["host"],
        port=int(cfg["port"]),
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
        connect_timeout=10,
        sslmode="require",
    )

@contextmanager
def get_conn():
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)

def db_exec(sql: str, params: tuple = ()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()

def db_fetchall(sql: str, params: tuple = ()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

def db_fetchone(sql: str, params: tuple = ()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

def db_ping() -> tuple[bool, str]:
    """スリープ復帰直後の軽量チェック（重い初期化はしない）"""
    try:
        r = db_fetchone("SELECT 1")
        return (bool(r), "ok")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


# ================================================================
# キャッシュクリア（更新後に呼ぶ）
# ================================================================
def clear_read_caches():
    list_items_cached.clear()
    reservations_map_cached.clear()
    posts_cached.clear()
    loans_cached.clear()
    member_slots_cached.clear()
    photo_map_cached.clear()


# ================================================================
# 初期化耐性（スキーマ保証＋移行＋シーケンス自己修復＋予約position焼き直し）
# ================================================================
@st.cache_resource(show_spinner=False)
def ensure_schema_and_migrate_once(max_members: int):
    with get_conn() as conn:
        with conn.cursor() as c:
            c.execute("""
            CREATE TABLE IF NOT EXISTS members(
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE,
                insta TEXT,
                is_active BOOLEAN DEFAULT FALSE,
                created_at TEXT
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS items(
                id SERIAL PRIMARY KEY,
                name TEXT,
                category TEXT,
                size TEXT,
                condition TEXT,
                owner TEXT,
                owner_id INTEGER,
                location TEXT,
                note TEXT,
                status TEXT DEFAULT '在庫あり',
                photo BYTEA
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS loans(
                id SERIAL PRIMARY KEY,
                item_id INTEGER,
                borrower TEXT,
                borrower_id INTEGER,
                start_date TEXT,
                due_date TEXT,
                reminder_days INTEGER,
                last_notified TEXT,
                returned_date TEXT,
                status TEXT DEFAULT '貸出中'
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS reservations(
                id SERIAL PRIMARY KEY,
                item_id INTEGER,
                reserver TEXT,
                reserver_id INTEGER,
                position INTEGER,
                reserved_date TEXT
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS posts(
                id SERIAL PRIMARY KEY,
                author TEXT,
                author_id INTEGER,
                ptype TEXT,
                category TEXT,
                title TEXT,
                body TEXT,
                created TEXT
            )""")

            # 旧DB追従
            c.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS owner_id INTEGER")
            c.execute("ALTER TABLE loans ADD COLUMN IF NOT EXISTS borrower_id INTEGER")
            c.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS reserver_id INTEGER")
            c.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS author_id INTEGER")
            c.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS position INTEGER")
            c.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS reserved_date TEXT")

            # Index
            c.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_items_owner_id ON items(owner_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_resv_item_pos ON reservations(item_id, position)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_loans_item_status ON loans(item_id, status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_posts_id_desc ON posts(id DESC)")

            # ✅ 席（generate_series で一括）
            c.execute(
                """
                INSERT INTO members(id, name, insta, is_active, created_at)
                SELECT gs, NULL, NULL, FALSE, %s
                FROM generate_series(1, %s) AS gs
                ON CONFLICT (id) DO NOTHING
                """,
                (datetime.now().isoformat(timespec="seconds"), int(max_members))
            )

            # バックフィル（厳密一致）
            c.execute("""
            UPDATE items i
            SET owner_id = m.id
            FROM members m
            WHERE i.owner_id IS NULL AND i.owner IS NOT NULL AND m.name IS NOT NULL AND i.owner = m.name
            """)
            c.execute("""
            UPDATE loans l
            SET borrower_id = m.id
            FROM members m
            WHERE l.borrower_id IS NULL AND l.borrower IS NOT NULL AND m.name IS NOT NULL AND l.borrower = m.name
            """)
            c.execute("""
            UPDATE reservations r
            SET reserver_id = m.id
            FROM members m
            WHERE r.reserver_id IS NULL AND r.reserver IS NOT NULL AND m.name IS NOT NULL AND r.reserver = m.name
            """)
            c.execute("""
            UPDATE posts p
            SET author_id = m.id
            FROM members m
            WHERE p.author_id IS NULL AND p.author IS NOT NULL AND m.name IS NOT NULL AND p.author = m.name
            """)

            # ✅ 重要：SERIALシーケンス自己修復（duplicate key 恒久対策）
            c.execute("""
            DO $$
            DECLARE
              seq TEXT;
            BEGIN
              seq := pg_get_serial_sequence('items','id');
              IF seq IS NOT NULL THEN
                EXECUTE format('SELECT setval(%L, COALESCE((SELECT MAX(id) FROM items),0)+1, false)', seq);
              END IF;

              seq := pg_get_serial_sequence('loans','id');
              IF seq IS NOT NULL THEN
                EXECUTE format('SELECT setval(%L, COALESCE((SELECT MAX(id) FROM loans),0)+1, false)', seq);
              END IF;

              seq := pg_get_serial_sequence('reservations','id');
              IF seq IS NOT NULL THEN
                EXECUTE format('SELECT setval(%L, COALESCE((SELECT MAX(id) FROM reservations),0)+1, false)', seq);
              END IF;

              seq := pg_get_serial_sequence('posts','id');
              IF seq IS NOT NULL THEN
                EXECUTE format('SELECT setval(%L, COALESCE((SELECT MAX(id) FROM posts),0)+1, false)', seq);
              END IF;
            END $$;
            """)

            # ✅ reservations.position 焼き直し（NULL/重複/抜けを 1..n に整列）
            c.execute("""
            WITH ranked AS (
              SELECT
                id,
                item_id,
                ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY COALESCE(position, 9999), id) AS new_pos
              FROM reservations
            )
            UPDATE reservations r
            SET position = ranked.new_pos
            FROM ranked
            WHERE r.id = ranked.id
              AND (r.position IS DISTINCT FROM ranked.new_pos);
            """)

            # 上限超過は削除（念のため）
            c.execute(
                "DELETE FROM reservations WHERE position > %s",
                (MAX_RESERVATIONS_PER_ITEM,)
            )

        conn.commit()
    return True


# ================================================================
# 背景CSS（ライト/ダーク両対応）
# ================================================================
def set_background(image_path: str):
    try:
        with open(image_path, "rb") as f:
            data = f.read()
        encoded = base64.b64encode(data).decode("utf-8")
    except Exception:
        encoded = None

    bg = ""
    if encoded:
        bg = f"""
        .stApp {{
            background: url("data:image/png;base64,{encoded}") no-repeat center center fixed;
            background-size: cover;
        }}
        """

    st.markdown(
        f"""
        <style>
        {bg}
        .stApp > div {{ background-color: rgba(0,0,0,0.40); }}

        :root {{
          --gs-text: #f5f5f5;
          --gs-panel: rgba(0,0,0,0.80);
          --gs-input-bg: #222;
          --gs-input-fg: #f5f5f5;
          --gs-border: #555;
          --gs-muted: #aaa;
          --gs-accent: #ff4b4b;
        }}
        @media (prefers-color-scheme: light) {{
          :root {{
            --gs-text: #111;
            --gs-panel: rgba(255,255,255,0.86);
            --gs-input-bg: #ffffff;
            --gs-input-fg: #111111;
            --gs-border: #cfcfcf;
            --gs-muted: #666;
          }}
          .stApp > div {{ background-color: rgba(255,255,255,0.55); }}
        }}

        html, body {{ color: var(--gs-text) !important; }}
        section[data-testid="stSidebar"] {{ background-color: var(--gs-panel) !important; }}

        .stApp input, .stApp textarea, .stApp select {{
            color: var(--gs-input-fg) !important;
            background-color: var(--gs-input-bg) !important;
            border: 1px solid var(--gs-border) !important;
        }}
        .stApp ::placeholder {{ color: var(--gs-muted) !important; opacity: 1 !important; }}

        .stApp div[data-baseweb="select"] > div {{
          background-color: var(--gs-input-bg) !important;
          color: var(--gs-input-fg) !important;
          border-color: var(--gs-border) !important;
        }}
        .stApp div[data-baseweb="select"] span {{ color: var(--gs-input-fg) !important; }}

        .stApp button[role="tab"] {{
            border-radius: 18px 18px 0 0 !important;
            background-color: rgba(20,20,20,0.85) !important;
            color: #dddddd !important;
            border: 1px solid rgba(255,255,255,0.18) !important;
            padding: 0.5rem 1.2rem !important;
            font-weight: 700 !important;
        }}
        @media (prefers-color-scheme: light) {{
          .stApp button[role="tab"] {{
            background-color: rgba(255,255,255,0.95) !important;
            color: #111 !important;
            border: 1px solid rgba(0,0,0,0.18) !important;
          }}
        }}
        .stApp button[role="tab"][aria-selected="true"] {{
            background: linear-gradient(135deg, #ff6b6b, var(--gs-accent)) !important;
            color: #ffffff !important;
            border-color: #ff8a8a !important;
        }}

        .stApp button {{
            background-color: rgba(40,40,40,0.85) !important;
            color: var(--gs-text) !important;
            border: 1px solid rgba(255,255,255,0.18) !important;
            border-radius: 8px !important;
        }}
        @media (prefers-color-scheme: light) {{
          .stApp button {{
            background-color: rgba(255,255,255,0.95) !important;
            border: 1px solid rgba(0,0,0,0.18) !important;
          }}
        }}

        .resv-badge {{
            display:inline-block;
            padding: 0.1rem 0.55rem;
            border-radius: 999px;
            border: 1px solid rgba(255,255,255,0.25);
            background: rgba(255,75,75,0.18);
            font-size: 0.8rem;
            margin-left: .4rem;
        }}

        .bbs-card {{
            background-color: var(--gs-panel);
            border-radius: 10px;
            padding: 0.8rem 1rem;
            margin-bottom: 0.35rem;
        }}
        .bbs-title {{ font-weight: 800; margin-bottom: .2rem; }}
        .bbs-meta  {{ font-size: 0.8rem; opacity: 0.85; margin-bottom: 0.4rem; }}
        .bbs-body  {{ font-size: 0.95rem; line-height: 1.5; white-space: pre-wrap; }}

        .stApp a, .stApp a:link, .stApp a:visited {{
          color: #8cc2ff !important; text-decoration: underline !important;
        }}
        .stApp a:hover {{ color: #c6e3ff !important; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# ================================================================
# 画像ユーティリティ
# ================================================================
def blob_to_img(blob, thumb_px=900):
    if not blob:
        return None
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    i = Image.open(BytesIO(blob))
    i.thumbnail((thumb_px, thumb_px))
    return i

def img_to_blob(img, max_px=1400):
    if not img:
        return None
    img = img.convert("RGB")
    img.thumbnail((max_px, max_px))
    b = BytesIO()
    img.save(b, format="JPEG", quality=85, optimize=True)
    return b.getvalue()

def file_to_blob(uploaded_file):
    if uploaded_file is None:
        return None
    raw = uploaded_file.getvalue()
    if not raw:
        return None
    try:
        img = Image.open(BytesIO(raw))
        return img_to_blob(img)
    except UnidentifiedImageError:
        return None
    except Exception:
        return None

def compute_due(start, days):
    try:
        s = dt_parse(start).date()
    except Exception:
        s = date.today()
    return s + timedelta(days=days)


# ================================================================
# members API
# ================================================================
def is_admin_name(display_name: str) -> bool:
    return bool(display_name) and display_name in ADMIN_USERS

def get_member(mid: int):
    r = db_fetchone("SELECT id, name, insta, is_active FROM members WHERE id=%s", (mid,))
    if not r:
        return None
    return {"id": int(r[0]), "name": r[1], "insta": r[2], "is_active": bool(r[3])}

@st.cache_data(ttl=TTL_MEMBERS, show_spinner=False)
def member_slots_cached(max_members: int):
    rows = db_fetchall("SELECT id, name, insta, is_active FROM members WHERE id <= %s ORDER BY id", (max_members,))
    return [{"id": int(mid), "name": name, "insta": insta, "is_active": bool(act)} for mid, name, insta, act in rows]

def update_member_profile(mid: int, name, insta, activate):
    if insta is not None:
        insta = (insta or "").strip().lstrip("@") or None
    if name is not None:
        name = (name or "").strip() or None

    sets, params = [], []
    if name is not None:
        sets.append("name=%s"); params.append(name)
    if insta is not None:
        sets.append("insta=%s"); params.append(insta)
    if activate is not None:
        sets.append("is_active=%s"); params.append(bool(activate))
    if not sets:
        return
    params.append(mid)
    db_exec(f"UPDATE members SET {', '.join(sets)} WHERE id=%s", tuple(params))
    clear_read_caches()

def get_insta_by_id(mid: int):
    r = db_fetchone("SELECT insta FROM members WHERE id=%s", (mid,))
    return r[0] if r and r[0] else None

def member_label(m):
    nm = m["name"] if m["name"] else "未登録"
    return f'{m["id"]} : {nm}'

def member_name_by_id(mid: int):
    r = db_fetchone("SELECT name FROM members WHERE id=%s", (mid,))
    return r[0] if r and r[0] else None


# ================================================================
# 予約：まとめ取り
# ================================================================
@st.cache_data(ttl=TTL_RESERVATIONS, show_spinner=False)
def reservations_map_cached(item_ids: tuple[int, ...]):
    if not item_ids:
        return {}
    placeholders = ",".join(["%s"] * len(item_ids))
    rows = db_fetchall(
        f"""
        SELECT
          r.item_id,
          r.position,
          COALESCE(m.name, r.reserver) AS reserver_name,
          r.reserver_id,
          r.reserved_date
        FROM reservations r
        LEFT JOIN members m ON r.reserver_id = m.id
        WHERE r.item_id IN ({placeholders})
        ORDER BY r.item_id ASC, r.position ASC, r.id ASC
        """,
        tuple(item_ids)
    )
    mp = {}
    for item_id, pos, nm, rid, rdt in rows:
        item_id = int(item_id)
        pos_i = int(pos) if pos is not None else 999
        rid_i = int(rid) if rid is not None else None
        mp.setdefault(item_id, []).append((pos_i, nm, rid_i, rdt))
    return mp

def can_reserve(item_id: int, reserver_id: int) -> tuple[bool, str]:
    r = db_fetchone("SELECT position FROM reservations WHERE item_id=%s AND reserver_id=%s", (item_id, reserver_id))
    if r:
        return False, f"すでに予約済み（{int(r[0])}番目）です。"
    r2 = db_fetchone("SELECT COUNT(*) FROM reservations WHERE item_id=%s", (item_id,))
    cnt = int(r2[0]) if r2 else 0
    if cnt >= MAX_RESERVATIONS_PER_ITEM:
        return False, "予約枠がいっぱいです。"
    return True, ""

def create_reservation(item_id: int, reserver_id: int, reserver_name: str):
    r = db_fetchone("SELECT COALESCE(MAX(position),0)+1 FROM reservations WHERE item_id=%s", (item_id,))
    pos = int(r[0]) if r else 1
    if pos > MAX_RESERVATIONS_PER_ITEM:
        raise ValueError("予約枠がいっぱいです")
    db_exec(
        "INSERT INTO reservations(item_id,reserver,reserver_id,position,reserved_date) VALUES(%s,%s,%s,%s,%s)",
        (item_id, reserver_name, reserver_id, pos, str(date.today()))
    )
    clear_read_caches()
    return pos

def cancel_reservation(item_id: int, reserver_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id, position FROM reservations WHERE item_id=%s AND reserver_id=%s", (item_id, reserver_id))
            row = c.fetchone()
            if not row:
                return False
            rid, my_pos = int(row[0]), int(row[1])
            c.execute("DELETE FROM reservations WHERE id=%s", (rid,))
            c.execute("UPDATE reservations SET position = position - 1 WHERE item_id=%s AND position > %s", (item_id, my_pos))
        conn.commit()
    clear_read_caches()
    return True


# ================================================================
# ★在庫一覧：photoを含めずにキャッシュ（pickle問題回避）
# ================================================================
@st.cache_data(ttl=TTL_ITEMS, show_spinner=False)
def list_items_cached(kw: str, f_cat: tuple[str, ...], f_owner: str, f_status: tuple[str, ...], show_arch: bool):
    q = """
    SELECT
      i.id, i.name, i.category, i.size, i.condition,
      COALESCE(om.name, i.owner) AS owner_name,
      i.owner_id,
      i.location, i.note, i.status,

      COALESCE(rc.cnt, 0) AS reservation_count,
      COALESCE(nm.name, nr.reserver) AS next_reserver_name,
      nr.position AS next_position

    FROM items i
    LEFT JOIN members om ON i.owner_id = om.id

    LEFT JOIN (
      SELECT item_id, COUNT(*) AS cnt
      FROM reservations
      GROUP BY item_id
    ) rc ON rc.item_id = i.id

    LEFT JOIN reservations nr
      ON nr.item_id = i.id AND nr.position = 1
    LEFT JOIN members nm
      ON nr.reserver_id = nm.id

    WHERE 1=1
    """
    p = []
    if kw:
        q += " AND (i.name ILIKE %s OR COALESCE(om.name, i.owner) ILIKE %s OR i.note ILIKE %s OR i.size ILIKE %s)"
        like = f"%{kw}%"
        p += [like, like, like, like]
    if f_cat:
        q += " AND i.category IN (" + ",".join(["%s"] * len(f_cat)) + ")"
        p += list(f_cat)
    if f_owner:
        q += " AND COALESCE(om.name, i.owner) ILIKE %s"
        p.append(f"%{f_owner}%")
    if f_status:
        q += " AND i.status IN (" + ",".join(["%s"] * len(f_status)) + ")"
        p += list(f_status)
    if not show_arch:
        q += " AND i.status <> 'アーカイブ'"
    q += " ORDER BY i.status DESC, i.category, i.name"
    return db_fetchall(q, tuple(p))


# ================================================================
# ★画像だけ別で取って bytes にしてキャッシュ（pickle可能）
# ================================================================
@st.cache_data(ttl=TTL_PHOTOS, show_spinner=False)
def photo_map_cached(item_ids: tuple[int, ...]):
    if not item_ids:
        return {}
    placeholders = ",".join(["%s"] * len(item_ids))
    rows = db_fetchall(
        f"SELECT id, photo FROM items WHERE id IN ({placeholders})",
        tuple(item_ids)
    )
    mp = {}
    for iid, blob in rows:
        if blob is None:
            mp[int(iid)] = None
        elif isinstance(blob, memoryview):
            mp[int(iid)] = blob.tobytes()
        elif isinstance(blob, (bytes, bytearray)):
            mp[int(iid)] = bytes(blob)
        else:
            mp[int(iid)] = None
    return mp


# ================================================================
# 掲示板/履歴キャッシュ
# ================================================================
@st.cache_data(ttl=TTL_POSTS, show_spinner=False)
def posts_cached(kw_b: str, f_type: tuple[str, ...], f_cat_b: tuple[str, ...], f_author: str):
    q = """
    SELECT
      p.id,
      COALESCE(m.name, p.author) AS author_name,
      p.ptype,
      p.category,
      p.title,
      p.body,
      p.created,
      p.author_id
    FROM posts p
    LEFT JOIN members m ON p.author_id = m.id
    WHERE 1=1
    """
    p = []
    if kw_b:
        q += " AND (p.title ILIKE %s OR p.body ILIKE %s)"
        like = f"%{kw_b}%"
        p += [like, like]
    if f_type:
        q += " AND p.ptype IN (" + ",".join(["%s"] * len(f_type)) + ")"
        p += list(f_type)
    if f_cat_b:
        q += " AND p.category IN (" + ",".join(["%s"] * len(f_cat_b)) + ")"
        p += list(f_cat_b)
    if f_author:
        q += " AND COALESCE(m.name, p.author) ILIKE %s"
        p.append(f"%{f_author}%")
    q += " ORDER BY p.id DESC"
    return db_fetchall(q, tuple(p))

@st.cache_data(ttl=TTL_LOANS, show_spinner=False)
def loans_cached():
    return db_fetchall(
        """
        SELECT
          i.name,
          COALESCE(m.name, l.borrower) AS borrower_name,
          l.start_date, l.due_date, l.returned_date, l.status
        FROM loans l
        LEFT JOIN items i ON l.item_id=i.id
        LEFT JOIN members m ON l.borrower_id = m.id
        ORDER BY l.id DESC
        """
    )


# ================================================================
# App本体
# ================================================================
def run_app():
    # まずはCSSだけ（軽い）
    set_background("bg_gearswamp.png")

    # --- スリープ復帰対策：最初は軽い ping だけ ---
    st.session_state.setdefault("_schema_ready", False)

    ok, msg = db_ping()
    with st.sidebar:
        st.subheader("復帰状態")
        st.caption(f"DB ping: {'OK' if ok else 'NG'}")
        if not ok:
            st.warning("スリープ復帰直後はDBが起きていない場合があります。")
            st.code(msg)

        st.divider()
        st.subheader("初期化/修復（オーブン）")
        st.caption("白画面/登録失敗/予約崩れが出たら押す（スマホでもOK）")
        if st.button("DB初期化/修復を実行"):
            ensure_schema_and_migrate_once(MAX_MEMBERS)
            st.session_state["_schema_ready"] = True
            clear_read_caches()
            st.success("修復が完了しました。")
            st.rerun()

    if not ok:
        st.info("DBがまだ起床していません。少し待って再読み込みするか、右の修復ボタンを押して。")
        st.stop()

    # ping OK の時だけ初期化（ただし毎回はやらない：スリープ復帰で重くしない）
    if not st.session_state.get("_schema_ready", False):
        ensure_schema_and_migrate_once(MAX_MEMBERS)
        st.session_state["_schema_ready"] = True

    # 以降は元の動き（ほぼそのまま）
    st.session_state.setdefault("authed", False)
    st.session_state.setdefault("member_id", None)
    st.session_state.setdefault("member_name", "")
    st.session_state.setdefault("insta_input", "")
    st.session_state.setdefault("last_borrowed_item_id", None)

    # --- Sidebar 認証 ---
    slots = member_slots_cached(MAX_MEMBERS)
    label_list = [member_label(m) for m in slots]
    label_to_id = {member_label(m): m["id"] for m in slots}

    with st.sidebar:
        st.divider()
        st.subheader("メンバー認証（番号＋パス）")

        default_mid = st.session_state.get("member_id") or 1
        default_label = next((member_label(m) for m in slots if m["id"] == default_mid), label_list[0])

        with st.form("auth_form", clear_on_submit=False):
            chosen_label = st.selectbox("メンバー番号", label_list, index=label_list.index(default_label))
            passcode = st.text_input("共通パスコード", type="password")
            invite = st.text_input("招待コード（初回のみ）", type="password")

            chosen_id = int(label_to_id[chosen_label])
            chosen_mem = get_member(chosen_id)
            is_empty_slot = (chosen_mem is None) or (not chosen_mem["name"])

            name_input = ""
            if is_empty_slot:
                name_input = st.text_input("あなたの名前（初回登録）", value="")
                st.caption("※ 未登録の番号を選んだ場合のみ、ここで名前を登録します。")
            else:
                st.caption(f"ログイン名：{chosen_mem['name']}")

            insta_in = st.text_input("Instagram（任意・@不要）", value=st.session_state.get("insta_input", ""))
            submitted = st.form_submit_button("認証/更新")

        if submitted:
            try:
                if passcode != SHARED_PASSCODE:
                    st.session_state["authed"] = False
                    st.error("パスコードが違います")
                else:
                    m = get_member(chosen_id) or {"id": chosen_id, "name": None, "insta": None, "is_active": False}

                    db_insta = get_insta_by_id(chosen_id)
                    if db_insta:
                        st.session_state["insta_input"] = db_insta
                        insta_final = db_insta
                    else:
                        insta_final = (insta_in or "").strip().lstrip("@") or None
                        st.session_state["insta_input"] = insta_final or ""

                    if not m["name"]:
                        nm = (name_input or "").strip()
                        if not nm:
                            st.session_state["authed"] = False
                            st.error("未登録の番号を使う場合、名前を入力してください。")
                            st.stop()
                        if invite != INVITE_CODE:
                            st.session_state["authed"] = False
                            st.error("初回登録には招待コードが必要です。")
                            st.stop()

                        update_member_profile(chosen_id, name=nm, insta=insta_final, activate=True)
                        st.success(f"{chosen_id}番を {nm} で登録しました。")
                        m = get_member(chosen_id)

                    if not m["is_active"]:
                        if invite == INVITE_CODE:
                            update_member_profile(chosen_id, name=None, insta=insta_final, activate=True)
                            m = get_member(chosen_id)
                            st.success("有効化しました。")
                        else:
                            st.session_state["authed"] = False
                            st.warning("未承認です。招待コードで有効化してください。")
                            st.stop()

                    if (not db_insta) and insta_final:
                        update_member_profile(chosen_id, name=None, insta=insta_final, activate=None)

                    st.session_state["member_id"] = chosen_id
                    st.session_state["member_name"] = m["name"] or ""
                    st.session_state["authed"] = True
                    clear_read_caches()

            except Exception:
                st.session_state["authed"] = False
                st.error("認証処理でエラーが発生しました（詳細）")
                st.code(traceback.format_exc())

        authed = bool(st.session_state.get("authed", False))
        mid = st.session_state.get("member_id")
        mname = st.session_state.get("member_name", "")

        if authed and mid:
            st.success(f"認証OK：{mid} / {mname}")
        else:
            st.caption("未認証のまま閲覧はできます（操作は不可）")

        if is_admin_name(mname):
            st.info(" 管理者モード")

        st.divider()
        st.subheader("アプリ共有（QR）")
        app_url = st.text_input("このアプリのURL", value="")
        if app_url:
            qr = qrcode.make(app_url)
            st.image(qr, caption="このQRを仲間に配布", width=260)

    # --- 状態 ---
    authed = bool(st.session_state.get("authed", False))
    member_id = st.session_state.get("member_id")
    member_name = st.session_state.get("member_name", "")
    is_admin = is_admin_name(member_name)

    # --- Tabs ---
    tab_inv, tab_list, tab_bbs, tab_logs, tab_mem, tab_csv, tab_backup = st.tabs(
        [" 在庫登録", " 在庫/貸出/予約", " 掲示板", " 履歴", " メンバー", " CSV", " バックアップ"]
    )

    # ============================================================
    # 在庫登録（フォーム化＋画像堅牢化）
    # ============================================================
    with tab_inv:
        st.subheader("在庫登録")
        if not authed:
            st.info("登録にはサイドバーで認証が必要です。")
            st.stop()

        with st.form("item_register_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                name = st.text_input("パーツ名")
                cat_sel = st.multiselect("カテゴリ（1件選択）", CATEGORIES, max_selections=1)
                cat = cat_sel[0] if cat_sel else ""
                size = st.text_input("サイズ")
                cond_sel = st.multiselect("状態（1件選択）", ["新品", "美品", "使用感あり", "要整備"], max_selections=1)
                cond = cond_sel[0] if cond_sel else ""

            with c2:
                st.text_input("所有者", value=f"{member_id} : {member_name}", disabled=True)
                loc = st.text_input("保管場所")
                note = st.text_area("備考", height=80)
                pic = st.file_uploader("写真（任意）", type=["jpg", "jpeg", "png"])

            submitted_item = st.form_submit_button("登録")

        if submitted_item:
            if not (name and cat and cond):
                st.warning("パーツ名 / カテゴリ / 状態 は必須です。")
                st.stop()

            blob = file_to_blob(pic)

            try:
                db_exec(
                    """
                    INSERT INTO items(name,category,size,condition,owner,owner_id,location,note,status,photo)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'在庫あり',%s)
                    """,
                    ((name or "").strip(), cat, (size or "").strip() or None, cond,
                     (member_name or "").strip(), int(member_id), (loc or "").strip() or None,
                     (note or "").strip() or None, blob),
                )
                clear_read_caches()
                st.success("登録しました。")
                st.rerun()
            except Exception:
                st.error("DB登録に失敗しました（詳細）")
                st.code(traceback.format_exc())
                st.stop()

    # ============================================================
    # 在庫/貸出/予約（以下は“貼ってくれた現状のまま”）
    # ============================================================
    with tab_list:
        st.subheader("在庫一覧")

        kw = st.text_input("キーワード検索", "")
        f_cat = tuple(st.multiselect("カテゴリ絞り込み", CATEGORIES))
        f_owner = st.text_input("所有者で絞る（名前）", "")
        f_status = tuple(st.multiselect("状態で絞る", ["在庫あり", "貸出中", "整備中", "アーカイブ"]))
        show_arch = st.checkbox("アーカイブも表示", value=False)

        items = list_items_cached(kw, f_cat, f_owner, f_status, show_arch)
        if not items:
            st.caption("該当する在庫がありません。")

        item_ids = tuple(int(r[0]) for r in items) if items else tuple()
        resv_map = reservations_map_cached(item_ids)
        photo_map = photo_map_cached(item_ids)

        for (i, nm, cat, size, cond, owner_name, owner_id, loc, note, status,
             resv_cnt, next_reserver_name, next_position) in items:

            i = int(i)
            resv_list = resv_map.get(i, [])
            next_name = resv_list[0][1] if resv_list else (next_reserver_name if int(resv_cnt) else None)
            next_pos = resv_list[0][0] if resv_list else (int(next_position) if next_position else None)

            with st.container(border=True):
                blob = photo_map.get(i)
                img = blob_to_img(blob) if blob else None
                if img:
                    st.image(img, width=900)

                title_line = f"**{nm}**"
                if int(resv_cnt) > 0:
                    title_line += f' <span class="resv-badge">予約 {int(resv_cnt)}/{MAX_RESERVATIONS_PER_ITEM}</span>'
                st.markdown(title_line, unsafe_allow_html=True)

                st.caption(
                    f"{cat} / サイズ:{size or '-'} / 状態:{cond} / 所有:{owner_name or '-'} / ステータス:{status}"
                )

                if status == "貸出中" and next_name:
                    st.info(f"次の予約：{next_name}（{next_pos}番目）")

                with st.expander("詳細", expanded=False):
                    st.caption(f"保管場所: {loc or '-'}")
                    st.write(note or "備考なし")

                    if not resv_list:
                        st.caption("予約：なし")
                    else:
                        lines = [f"{pos}) {rname}（{rdt or '-'}）" for pos, rname, rid, rdt in resv_list]
                        st.markdown("**予約状況**  " + " / ".join(lines))

                share_text_item = (
                    f"[Gear Swamp]\n"
                    f"パーツ: {nm}\n"
                    f"カテゴリ: {cat}\n"
                    f"サイズ: {size or '-'}\n"
                    f"状態: {cond}\n"
                    f"所有者: {owner_name or '-'}\n"
                    f"保管場所: {loc or '-'}\n"
                    f"備考: {note or '-'}"
                )
                line_url_item = f"https://line.me/R/msg/text/?{quote(share_text_item)}"

                c_b, c_r, c_s, c_cancel, c_state = st.columns([1, 1, 1, 1, 2])

                if c_b.button(" 借りる", key=f"b{i}"):
                    if not authed:
                        st.warning("借用には認証が必要です。")
                    elif status == "貸出中":
                        st.warning("すでに貸出中です。")
                    else:
                        today = date.today()
                        due = compute_due(str(today), 90)
                        with get_conn() as conn2:
                            with conn2.cursor() as cur:
                                cur.execute(
                                    """
                                    INSERT INTO loans(item_id, borrower, borrower_id, start_date, due_date, reminder_days, status)
                                    VALUES(%s,%s,%s,%s,%s,%s,%s)
                                    """,
                                    (i, member_name, member_id, str(today), str(due), 90, "貸出中"),
                                )
                                cur.execute("UPDATE items SET status='貸出中' WHERE id=%s", (i,))
                            conn2.commit()
                        st.session_state["last_borrowed_item_id"] = i
                        clear_read_caches()
                        st.success("借用登録しました（返却目安90日）。このパーツをLINEで共有できます ")

                if c_r.button(" 返却", key=f"r{i}"):
                    if not authed:
                        st.warning("返却には認証が必要です。")
                    elif status != "貸出中":
                        st.warning("貸出中ではありません。")
                    else:
                        with get_conn() as conn2:
                            with conn2.cursor() as cur:
                                cur.execute(
                                    "SELECT id FROM loans WHERE item_id=%s AND status='貸出中' ORDER BY id DESC LIMIT 1",
                                    (i,),
                                )
                                loan = cur.fetchone()
                                if loan:
                                    cur.execute(
                                        "UPDATE loans SET status='返却済', returned_date=%s WHERE id=%s",
                                        (str(date.today()), loan[0]),
                                    )
                                    cur.execute("UPDATE items SET status='在庫あり' WHERE id=%s", (i,))
                            conn2.commit()
                        st.session_state["last_borrowed_item_id"] = None
                        clear_read_caches()
                        st.success("返却しました（在庫ありに戻しました）")
                        st.rerun()

                if c_s.button(" 予約", key=f"s{i}"):
                    if not authed:
                        st.warning("予約には認証が必要です。")
                    else:
                        ok2, msg2 = can_reserve(i, int(member_id))
                        if not ok2:
                            st.warning(msg2)
                        else:
                            pos = create_reservation(i, int(member_id), member_name)
                            st.success(f"{pos}番目で予約しました")
                            st.rerun()

                if c_cancel.button(" キャンセル", key=f"cxl{i}"):
                    if not authed:
                        st.warning("キャンセルには認証が必要です。")
                    else:
                        done = cancel_reservation(i, int(member_id))
                        if done:
                            st.success("予約をキャンセルしました（繰り上げ済）")
                            st.rerun()
                        else:
                            st.caption("自分の予約はありません。")

                new_st = c_state.selectbox(
                    "状態変更",
                    ["変更しない", "在庫あり", "貸出中", "整備中", "アーカイブ"],
                    key=f"st{i}",
                )

                c_upd, c_arc, c_del = st.columns([1, 1, 2])

                if c_upd.button(" 更新", key=f"upd{i}"):
                    if not authed:
                        st.warning("更新には認証が必要です。")
                    elif new_st == "変更しない":
                        st.caption("変更なし")
                    else:
                        db_exec("UPDATE items SET status=%s WHERE id=%s", (new_st, i))
                        clear_read_caches()
                        st.success("状態を更新しました")
                        st.rerun()

                if c_arc.button(" アーカイブ", key=f"arc{i}"):
                    if not authed:
                        st.warning("アーカイブには認証が必要です。")
                    else:
                        db_exec("UPDATE items SET status='アーカイブ' WHERE id=%s", (i,))
                        clear_read_caches()
                        st.rerun()

                with c_del:
                    confirm_del = st.checkbox("削除確認", key=f"cf{i}")
                    if st.button(" 削除", key=f"del{i}") and confirm_del:
                        if not authed:
                            st.warning("削除には認証が必要です。")
                        else:
                            db_exec("DELETE FROM items WHERE id=%s", (i,))
                            clear_read_caches()
                            st.success("削除しました")
                            st.rerun()

                if st.session_state.get("last_borrowed_item_id") == i:
                    st.markdown(f"[ この貸出をLINEで共有]({line_url_item})")

    # ============================================================
    # 掲示板 / 履歴 / メンバー / CSV / バックアップ（現状のまま）
    # ============================================================
    with tab_bbs:
        st.subheader(" 掲示板（試乗・貸し借り・雑談）")
        st.markdown("### 新規投稿")
        if authed:
            ptype = st.selectbox("種別", POST_TYPES)
            pcat = st.selectbox("関連カテゴリ（任意）", ["指定なし"] + CATEGORIES)
            ptitle = st.text_input("タイトル", placeholder="例：誰かピスト試乗させてくれませんか？")
            pbody = st.text_area("本文", height=100)

            if st.button(" 投稿する"):
                if not ptitle.strip():
                    st.error("タイトルは必須です。")
                else:
                    db_exec(
                        """
                        INSERT INTO posts(author,author_id,ptype,category,title,body,created)
                        VALUES(%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            member_name,
                            member_id,
                            ptype,
                            None if pcat == "指定なし" else pcat,
                            ptitle.strip(),
                            pbody.strip(),
                            str(date.today()),
                        ),
                    )
                    clear_read_caches()
                    st.success("投稿しました。")
                    st.rerun()
        else:
            st.caption("※ 投稿には認証が必要です。")

        st.markdown("### 投稿一覧")
        kw_b = st.text_input("キーワード検索（掲示板）", "")
        f_type = tuple(st.multiselect("種別で絞る", POST_TYPES))
        f_cat_b = tuple(st.multiselect("カテゴリで絞る", CATEGORIES))
        f_author = st.text_input("投稿者で絞る（名前）", "")

        posts = posts_cached(kw_b, f_type, f_cat_b, f_author)
        if not posts:
            st.caption("まだ投稿がありません。")
        else:
            for pid, author_name, ptype, cat, title, body, created, author_id in posts:
                with st.container():
                    title_html = html.escape(title or "")
                    meta = f"{created} / 投稿者: {author_name}"
                    if cat:
                        meta += f" / カテゴリ: {cat}"
                    meta_html = html.escape(meta)
                    body_html = html.escape(body or "").replace("\n", "<br>")

                    st.markdown(
                        f"""
                        <div class="bbs-card">
                          <div class="bbs-title">[{html.escape(ptype)}] {title_html}</div>
                          <div class="bbs-meta">{meta_html}</div>
                          <div class="bbs-body">{body_html}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

                    insta_user = get_insta_by_id(int(author_id)) if author_id else None
                    colx, coly, colz = st.columns(3)

                    if insta_user:
                        insta_url = f"https://instagram.com/{insta_user}"
                        colx.markdown(f"[ @{insta_user} へDM](<{insta_url}>)")

                    share_text = f"[Gear Swamp掲示板]\n[{ptype}] {title}\n{body}\nfrom {author_name}"
                    line_url = f"https://line.me/R/msg/text/?{quote(share_text)}"
                    coly.markdown(f"[ LINEで共有]({line_url})")

                    if is_admin and colz.button(" 投稿削除", key=f"del_post_{pid}"):
                        db_exec("DELETE FROM posts WHERE id=%s", (pid,))
                        clear_read_caches()
                        st.success("投稿を削除しました。")
                        st.rerun()

    with tab_logs:
        st.subheader("貸出・返却履歴")
        rows = loans_cached()
        if not rows:
            st.caption("まだ履歴はありません。")
        else:
            for name, borrower_name, s, d, r, stt in rows:
                st.caption(
                    f"{name or '(削除済み)'} / 借り手:{borrower_name} / {s} → {r or '-'} / "
                    f"返却目安:{d or '-'} / 状態:{stt}"
                )

    with tab_mem:
        st.subheader("メンバー（番号席）")
        st.markdown("### メンバー一覧")
        slots2 = member_slots_cached(MAX_MEMBERS)
        for m in slots2:
            nm = m["name"] if m["name"] else "未登録"
            st.markdown(f"- **{m['id']}** : {nm}  / @{(m['insta'] or '-') }  / {'有効' if m['is_active'] else '未承認'}")

        st.divider()
        st.subheader("管理者ツール")
        if not is_admin:
            st.caption("（admin_users に登録された管理者のみ）")
        else:
            tgt = st.selectbox("対象番号", [m["id"] for m in slots2], index=0, key="admin_slot")
            curm = get_member(int(tgt)) or {"name": "", "insta": "", "is_active": False}

            new_name = st.text_input("表示名（空で未登録に戻す）", value=curm["name"] or "")
            new_insta = st.text_input("Instagram（@不要）", value=curm["insta"] or "")
            new_active = st.checkbox("有効化", value=bool(curm["is_active"]))

            if st.button("更新保存", key="admin_save_slot"):
                update_member_profile(int(tgt), name=new_name, insta=new_insta, activate=new_active)
                st.success("更新しました。")
                st.rerun()

    with tab_csv:
        st.subheader("CSV一括登録（在庫）")

        templ = StringIO()
        w = csv.writer(templ)
        w.writerow(["name", "category", "size", "condition", "owner_id", "location", "note"])
        w.writerow(["700C Front Wheel", "ホイール", "700C/100x12", "美品", 1, "自宅A", "ハブDT350"])
        w.writerow(["11s Cassette 11-28", "スプロケット/コグ", "HG 11s", "使用感あり", 1, "自宅B", "軽微摩耗"])
        st.download_button("テンプレCSVをダウンロード", templ.getvalue(), file_name="parts_template.csv", mime="text/csv")

        if not authed:
            st.caption("※ 一括登録は認証後に使えます。")
        else:
            up = st.file_uploader("CSVを選択（UTF-8推奨）", type=["csv"])
            if up and st.button("一括登録を実行"):
                text = up.read().decode("utf-8", "ignore")
                reader = csv.DictReader(StringIO(text))
                count = 0
                with get_conn() as conn:
                    with conn.cursor() as c:
                        for row in reader:
                            pname = (row.get("name") or "").strip()
                            if not pname:
                                continue
                            category = (row.get("category") or "").strip() or "その他"
                            size = (row.get("size") or "").strip()
                            condition = (row.get("condition") or "").strip() or "使用感あり"
                            owner_id = int(row.get("owner_id") or member_id or 1)
                            location = (row.get("location") or "").strip()
                            note = (row.get("note") or "").strip()

                            owner_name = member_name_by_id(owner_id) or ""
                            c.execute(
                                """
                                INSERT INTO items(name,category,size,condition,owner,owner_id,location,note,status,photo)
                                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'在庫あり',NULL)
                                """,
                                (pname, category, size or None, condition, owner_name, owner_id, location or None, note or None),
                            )
                            count += 1
                    conn.commit()
                clear_read_caches()
                st.success(f"{count} 件 登録しました。")
                st.rerun()

    with tab_backup:
        st.subheader(" バックアップ（Supabase運用）")
        st.info(
            "このアプリは **Supabase(Postgres)** を使っています。\n\n"
            "SQLiteファイルを保存する方式はありません。バックアップは Supabase 側で運用します。"
        )


# ================================================================
# 実行（白画面防止）
# ================================================================
try:
    run_app()
except Exception:
    st.error("🔥 アプリ実行中にエラーが発生しました（白画面防止のため詳細を表示します）")
    st.code(traceback.format_exc())
    raise
