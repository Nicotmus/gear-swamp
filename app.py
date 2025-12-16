# ================================================================
# app.py --- Gear Swamp（Supabase/Postgres版・member_id参照へ一気に移行＋予約完成）
# - ログイン: メンバー番号(1..MAX_MEMBERS) + 共通パス
# - DB参照: items.owner_id / loans.borrower_id / reservations.reserver_id / posts.author_id
# - 予約: 状況表示 / キャンセル / 繰り上げ / 予約ありバッジ / 次の人表示
# ================================================================
import csv
import html
import base64
import traceback
from io import BytesIO, StringIO
from contextlib import contextmanager
from datetime import date, timedelta
from urllib.parse import quote

import qrcode
import streamlit as st
from PIL import Image
from dateutil.parser import parse as dt_parse
import psycopg2

# ================================================================
# 設定
# ================================================================
MAX_MEMBERS = int(st.secrets.get("max_members", 6))  # ←人数増やすならここ(secrets優先)
SHARED_PASSCODE = st.secrets.get("passcode", "1234")
INVITE_CODE = st.secrets.get("invite_code", "join-123")
ADMIN_USERS = set(st.secrets.get("admin_users") or [])  # 例: ["TETSUYA"]

MAX_RESERVATIONS_PER_ITEM = 3  # 予約枠

CATEGORIES = [
    "フレーム/フォーク", "ヘッドセット", "ハンドル/ステム", "グリップ/バーテープ",
    "サドル/シートポスト", "ホイール", "ハブ/リム/スポーク", "タイヤ/チューブ",
    "ブレーキ（リム/ディスク）", "ローター/パッド", "シフター/ブレーキレバー",
    "ディレイラー（F/R）", "クランク/BB", "スプロケット/コグ",
    "チェーン/チェーンリング", "ペダル", "ケーブル/アウター", "小物/ツール", "その他"
]
POST_TYPES = ["試乗希望", "貸してほしい", "貸します", "譲ります", "雑談"]

st.set_page_config(
    page_title="Gear Swamp",
    page_icon="icon_gearswamp.png",
    layout="wide",
)

# ================================================================
# DB
# ================================================================
@contextmanager
def get_conn():
    cfg = st.secrets["postgres"]
    conn = psycopg2.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
        connect_timeout=10,
        sslmode="require",
    )
    try:
        yield conn
    finally:
        conn.close()

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
# 初期化耐性（スキーマ保証＋移行）
# ================================================================
@st.cache_resource(show_spinner=False)
def ensure_schema_and_migrate_once(max_members: int):
    with get_conn() as conn:
        with conn.cursor() as c:
            # members（席番号方式）
            c.execute("""
            CREATE TABLE IF NOT EXISTS members(
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE,
                insta TEXT,
                is_active BOOLEAN DEFAULT FALSE,
                created_at TEXT
            )
            """)

            # items
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
            )
            """)

            # loans
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
            )
            """)

            # reservations
            c.execute("""
            CREATE TABLE IF NOT EXISTS reservations(
                id SERIAL PRIMARY KEY,
                item_id INTEGER,
                reserver TEXT,
                reserver_id INTEGER,
                position INTEGER,
                reserved_date TEXT
            )
            """)

            # posts
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
            )
            """)

            # 念のため列追加（旧DB追従）
            c.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS owner_id INTEGER")
            c.execute("ALTER TABLE loans ADD COLUMN IF NOT EXISTS borrower_id INTEGER")
            c.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS reserver_id INTEGER")
            c.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS author_id INTEGER")
            c.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS position INTEGER")
            c.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS reserved_date TEXT")

            # Index
            c.execute("CREATE INDEX IF NOT EXISTS idx_items_owner_id ON items(owner_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_loans_borrower_id ON loans(borrower_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_resv_item_pos ON reservations(item_id, position)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_posts_author_id ON posts(author_id)")

            # 席を保証
            for mid in range(1, max_members + 1):
                c.execute(
                    """
                    INSERT INTO members(id, name, insta, is_active, created_at)
                    VALUES(%s, NULL, NULL, FALSE, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (mid, str(date.today()))
                )

            # バックフィル（厳密一致）
            c.execute("""
            UPDATE items i
            SET owner_id = m.id
            FROM members m
            WHERE i.owner_id IS NULL
              AND i.owner IS NOT NULL
              AND m.name IS NOT NULL
              AND i.owner = m.name
            """)
            c.execute("""
            UPDATE loans l
            SET borrower_id = m.id
            FROM members m
            WHERE l.borrower_id IS NULL
              AND l.borrower IS NOT NULL
              AND m.name IS NOT NULL
              AND l.borrower = m.name
            """)
            c.execute("""
            UPDATE reservations r
            SET reserver_id = m.id
            FROM members m
            WHERE r.reserver_id IS NULL
              AND r.reserver IS NOT NULL
              AND m.name IS NOT NULL
              AND r.reserver = m.name
            """)
            c.execute("""
            UPDATE posts p
            SET author_id = m.id
            FROM members m
            WHERE p.author_id IS NULL
              AND p.author IS NOT NULL
              AND m.name IS NOT NULL
              AND p.author = m.name
            """)

        conn.commit()
    return True

# ================================================================
# テーマ＆背景
# ================================================================
def set_background(image_path: str):
    try:
        with open(image_path, "rb") as f:
            data = f.read()
        encoded = base64.b64encode(data).decode("utf-8")

        st.markdown(
            f"""
            <style>
            html, body {{
                background-color: #000000 !important;
                color: #f5f5f5 !important;
            }}
            .stApp {{
                background: url("data:image/png;base64,{encoded}") no-repeat center center fixed;
                background-size: cover;
            }}
            .stApp > div {{
                background-color: rgba(0,0,0,0.40);
            }}
            .stApp, .stApp p, .stApp li, .stApp span,
            .stApp label, .stApp div, .stMarkdown,
            .stTextInput label, .stSelectbox label, .stMultiSelect label {{
                color: #f5f5f5 !important;
            }}
            .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6 {{
                color: #ffffff !important;
            }}
            section[data-testid="stSidebar"] {{
                background-color: #111111 !important;
            }}

            /* Tabs */
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

            /* Inputs */
            .stApp input,
            .stApp textarea,
            .stApp select {{
                color: #f5f5f5 !important;
                background-color: #222222 !important;
                border: 1px solid #555555 !important;
            }}
            .stApp ::placeholder {{
                color: #aaaaaa !important;
                opacity: 1 !important;
            }}

            /* Buttons */
            .stApp button {{
                background-color: #333333 !important;
                color: #f5f5f5 !important;
                border: 1px solid #777777 !important;
                border-radius: 6px !important;
                padding: 0.25rem 0.9rem !important;
                font-size: 0.9rem !important;
                margin-top: 0.1rem !important;
                margin-bottom: 0.1rem !important;
            }}

            /* Reservation badge */
            .resv-badge {{
                display:inline-block;
                padding: 0.1rem 0.55rem;
                border-radius: 999px;
                border: 1px solid rgba(255,255,255,0.25);
                background: rgba(255,75,75,0.18);
                font-size: 0.8rem;
                margin-left: .4rem;
            }}

            /* BBS card */
            .bbs-card {{
                background-color: rgba(0,0,0,0.85);
                border-radius: 10px;
                padding: 0.8rem 1rem;
                margin-bottom: 0.1rem;
            }}
            .bbs-title {{
                font-weight: 700;
                margin-bottom: .2rem;
            }}
            .bbs-meta {{
                font-size: 0.8rem;
                opacity: 0.85;
                margin-bottom: 0.4rem;
            }}
            .bbs-body {{
                font-size: 0.95rem;
                line-height: 1.5;
                white-space: pre-wrap;
            }}

            /* Links */
            .stApp a, .stApp a:link, .stApp a:visited {{
                color: #8cc2ff !important;
                text-decoration: underline !important;
            }}
            .stApp a:hover {{
                color: #c6e3ff !important;
            }}
            </style>
            """,
            unsafe_allow_html=True,
        )
    except Exception as e:
        st.warning("背景の読み込みに失敗しました（bg_gearswamp.png を確認）")
        st.code(str(e))

# ================================================================
# 画像ユーティリティ
# ================================================================
def img_to_blob(img, max_px=1400):
    if not img:
        return None
    img = img.convert("RGB")
    img.thumbnail((max_px, max_px))
    b = BytesIO()
    img.save(b, format="JPEG", quality=85, optimize=True)
    return b.getvalue()

def blob_to_img(blob, thumb_px=900):
    if not blob:
        return None
    i = Image.open(BytesIO(blob))
    i.thumbnail((thumb_px, thumb_px))
    return i

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

def list_member_slots(max_members: int):
    rows = db_fetchall("SELECT id, name, insta, is_active FROM members WHERE id <= %s ORDER BY id", (max_members,))
    out = []
    for mid, name, insta, act in rows:
        out.append({"id": int(mid), "name": name, "insta": insta, "is_active": bool(act)})
    return out

def update_member_profile(mid: int, name, insta, activate):
    if insta is not None:
        insta = (insta or "").strip().lstrip("@") or None
    if name is not None:
        name = (name or "").strip() or None

    sets = []
    params = []
    if name is not None:
        sets.append("name=%s")
        params.append(name)
    if insta is not None:
        sets.append("insta=%s")
        params.append(insta)
    if activate is not None:
        sets.append("is_active=%s")
        params.append(bool(activate))
    if not sets:
        return
    params.append(mid)
    db_exec(f"UPDATE members SET {', '.join(sets)} WHERE id=%s", tuple(params))

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
# 予約（完成セット）
# ================================================================
def list_reservations_for_item(item_id: int):
    """
    position順に返す: [(position, reserver_name, reserver_id, reserved_date), ...]
    """
    rows = db_fetchall(
        """
        SELECT
          r.position,
          COALESCE(m.name, r.reserver) AS reserver_name,
          r.reserver_id,
          r.reserved_date
        FROM reservations r
        LEFT JOIN members m ON r.reserver_id = m.id
        WHERE r.item_id=%s
        ORDER BY r.position ASC, r.id ASC
        """,
        (item_id,)
    )
    out = []
    for pos, nm, rid, dt in rows:
        out.append((int(pos) if pos is not None else 999, nm, int(rid) if rid else None, dt))
    return out

def has_reservation(item_id: int) -> bool:
    r = db_fetchone("SELECT 1 FROM reservations WHERE item_id=%s LIMIT 1", (item_id,))
    return bool(r)

def can_reserve(item_id: int, reserver_id: int) -> tuple[bool, str]:
    # すでに自分が予約していないか
    r = db_fetchone(
        "SELECT position FROM reservations WHERE item_id=%s AND reserver_id=%s",
        (item_id, reserver_id)
    )
    if r:
        return False, f"すでに予約済み（{int(r[0])}番目）です。"

    # 枠
    r2 = db_fetchone("SELECT COUNT(*) FROM reservations WHERE item_id=%s", (item_id,))
    cnt = int(r2[0]) if r2 else 0
    if cnt >= MAX_RESERVATIONS_PER_ITEM:
        return False, "予約枠がいっぱいです。"
    return True, ""

def create_reservation(item_id: int, reserver_id: int, reserver_name: str):
    # 次ポジション
    r = db_fetchone("SELECT COALESCE(MAX(position),0)+1 FROM reservations WHERE item_id=%s", (item_id,))
    pos = int(r[0]) if r else 1
    if pos > MAX_RESERVATIONS_PER_ITEM:
        raise ValueError("予約枠がいっぱいです")

    db_exec(
        """
        INSERT INTO reservations(item_id, reserver, reserver_id, position, reserved_date)
        VALUES(%s,%s,%s,%s,%s)
        """,
        (item_id, reserver_name, reserver_id, pos, str(date.today())),
    )
    return pos

def cancel_reservation(item_id: int, reserver_id: int) -> bool:
    """
    自分の予約を削除し、後ろを詰める
    """
    with get_conn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT id, position FROM reservations WHERE item_id=%s AND reserver_id=%s",
                (item_id, reserver_id)
            )
            row = c.fetchone()
            if not row:
                return False
            rid, my_pos = int(row[0]), int(row[1])

            # 自分削除
            c.execute("DELETE FROM reservations WHERE id=%s", (rid,))

            # position詰め
            c.execute(
                """
                UPDATE reservations
                SET position = position - 1
                WHERE item_id=%s AND position > %s
                """,
                (item_id, my_pos)
            )
        conn.commit()
    return True

# ================================================================
# App本体
# ================================================================
def run_app():
    ensure_schema_and_migrate_once(MAX_MEMBERS)
    set_background("bg_gearswamp.png")

    st.session_state.setdefault("authed", False)
    st.session_state.setdefault("member_id", None)
    st.session_state.setdefault("member_name", "")
    st.session_state.setdefault("insta_input", "")
    st.session_state.setdefault("last_borrowed_item_id", None)

    # ------------------------------------------------------------
    # サイドバー（番号＋パス）
    # ------------------------------------------------------------
    slots = list_member_slots(MAX_MEMBERS)
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

            chosen_id = label_to_id[chosen_label]
            chosen_mem = get_member(chosen_id)  # 表示用（1回）
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

                    # 既存ユーザーなら insta をDBから引っ張る
                    db_insta = get_insta_by_id(chosen_id)
                    if db_insta:
                        st.session_state["insta_input"] = db_insta
                        insta_final = db_insta
                    else:
                        insta_final = (insta_in or "").strip().lstrip("@") or None
                        st.session_state["insta_input"] = insta_final or ""

                    # 未登録席の初回登録
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

                    # 既存席：未承認なら招待で有効化
                    if not m["is_active"]:
                        if invite == INVITE_CODE:
                            update_member_profile(chosen_id, name=None, insta=insta_final, activate=True)
                            m = get_member(chosen_id)
                            st.success("有効化しました。")
                        else:
                            st.session_state["authed"] = False
                            st.warning("未承認です。招待コードで有効化してください。")
                            st.stop()

                    # DBにinstaが空で入力があれば更新
                    if (not db_insta) and insta_final:
                        update_member_profile(chosen_id, name=None, insta=insta_final, activate=None)

                    st.session_state["member_id"] = chosen_id
                    st.session_state["member_name"] = m["name"] or ""
                    st.session_state["authed"] = True

            except Exception as e:
                st.session_state["authed"] = False
                st.error("認証処理でエラーが発生しました（詳細）")
                st.code(str(e))

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

    # ------------------------------------------------------------
    # 認証状態
    # ------------------------------------------------------------
    authed = bool(st.session_state.get("authed", False))
    member_id = st.session_state.get("member_id")
    member_name = st.session_state.get("member_name", "")
    is_admin = is_admin_name(member_name)

    # ------------------------------------------------------------
    # タブ
    # ------------------------------------------------------------
    tab_inv, tab_list, tab_bbs, tab_logs, tab_mem, tab_csv, tab_backup = st.tabs(
        [" 在庫登録", " 在庫/貸出/予約", " 掲示板", " 履歴", " メンバー", " CSV", " バックアップ"]
    )

    # ============================================================
    # 在庫登録
    # ============================================================
    with tab_inv:
        st.subheader("在庫登録")

        if not authed:
            st.info("登録にはサイドバーで認証が必要です。")
            st.stop()

        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("パーツ名")
            cat_sel = st.multiselect("カテゴリ（1件選択）", CATEGORIES, max_selections=1)
            cat = cat_sel[0] if cat_sel else ""
            size = st.text_input("サイズ")
            cond_sel = st.multiselect("状態（1件選択）", ["新品", "美品", "使用感あり", "要整備"], max_selections=1)
            cond = cond_sel[0] if cond_sel else ""

        with c2:
            # ownerは基本自分固定。管理者のみ変更可
            if is_admin:
                owners = list_member_slots(MAX_MEMBERS)
                owner_labels = [member_label(m) for m in owners if m["name"]]
                if owner_labels:
                    my_label = next((member_label(m) for m in owners if m["id"] == member_id and m["name"]), owner_labels[0])
                    idx = owner_labels.index(my_label) if my_label in owner_labels else 0
                    owner_label = st.selectbox("所有者（管理者のみ変更可）", owner_labels, index=idx)
                    owner_id = int(owner_label.split(":")[0].strip())
                else:
                    owner_id = member_id
                    st.caption("所有者：メンバー未登録のため自動設定")
            else:
                owner_id = member_id
                st.text_input("所有者", value=f"{member_id} : {member_name}", disabled=True)

            loc = st.text_input("保管場所")
            note = st.text_area("備考", height=80)
            pic = st.file_uploader("写真", type=["jpg", "jpeg", "png"])

        if st.button("登録", disabled=not (name and cat and cond)):
            blob = img_to_blob(Image.open(pic)) if pic else None
            db_exec(
                """
                INSERT INTO items(name,category,size,condition,owner,owner_id,location,note,status,photo)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'在庫あり',%s)
                """,
                (name, cat, size, cond, member_name, owner_id, loc, note, blob),
            )
            st.success("登録しました。")
            st.rerun()

    # ============================================================
    # 在庫/貸出/予約
    # ============================================================
    with tab_list:
        st.subheader("在庫一覧")

        kw = st.text_input("キーワード検索", "")
        f_cat = st.multiselect("カテゴリ絞り込み", CATEGORIES)
        f_owner = st.text_input("所有者で絞る（名前）", "")
        f_status = st.multiselect("状態で絞る", ["在庫あり", "貸出中", "整備中", "アーカイブ"])
        show_arch = st.checkbox("アーカイブも表示", value=False)

        def list_items():
            q = """
            SELECT
              i.id, i.name, i.category, i.size, i.condition,
              COALESCE(m.name, i.owner) AS owner_name,
              i.owner_id,
              i.location, i.note, i.status, i.photo
            FROM items i
            LEFT JOIN members m ON i.owner_id = m.id
            WHERE 1=1
            """
            p = []
            if kw:
                q += " AND (i.name ILIKE %s OR COALESCE(m.name, i.owner) ILIKE %s OR i.note ILIKE %s OR i.size ILIKE %s)"
                like = f"%{kw}%"
                p += [like, like, like, like]
            if f_cat:
                q += " AND i.category IN (" + ",".join(["%s"] * len(f_cat)) + ")"
                p += list(f_cat)
            if f_owner:
                q += " AND COALESCE(m.name, i.owner) ILIKE %s"
                p.append(f"%{f_owner}%")
            if f_status:
                q += " AND i.status IN (" + ",".join(["%s"] * len(f_status)) + ")"
                p += list(f_status)
            if not show_arch:
                q += " AND i.status <> 'アーカイブ'"
            q += " ORDER BY i.status DESC, i.category, i.name"
            return db_fetchall(q, tuple(p))

        items = list_items()
        if not items:
            st.caption("該当する在庫がありません。")

        for i, nm, cat, size, cond, owner_name, owner_id, loc, note, status, photo in items:
            # 予約状況取得
            resv = list_reservations_for_item(i)
            resv_cnt = len(resv)
            next_name = resv[0][1] if resv_cnt >= 1 else None

            with st.container(border=True):
                img = blob_to_img(photo)
                if img:
                    st.image(img, width=900)

                # タイトル行に予約バッジ
                title_line = f"**{nm}**"
                if resv_cnt > 0:
                    title_line += f' <span class="resv-badge">予約 {resv_cnt}/{MAX_RESERVATIONS_PER_ITEM}</span>'
                st.markdown(title_line, unsafe_allow_html=True)

                st.caption(
                    f"{cat} / サイズ:{size or '-'} / 状態:{cond} / 所有:{owner_name or '-'} / ステータス:{status}"
                )

                # 貸出中なら次の人を見せる
                if status == "貸出中" and next_name:
                    st.info(f"次の予約：{next_name}（{resv[0][0]}番目）")

                with st.expander("詳細", expanded=False):
                    st.caption(f"保管場所: {loc or '-'}")
                    st.write(note or "備考なし")

                    # 予約一覧（表示）
                    if resv_cnt == 0:
                        st.caption("予約：なし")
                    else:
                        lines = []
                        for pos, rname, rid, rdt in resv:
                            lines.append(f"{pos}) {rname}（{rdt or '-'}）")
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

                # 借りる
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
                        st.success("借用登録しました（返却目安90日）。このパーツをLINEで共有できます ")

                # 返却
                if c_r.button(" 返却", key=f"r{i}"):
                    if not authed:
                        st.warning("返却には認証が必要です。")
                    elif status != "貸出中":
                        st.warning("貸出中ではありません。")
                    else:
                        with get_conn() as conn2:
                            with conn2.cursor() as cur:
                                cur.execute(
                                    """
                                    SELECT id FROM loans
                                    WHERE item_id=%s AND status='貸出中'
                                    ORDER BY id DESC LIMIT 1
                                    """,
                                    (i,),
                                )
                                loan = cur.fetchone()
                                if loan:
                                    cur.execute(
                                        """
                                        UPDATE loans
                                        SET status='返却済', returned_date=%s
                                        WHERE id=%s
                                        """,
                                        (str(date.today()), loan[0]),
                                    )
                                    cur.execute("UPDATE items SET status='在庫あり' WHERE id=%s", (i,))
                            conn2.commit()
                        st.session_state["last_borrowed_item_id"] = None
                        st.success("返却しました（在庫ありに戻しました）")
                        st.rerun()

                # 予約（追加）
                if c_s.button(" 予約", key=f"s{i}"):
                    if not authed:
                        st.warning("予約には認証が必要です。")
                    else:
                        ok, msg = can_reserve(i, int(member_id))
                        if not ok:
                            st.warning(msg)
                        else:
                            pos = create_reservation(i, int(member_id), member_name)
                            st.success(f"{pos}番目で予約しました")
                            st.rerun()

                # 予約キャンセル（自分のみ）
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
                        st.success("状態を更新しました")
                        st.rerun()

                if c_arc.button(" アーカイブ", key=f"arc{i}"):
                    if not authed:
                        st.warning("アーカイブには認証が必要です。")
                    else:
                        db_exec("UPDATE items SET status='アーカイブ' WHERE id=%s", (i,))
                        st.rerun()

                with c_del:
                    confirm_del = st.checkbox("削除確認", key=f"cf{i}")
                    if st.button(" 削除", key=f"del{i}") and confirm_del:
                        if not authed:
                            st.warning("削除には認証が必要です。")
                        else:
                            db_exec("DELETE FROM items WHERE id=%s", (i,))
                            st.success("削除しました")
                            st.rerun()

                if st.session_state.get("last_borrowed_item_id") == i:
                    st.markdown(f"[ この貸出をLINEで共有]({line_url_item})")

    # ============================================================
    # 掲示板
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
                    st.success("投稿しました。")
                    st.rerun()
        else:
            st.caption("※ 投稿には認証が必要です。")

        st.markdown("### 投稿一覧")
        kw_b = st.text_input("キーワード検索（掲示板）", "")
        f_type = st.multiselect("種別で絞る", POST_TYPES)
        f_cat_b = st.multiselect("カテゴリで絞る", CATEGORIES)
        f_author = st.text_input("投稿者で絞る（名前）", "")

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

        posts = db_fetchall(q, tuple(p))
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

                    insta_user = None
                    if author_id:
                        insta_user = get_insta_by_id(int(author_id))

                    colx, coly, colz = st.columns(3)

                    if insta_user:
                        insta_url = f"https://instagram.com/{insta_user}"
                        colx.markdown(f"[ @{insta_user} へDM](<{insta_url}>)")

                    share_text = f"[Gear Swamp掲示板]\n[{ptype}] {title}\n{body}\nfrom {author_name}"
                    line_url = f"https://line.me/R/msg/text/?{quote(share_text)}"
                    coly.markdown(f"[ LINEで共有]({line_url})")

                    if is_admin and colz.button(" 投稿削除", key=f"del_post_{pid}"):
                        db_exec("DELETE FROM posts WHERE id=%s", (pid,))
                        st.success("投稿を削除しました。")
                        st.rerun()

    # ============================================================
    # 履歴
    # ============================================================
    with tab_logs:
        st.subheader("貸出・返却履歴")

        rows = db_fetchall(
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
        if not rows:
            st.caption("まだ履歴はありません。")
        else:
            for name, borrower_name, s, d, r, stt in rows:
                st.caption(
                    f"{name or '(削除済み)'} / 借り手:{borrower_name} / {s} → {r or '-'} / "
                    f"返却目安:{d or '-'} / 状態:{stt}"
                )

    # ============================================================
    # メンバー
    # ============================================================
    with tab_mem:
        st.subheader("メンバー（番号席）")

        st.markdown("### メンバー一覧")
        slots = list_member_slots(MAX_MEMBERS)
        for m in slots:
            nm = m["name"] if m["name"] else "未登録"
            st.markdown(f"- **{m['id']}** : {nm}  / @{(m['insta'] or '-') }  / {'有効' if m['is_active'] else '未承認'}")

        st.divider()
        st.subheader("管理者ツール")
        if not is_admin:
            st.caption("（admin_users に登録された管理者のみ）")
        else:
            st.markdown("### 1) 席の編集（名前・Insta・有効化）")
            tgt = st.selectbox("対象番号", [m["id"] for m in slots], index=0, key="admin_slot")
            curm = get_member(int(tgt))
            cur_name = curm["name"] if curm else ""
            cur_insta = curm["insta"] if curm else ""
            cur_act = curm["is_active"] if curm else False

            new_name = st.text_input("表示名（空で未登録に戻す）", value=cur_name or "")
            new_insta = st.text_input("Instagram（@不要）", value=cur_insta or "")
            new_active = st.checkbox("有効化", value=bool(cur_act))

            if st.button("更新保存", key="admin_save_slot"):
                update_member_profile(int(tgt), name=new_name, insta=new_insta, activate=new_active)
                st.success("更新しました。")
                st.rerun()

    # ============================================================
    # CSV
    # ============================================================
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
                                (pname, category, size, condition, owner_name, owner_id, location, note),
                            )
                            count += 1
                    conn.commit()
                st.success(f"{count} 件 登録しました。")
                st.rerun()

    # ============================================================
    # バックアップ
    # ============================================================
    with tab_backup:
        st.subheader(" バックアップ（Supabase運用）")
        st.info(
            "このアプリは **Supabase(Postgres)** を使っています。\n\n"
            "SQLiteファイルを保存する方式はありません。バックアップは Supabase 側で運用します。"
        )

        st.markdown("### 代替：CSVエクスポート（items + 予約数）")
        if st.button(" items をCSVでダウンロード"):
            rows = db_fetchall(
                """
                SELECT
                  i.id, i.name, i.category, i.size, i.condition,
                  COALESCE(m.name, i.owner) AS owner_name,
                  i.owner_id, i.location, i.note, i.status,
                  (SELECT COUNT(*) FROM reservations r WHERE r.item_id=i.id) AS reservation_count
                FROM items i
                LEFT JOIN members m ON i.owner_id=m.id
                ORDER BY i.id
                """
            )
            out = StringIO()
            w = csv.writer(out)
            w.writerow(["id","name","category","size","condition","owner_name","owner_id","location","note","status","reservation_count"])
            for r in rows:
                w.writerow(list(r))

            st.download_button(
                "CSVをダウンロード",
                data=out.getvalue(),
                file_name=f"items_export_{date.today().isoformat()}.csv",
                mime="text/csv",
            )

# ================================================================
# 実行（白画面防止：例外を画面に出す）
# ================================================================
try:
    run_app()
except Exception:
    st.error("🔥 アプリ実行中にエラーが発生しました（白画面防止のため詳細を表示します）")
    st.code(traceback.format_exc())
    raise
