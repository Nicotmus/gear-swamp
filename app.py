# ================================================================
# app.py --- Gear Swamp（Supabase/Postgres版・安定/高速・完全差替え）
#
# ✅ 今回の目的：在庫（パーツ）登録を確実に通す
# - 在庫登録を st.form 化（連打/二重登録/リラン事故を減らす）
# - member_id / member_name の未設定を防止して INSERT 前に弾く
# - 画像アップロードを getvalue→BytesIO→PIL に統一して堅牢化
# - DB例外はその場で trace を表示（白画面防止）
# - clear_read_caches を安全化（未定義キャッシュがあっても落ちない）
#
# ✅ 速度/安定
# - psycopg2 接続プール
# - N+1撲滅（在庫/予約）
# - st.cache_data（TTL）
# - photo(BYTEA=memoryview)は別キャッシュでbytes化
# ================================================================

import base64
import traceback
from io import BytesIO
from contextlib import contextmanager
from datetime import date, timedelta

import streamlit as st
from PIL import Image
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

st.set_page_config(page_title="Gear Swamp", page_icon="icon_gearswamp.png", layout="wide")


# ================================================================
# DB: 接続プール
# ================================================================
@st.cache_resource(show_spinner=False)
def get_pool() -> SimpleConnectionPool:
    cfg = st.secrets["postgres"]
    minconn = int(st.secrets.get("pg_pool_minconn", 1))
    maxconn = int(st.secrets.get("pg_pool_maxconn", 10))
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


# ================================================================
# キャッシュクリア（更新後に呼ぶ）※安全化
# ================================================================
def _safe_clear(fn):
    try:
        if fn is not None and hasattr(fn, "clear"):
            fn.clear()
    except Exception:
        pass

def clear_read_caches():
    _safe_clear(globals().get("list_items_cached"))
    _safe_clear(globals().get("reservations_map_cached"))
    _safe_clear(globals().get("photo_map_cached"))
    _safe_clear(globals().get("loans_cached"))
    _safe_clear(globals().get("member_slots_cached"))


# ================================================================
# 初期化耐性（テーブル作成＋席作成）
# ================================================================
@st.cache_resource(show_spinner=False)
def ensure_schema_and_slots_once(max_members: int):
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

            # 旧DB追従（列が無い場合に追加）
            c.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS owner_id INTEGER")
            c.execute("ALTER TABLE loans ADD COLUMN IF NOT EXISTS borrower_id INTEGER")
            c.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS reserver_id INTEGER")
            c.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS position INTEGER")
            c.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS reserved_date TEXT")

            # Index
            c.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_items_owner_id ON items(owner_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_resv_item_pos ON reservations(item_id, position)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_loans_item_status ON loans(item_id, status)")

            # ✅席作成：一括INSERT（不足分のみ作成）
            c.execute(
                """
                INSERT INTO members(id, name, insta, is_active, created_at)
                SELECT gs, NULL, NULL, FALSE, %s
                FROM generate_series(1, %s) AS gs
                ON CONFLICT (id) DO NOTHING
                """,
                (str(date.today()), int(max_members))
            )

        conn.commit()
    return True


# ================================================================
# 背景CSS
# ================================================================
def set_background(image_path: str):
    try:
        with open(image_path, "rb") as f:
            data = f.read()
        encoded = base64.b64encode(data).decode("utf-8")
        st.markdown(
            f"""
            <style>
            html, body {{ background-color:#000 !important; color:#f5f5f5 !important; }}
            .stApp {{
                background: url("data:image/png;base64,{encoded}") no-repeat center center fixed;
                background-size: cover;
            }}
            .stApp > div {{ background-color: rgba(0,0,0,0.40); }}
            section[data-testid="stSidebar"] {{ background-color:#111 !important; }}

            .stApp button[role="tab"] {{
                border-radius: 18px 18px 0 0 !important;
                background-color: rgba(20,20,20,0.8) !important;
                color: #dddddd !important;
                border: 1px solid #333333 !important;
                padding: 0.5rem 1.2rem !important;
                font-weight: 600 !important;
            }}
            .stApp button[role="tab"][aria-selected="true"] {{
                background: linear-gradient(135deg, #ff6b6b, #ff4b4b) !important;
                color: #ffffff !important;
                border-color: #ff8a8a !important;
            }}

            .stApp input, .stApp textarea, .stApp select {{
                color:#f5f5f5 !important; background-color:#222 !important; border:1px solid #555 !important;
            }}
            .stApp ::placeholder {{ color:#aaa !important; opacity:1 !important; }}

            .stApp button {{
                background-color:#333 !important; color:#f5f5f5 !important; border:1px solid #777 !important;
                border-radius:6px !important; padding:0.25rem 0.9rem !important; font-size:0.9rem !important;
                margin-top:0.1rem !important; margin-bottom:0.1rem !important;
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

            .stApp a, .stApp a:link, .stApp a:visited {{ color:#8cc2ff !important; text-decoration: underline !important; }}
            .stApp a:hover {{ color:#c6e3ff !important; }}
            </style>
            """,
            unsafe_allow_html=True,
        )
    except Exception as e:
        st.warning("背景の読み込みに失敗しました（bg_gearswamp.png を確認）")
        st.code(str(e))


# ================================================================
# 画像
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

def compute_due(start, days):
    try:
        s = dt_parse(start).date()
    except Exception:
        s = date.today()
    return s + timedelta(days=days)


# ================================================================
# members（UPDATEのみ）
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


# ================================================================
# 一覧（photo無し）＋ 予約まとめ ＋ photo別（bytes化してキャッシュ）
# ================================================================
@st.cache_data(ttl=TTL_ITEMS, show_spinner=False)
def list_items_cached(kw: str, f_cat: tuple[str, ...], f_owner: str, f_status: tuple[str, ...], show_arch: bool):
    q = """
    SELECT
      i.id, i.name, i.category, i.size, i.condition,
      COALESCE(om.name, i.owner) AS owner_name,
      i.owner_id,
      i.location, i.note, i.status,
      COALESCE(rc.cnt, 0) AS reservation_count
    FROM items i
    LEFT JOIN members om ON i.owner_id = om.id
    LEFT JOIN (
      SELECT item_id, COUNT(*) AS cnt
      FROM reservations
      GROUP BY item_id
    ) rc ON rc.item_id = i.id
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
    q += " ORDER BY i.id DESC"
    return db_fetchall(q, tuple(p))

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

@st.cache_data(ttl=TTL_PHOTOS, show_spinner=False)
def photo_map_cached(item_ids: tuple[int, ...]):
    if not item_ids:
        return {}
    placeholders = ",".join(["%s"] * len(item_ids))
    rows = db_fetchall(f"SELECT id, photo FROM items WHERE id IN ({placeholders})", tuple(item_ids))
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
# 予約
# ================================================================
def can_reserve(item_id: int, reserver_id: int) -> tuple[bool, str]:
    r = db_fetchone("SELECT position FROM reservations WHERE item_id=%s AND reserver_id=%s", (item_id, reserver_id))
    if r:
        return False, f"すでに予約済み（{int(r[0])}番目）です。"
    r2 = db_fetchone("SELECT COUNT(*) FROM reservations WHERE item_id=%s", (item_id,))
    cnt = int(r2[0]) if r2 else 0
    if cnt >= MAX_RESERVATIONS_PER_ITEM:
        return False, "予約枠がいっぱいです。"
    return True, ""

def create_reservation(item_id: int, reserver_id: int, reserver_name: str) -> int:
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
# 履歴（最低限）
# ================================================================
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
# App
# ================================================================
def run_app():
    ensure_schema_and_slots_once(MAX_MEMBERS)
    set_background("bg_gearswamp.png")

    st.session_state.setdefault("authed", False)
    st.session_state.setdefault("member_id", None)
    st.session_state.setdefault("member_name", "")
    st.session_state.setdefault("insta_input", "")

    # Sidebar
    slots = member_slots_cached(MAX_MEMBERS)
    label_list = [member_label(m) for m in slots]
    label_to_id = {member_label(m): m["id"] for m in slots}

    with st.sidebar:
        st.subheader("メンバー認証（番号＋パス）")

        default_mid = st.session_state.get("member_id") or 1
        default_label = next((member_label(m) for m in slots if m["id"] == default_mid), label_list[0])

        with st.form("auth_form", clear_on_submit=False):
            chosen_label = st.selectbox("メンバー番号", label_list, index=label_list.index(default_label))
            passcode = st.text_input("共通パスコード", type="password")
            invite = st.text_input("招待コード（初回のみ）", type="password")

            chosen_id = int(label_to_id[chosen_label])
            m = get_member(chosen_id)
            is_empty_slot = (m is None) or (not m["name"])

            name_input = ""
            if is_empty_slot:
                name_input = st.text_input("あなたの名前（初回登録）", value="")
                st.caption("※ 未登録の番号を選んだ場合のみ、ここで名前を登録します。")
            else:
                st.caption(f"ログイン名：{m['name']}")

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

                    # 初回登録：UPDATEで席を埋める
                    if not m["name"]:
                        nm = (name_input or "").strip()
                        if not nm:
                            st.error("未登録の番号を使う場合、名前を入力してください。")
                            st.stop()
                        if invite != INVITE_CODE:
                            st.error("初回登録には招待コードが必要です。")
                            st.stop()
                        update_member_profile(chosen_id, name=nm, insta=insta_final, activate=True)
                        m = get_member(chosen_id)
                        st.success(f"{chosen_id}番を {m['name']} で登録しました。")

                    # 未承認なら招待で有効化
                    if not m["is_active"]:
                        if invite == INVITE_CODE:
                            update_member_profile(chosen_id, name=None, insta=insta_final, activate=True)
                            m = get_member(chosen_id)
                            st.success("有効化しました。")
                        else:
                            st.warning("未承認です。招待コードで有効化してください。")
                            st.stop()

                    # DBが空なら入力で更新
                    if (not db_insta) and insta_final:
                        update_member_profile(chosen_id, name=None, insta=insta_final, activate=None)

                    st.session_state["member_id"] = chosen_id
                    st.session_state["member_name"] = m["name"] or ""
                    st.session_state["authed"] = True
                    clear_read_caches()

            except psycopg2.errors.UniqueViolation:
                st.session_state["authed"] = False
                st.error("登録名が重複しています。別の表記にしてください。")
            except Exception as e:
                st.session_state["authed"] = False
                st.error("認証処理でエラーが発生しました")
                st.code(str(e))
                st.code(traceback.format_exc())

        authed = bool(st.session_state.get("authed", False))
        mid = st.session_state.get("member_id")
        mname = st.session_state.get("member_name", "")

        if authed and mid:
            st.success(f"認証OK：{mid} / {mname}")
        else:
            st.caption("未認証のまま閲覧はできます（操作は不可）")

    authed = bool(st.session_state.get("authed", False))
    member_id = st.session_state.get("member_id")
    member_name = st.session_state.get("member_name", "")

    tab_inv, tab_list, tab_logs = st.tabs([" 在庫登録", " 在庫/貸出/予約", " 履歴"])

    # ============================================================
    # 在庫登録（★ここが今回の主修正：フォーム化＋堅牢化）
    # ============================================================
    with tab_inv:
        st.subheader("在庫登録")
        if not authed:
            st.info("登録には認証が必要です。")
            st.stop()

        # member_id / member_name の確定（NoneのままINSERTしない）
        try:
            member_id_int = int(member_id) if member_id is not None else None
        except Exception:
            member_id_int = None
        member_name_str = (member_name or "").strip()

        if member_id_int is None or not member_name_str:
            st.error("認証情報が不完全です（member_id / member_name が空）。左の認証をやり直して。")
            st.stop()

        with st.form("item_register_form", clear_on_submit=True):
            c1, c2 = st.columns(2)

            with c1:
                name = st.text_input("パーツ名", value="")
                cat_sel = st.multiselect("カテゴリ（1件選択）", CATEGORIES, max_selections=1)
                cat = cat_sel[0] if cat_sel else ""
                size = st.text_input("サイズ", value="")
                cond_sel = st.multiselect("状態（1件選択）", ["新品", "美品", "使用感あり", "要整備"], max_selections=1)
                cond = cond_sel[0] if cond_sel else ""

            with c2:
                st.text_input("所有者", value=f"{member_id_int} : {member_name_str}", disabled=True)
                loc = st.text_input("保管場所", value="")
                note = st.text_area("備考", value="", height=80)
                pic = st.file_uploader("写真（任意）", type=["jpg", "jpeg", "png"])

            submitted_item = st.form_submit_button("登録")

        if submitted_item:
            name = (name or "").strip()
            cat = (cat or "").strip()
            cond = (cond or "").strip()
            size = (size or "").strip()
            loc = (loc or "").strip()
            note = (note or "").strip()

            if not name:
                st.warning("パーツ名を入力して。"); st.stop()
            if not cat:
                st.warning("カテゴリを1つ選んで。"); st.stop()
            if not cond:
                st.warning("状態を1つ選んで。"); st.stop()

            # 画像処理（堅牢化：UploadedFile → bytes → PIL）
            blob = None
            try:
                if pic is not None:
                    raw = pic.getvalue()
                    if raw:
                        img = Image.open(BytesIO(raw))
                        blob = img_to_blob(img)
            except Exception as e:
                st.error("画像の読み込み/変換に失敗しました（JPEG/PNGを確認）")
                st.code(str(e))
                st.stop()

            # DB登録
            try:
                db_exec(
                    """
                    INSERT INTO items(name,category,size,condition,owner,owner_id,location,note,status,photo)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'在庫あり',%s)
                    """,
                    (name, cat, size or None, cond, member_name_str, member_id_int, loc or None, note or None, blob),
                )
                clear_read_caches()
                st.success("登録しました。")
                st.rerun()
            except Exception as e:
                st.error("DB登録に失敗しました。エラー内容を確認して。")
                st.code(str(e))
                st.code(traceback.format_exc())
                st.stop()

    # ============================================================
    # 在庫一覧 / 予約
    # ============================================================
    with tab_list:
        st.subheader("在庫一覧")

        kw = st.text_input("キーワード検索", "")
        f_cat = tuple(st.multiselect("カテゴリ絞り込み", CATEGORIES))
        f_owner = st.text_input("所有者で絞る（名前）", "")
        f_status = tuple(st.multiselect("状態で絞る", ["在庫あり", "貸出中", "整備中", "アーカイブ"]))
        show_arch = st.checkbox("アーカイブも表示", value=False)

        items = list_items_cached(kw, f_cat, f_owner, f_status, show_arch)
        item_ids = tuple(int(r[0]) for r in items) if items else tuple()
        resv_map = reservations_map_cached(item_ids)
        photo_map = photo_map_cached(item_ids)

        for (iid, nm, cat, size, cond, owner_name, owner_id, loc, note, status, resv_cnt) in items:
            iid = int(iid)
            resv_cnt = int(resv_cnt or 0)
            resv_list = resv_map.get(iid, [])

            with st.container(border=True):
                blob = photo_map.get(iid)
                img = blob_to_img(blob) if blob else None
                if img:
                    st.image(img, width=900)

                title_line = f"**{nm}**"
                if resv_cnt > 0:
                    title_line += f' <span class="resv-badge">予約 {resv_cnt}/{MAX_RESERVATIONS_PER_ITEM}</span>'
                st.markdown(title_line, unsafe_allow_html=True)

                st.caption(f"{cat} / サイズ:{size or '-'} / 状態:{cond} / 所有:{owner_name or '-'} / ステータス:{status}")

                with st.expander("詳細", expanded=False):
                    st.caption(f"保管場所: {loc or '-'}")
                    st.write(note or "備考なし")
                    if not resv_list:
                        st.caption("予約：なし")
                    else:
                        lines = [f"{pos}) {rname}（{rdt or '-'}）" for pos, rname, rid, rdt in resv_list]
                        st.markdown("**予約状況**  " + " / ".join(lines))

                c_s, c_cancel = st.columns([1, 1])

                if c_s.button(" 予約", key=f"s{iid}"):
                    if not authed:
                        st.warning("予約には認証が必要です。")
                    else:
                        ok, msg = can_reserve(iid, int(member_id))
                        if not ok:
                            st.warning(msg)
                        else:
                            pos = create_reservation(iid, int(member_id), member_name)
                            st.success(f"{pos}番目で予約しました")
                            st.rerun()

                if c_cancel.button(" キャンセル", key=f"cxl{iid}"):
                    if not authed:
                        st.warning("キャンセルには認証が必要です。")
                    else:
                        done = cancel_reservation(iid, int(member_id))
                        if done:
                            st.success("予約をキャンセルしました（繰り上げ済）")
                            st.rerun()
                        else:
                            st.caption("自分の予約はありません。")

    # ============================================================
    # 履歴
    # ============================================================
    with tab_logs:
        st.subheader("貸出・返却履歴")
        rows = loans_cached()
        if not rows:
            st.caption("まだ履歴はありません。")
        else:
            for name, borrower_name, s, d, r, stt in rows:
                st.caption(f"{name or '(削除済み)'} / 借り手:{borrower_name} / {s} → {r or '-'} / 返却目安:{d or '-'} / 状態:{stt}")


# ================================================================
# 実行（白画面防止）
# ================================================================
try:
    run_app()
except Exception:
    st.error("🔥 アプリ実行中にエラーが発生しました（白画面防止のため詳細を表示します）")
    st.code(traceback.format_exc())
    raise
