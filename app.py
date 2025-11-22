# ================================================================
# app.py --- Gear Swamp（完全版）
# 招待制 / Admin管理 / 在庫・貸出・予約 / 掲示板 / CSV / 写真 /
# カテゴリ・所有者・状態フィルタ / 自分の名前変更 / 返却目安90日
# 掲示板投稿は管理者のみ削除可能
# 💾 DBバックアップ＆復元（手動）付き
# 完全ダークテーマ（OSのダーク/ライトに依存しない）
# 入力欄はすべて黒ベース＋白文字
# 「借りる」後にそのパーツだけ LINE共有ボタン表示
# ================================================================
import os
import csv
import sqlite3
from io import BytesIO, StringIO
from contextlib import contextmanager
from datetime import date, timedelta
from urllib.parse import quote
import base64

import qrcode
import streamlit as st
from PIL import Image
from dateutil.parser import parse as dt_parse

# ================================================================
# 設定
# ================================================================
DB_PATH = "parts_share.db"
SHARED_PASSCODE = st.secrets.get("passcode", "1234")
INVITE_CODE = st.secrets.get("invite_code", "join-123")
ADMIN_USERS = set(st.secrets.get("admin_users", []))  # 例: ["TETSUYA"]

def notify_line(msg: str) -> bool:
    """将来Messaging APIに差し替える用のダミー"""
    return False

CATEGORIES = [
    "フレーム/フォーク","ヘッドセット","ハンドル/ステム","グリップ/バーテープ",
    "サドル/シートポスト","ホイール","ハブ/リム/スポーク","タイヤ/チューブ",
    "ブレーキ（リム/ディスク）","ローター/パッド","シフター/ブレーキレバー",
    "ディレイラー（F/R）","クランク/BB","スプロケット/コグ",
    "チェーン/チェーンリング","ペダル","ケーブル/アウター","小物/ツール","その他"
]

POST_TYPES = ["試乗希望", "貸してほしい", "貸します", "譲ります", "雑談"]

st.set_page_config(
    page_title="Gear Swamp",
    page_icon="icon_gearswamp.png",  # リポジトリ直下に置く
    layout="wide",
)

# ================================================================
# テーマ＆背景（完全ダーク固定）
# ================================================================
def set_background(image_path: str):
    """背景画像を敷きつつ、完全ダークテーマで統一（OSテーマに依存しない）"""
    try:
        with open(image_path, "rb") as f:
            data = f.read()
        encoded = base64.b64encode(data).decode()
        st.markdown(
            f"""
            <style>
            /* OSのダーク/ライトに関係なく、常に黒背景＋白文字ベースにする */
            html, body {{
                background-color: #000000 !line-height:1.4;
                color: #f5f5f5 !important;
            }}

            /* アプリ本体に背景画像＋薄いオーバーレイ */
            .stApp {{
                background: url("data:image/png;base64,{encoded}") no-repeat center center fixed;
                background-size: cover;
            }}
            .stApp > div {{
                background-color: rgba(0,0,0,0.40);
            }}

            /* 全テキストを白系で統一（メイン＋サイドバー共通） */
            .stApp, .stApp p, .stApp li, .stApp span,
            .stApp label, .stApp div, .stMarkdown, .stTextInput label,
            .stCheckbox, .stRadio, .stSelectbox, .stMultiSelect, .stDateInput, .stTextArea {{
                color: #f5f5f5 !important;
            }}
            .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6 {{
                color: #ffffff !important;
            }}

            /* サイドバーの背景も暗く（スマホ/PC共通） */
            section[data-testid="stSidebar"] {{
                background-color: #111111 !important;
            }}

            /* 全ての入力系コンポーネントを黒ベース＋白文字に統一 */
            .stApp input,
            .stApp textarea,
            .stApp select {{
                color: #f5f5f5 !important;
                background-color: #222222 !important;
                border: 1px solid #555555 !important;
            }}
            /* BaseWebコンポーネント（TextInput / Select など） */
            .stApp div[data-baseweb="input"],
            .stApp div[data-baseweb="select"],
            .stApp div[data-baseweb="textarea"] {{
                background-color: #222222 !important;
                color: #f5f5f5 !important;
            }}
            .stApp div[data-baseweb="input"] input,
            .stApp div[data-baseweb="select"] * ,
            .stApp div[data-baseweb="textarea"] textarea {{
                color: #f55f5 !important;
            }}

            /* ファイルアップローダーも暗めに */
            [data-testid="stFileUploader"] > section {{
                background-color: #222222 !important;
                color: #f5f5f5 !important;
                border-radius: 0.5rem;
            }}
            [data-testid="stFileUploader"] div[role="button"] {{
                background-color: #333333 !important;
                color: #f5f5f5 !important;
            }}

            /* ボタン文字も白 */
            .stButton>button {{
                color: #f5f5f5 !important;
            }}
            </style>
            """,
            unsafe_allow_html=True,
        )
    except Exception as e:
        st.write("背景CSSエラー:", e)

set_background("bg_gearswamp.png")

# ================================================================
# DB & 共通ユーティリティ
# ================================================================
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()


def init_all_tables():
    with get_conn() as c:
        c.executescript(
            """
        CREATE TABLE IF NOT EXISTS members(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            insta TEXT,
            is_active INTEGER DEFAULT 0,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            category TEXT,
            size TEXT,
            condition TEXT,
            owner TEXT,
            location TEXT,
            note TEXT,
            status TEXT DEFAULT '在庫あり',
            photo BLOB
        );

        CREATE TABLE IF NOT EXISTS loans(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            borrower TEXT,
            start_date TEXT,
            due_date TEXT,
            reminder_days INTEGER,
            last_notified TEXT,
            returned_date TEXT,
            status TEXT DEFAULT '貸出中',
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reservations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            reserver TEXT,
            position INTEGER,
            reserved_date TEXT,
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT,
            ptype TEXT,
            category TEXT,
            title TEXT,
            body TEXT,
            created TEXT
        );

        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            member TEXT,
            action TEXT,
            detail TEXT
        );
        """
        )

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

# メンバー系
def upsert_member(name, insta=None, activate=False):
    if not name:
        return
    insta = (insta or "").strip().lstrip("@") or None
    with get_conn() as c:
        r = c.execute("SELECT id FROM members WHERE name=?", (name,)).fetchone()
        if r:
            c.execute(
                "UPDATE members SET insta=?, is_active=? WHERE name=?",
                (insta, 1 if activate else 0, name),
            )
        else:
            c.execute(
                "INSERT INTO members(name,insta,is_active,created_at) VALUES(?,?,?,?)",
                (name, insta, 1 if activate else 0, str(date.today())),
            )

def is_active(user):
    if not user:
        return False
    with get_conn() as c:
        r = c.execute("SELECT is_active FROM members WHERE name=?", (user,)).fetchone()
        return bool(r and r[0] == 1)

def get_insta(user):
    if not user:
        return None
    with get_conn() as c:
        r = c.execute("SELECT insta FROM members WHERE name=?", (user,)).fetchone()
        return r[0] if r and r[0] else None

def rename_member(old_name, new_name):
    if not old_name or not new_name or old_name == new_name:
        return False
    with get_conn() as c:
        c.execute("UPDATE members SET name=? WHERE name=?", (new_name, old_name))
        c.execute("UPDATE items SET owner=? WHERE owner=?", (new_name, old_name))
        c.execute("UPDATE loans SET borrower=? WHERE borrower=?", (new_name, old_name))
        c.execute("UPDATE reservations SET reserver=? WHERE reserver=?", (new_name, old_name))
        c.execute("UPDATE posts SET author=? WHERE author=?", (new_name, old_name))
    return True

def transfer_ownership(frm, to):
    if not frm or not to or frm == to:
        return 0
    with get_conn() as c:
        c.execute("UPDATE items SET owner=? WHERE owner=?", (to, frm))
        return c.total_changes

def delete_member(member_name):
    with get_conn() as c:
        c.execute("DELETE FROM members WHERE name=?", (member_name,))
        return c.total_changes

# 初期化
init_all_tables()

# ================================================================
# サイドバー：認証
# ================================================================
with st.sidebar:
    st.subheader("メンバー認証")

    default_name = st.session_state.get("member", "")
    member = st.text_input("あなたの名前", value=default_name)
    st.session_state["member"] = member

    passcode = st.text_input("共通パスコード", type="password")
    login = bool(member) and (passcode == SHARED_PASSCODE)

    insta = st.text_input("Instagram（任意・@不要）", value=get_insta(member) or "")
    invite = st.text_input("招待コード（初回のみ）", type="password")

    if member:
        active_now = is_active(member)
        if not active_now and invite == INVITE_CODE:
            upsert_member(member, insta, True)
            st.success("参加が有効化されました。")
        else:
            upsert_member(member, insta, active_now)

    if login and is_active(member):
        st.success(f"認証OK：{member}")
    elif member:
        st.warning("未承認です。招待コードを入力してください。")

    if is_admin(member):
        st.info("👑 管理者モード")

    st.divider()
    st.subheader("アプリ共有（QR）")
    app_url = st.text_input("このアプリのURL", value="")
    if app_url:
        qr = qrcode.make(app_url)
        st.image(qr, caption="このQRを仲間に配布", use_column_width=True)

# ================================================================
# タブ構成
# ================================================================
tab_inv, tab_list, tab_logs, tab_mem, tab_bbs, tab_csv, tab_backup = st.tabs(
    ["➕在庫登録", "📦在庫/貸出/予約", "📜履歴", "👥メンバー", "🗣掲示板", "📥CSV", "💾バックアップ"]
)

# ================================================================
# 在庫登録タブ
# ================================================================
with tab_inv:
    st.subheader("在庫登録")
    c1, c2 = st.columns(2)
    with c1:
        name = st.text_input("パーツ名")
        cat = st.selectbox("カテゴリ", CATEGORIES)
        size = st.text_input("サイズ")
        cond = st.selectbox("状態", ["新品", "美品", "使用感あり", "要整備"])
    with c2:
        owner = st.text_input("所有者", value=member)
        loc = st.text_input("保管場所")
        note = st.text_area("備考", height=80)
        pic = st.file_uploader("写真", type=["jpg", "jpeg", "png"])

    if st.button("登録", disabled=not (login and is_active(member) and name)):
        blob = img_to_blob(Image.open(pic)) if pic else None
        with get_conn() as c:
            c.execute(
                """INSERT INTO items(name,category,size,condition,owner,location,note,status,photo)
                   VALUES(?,?,?,?,?,?,?,'在庫あり',?)""",
                (name, cat, size, cond, owner, loc, note, blob),
            )
        st.success("登録しました。")

# ================================================================
# 在庫/貸出/予約タブ
# ================================================================
with tab_list:
    st.subheader("在庫一覧")

    kw = st.text_input("キーワード検索", "")
    f_cat = st.multiselect("カテゴリ絞り込み", CATEGORIES)
    f_owner = st.text_input("所有者で絞る", "")
    f_status = st.multiselect("状態で絞る", ["在庫あり", "貸出中", "整備中", "アーカイブ"])
    show_arch = st.checkbox("アーカイブも表示", value=False)

    def list_items():
        with get_conn() as c:
            q = "SELECT id,name,category,size,condition,owner,location,note,status,photo FROM items WHERE 1=1"
            p = []
            if kw:
                q += " AND (name LIKE ? OR owner LIKE ? OR note LIKE ? OR size LIKE ?)"
                like = f"%{kw}%"
                p += [like, like, like, like]
            if f_cat:
                q += f" AND category IN ({','.join(['?']*len(f_cat))})"
                p += f_cat
            if f_owner:
                q += " AND owner LIKE ?"
                p.append(f"%{f_owner}%")
            if f_status:
                q += f" AND status IN ({','.join(['?']*len(f_status))})"
                p += f_status
            if not show_arch:
                q += " AND status!='アーカイブ'"
            q += " ORDER BY status DESC, category, name"
            return c.execute(q, p).fetchall()

    last_borrowed_id = st.session_state.get("last_borrowed_item_id")

    for i, nm, cat, size, cond, owner, loc, note, status, photo in list_items():
        with st.container(border=True):
            img = blob_to_img(photo)
            if img:
                st.image(img, use_column_width=True)

            st.markdown(f"**{nm}**")
            st.caption(f"{cat} / サイズ:{size or '-'} / 状態:{cond} / 所有:{owner} / ステータス:{status}")

            with st.expander("詳細", expanded=False):
                st.caption(f"保管場所: {loc or '-'}")
                st.write(note or "備考なし")

            # このパーツのLINE共有テキスト（在庫／貸出両用に利用）
            item_share_text = (
                f"[Gear Swamp]\n"
                f"パーツ: {nm}\n"
                f"カテゴリ: {cat}\n"
                f"サイズ: {size or '-'}\n"
                f"状態: {cond}\n"
                f"所有者: {owner}\n"
                f"保管場所: {loc or '-'}\n"
                f"備考: {note or '-'}"
            )
            line_url = f"https://line.me/R/msg/text/?{quote(item_share_text)}"

            col1, col2, col3, col4 = st.columns(4)

            # 借りる（返却目安 90日）
            if col1.button("📥 借りる", key=f"b{i}") and login and is_active(member) and status != "貸出中":
                today = date.today()
                due = compute_due(str(today), 90)
                with get_conn() as conn:
                    conn.execute(
                        """INSERT INTO loans(item_id,borrower,kyrtics
                            start_date,due_date,reminder_days,status)
                           VALUES(?,?,?,?,90,'貸出中')""",
                        (i, member, str(today), str(due)),
                    )
                    conn.execute("UPDATE items SET status='貸出中' WHERE id=?", (i,))
                st.session_state["last_borrowed_item_id"] = i
                st.success("借用登録しました（返却目安90日）。このパーツをLINEで共有できます👇")

            # 返却
            if col2.button("📤 返却", key=f"r{i}") and login and is_active(member) and status == "貸出中":
                with get_conn() as conn:
                    loan = conn.execute(
                        "SELECT id FROM loans WHERE item_id=? AND status='貸出中' ORDER BY id DESC LIMIT 1",
                        (i,),
                    ).fetchone()
                    if loan:
                        conn.execute(
                            "UPDATE loans SET status='返却済', returned_date=? WHERE id=?",
                            (str(date.today()), loan[0]),
                        )
                    conn.execute("UPDATE items SET status='在庫あり' WHERE id=?", (i,))
                st.session_state["last_borrowed_id"] = None
                st.success("返却しました（在庫ありに戻しました）")
                st.rerun()

            # 予約
            if col3.button("⏳ 予約", key=f"s{i}") and login and is_active(member):
                with get_conn() as conn:
                    pos = conn.execute(
                        "SELECT COALESCE(MAX(position),0)+1 FROM reservations WHERE item_id=?",
                        (i,),
                    ).fetchone()[0]
                    if pos <= 3:
                        conn.execute(
                            "INSERT INTO reservations(item_id,reserver,position,reserved_date) VALUES(?,?,?,?)",
                            (i, member, pos, str(date.today())),
                        )
                        st.success(f"{pos}番目で予約しました")
                        st.rerun()
                    else:
                        st.warning("予約枠がいっぱいです")

            # 状態変更
            new_st = col4.selectbox(
                "状態変更",
                ["変更しない", "在庫あり", "貸出中", "整備中", "アーカイブ"],
                key=f"st{i}",
            )
            if col4.button("🔧 更新", key=f"upd{i}") and login and is_active(member) and new_st != "変更しない":
                with get_conn() as conn:
                    conn.execute("UPDATE items SET status=? WHERE id=?", (new_st, i))
                st.success("状態を更新しました")
                st.rerun()

            # 「最後に借りたパーツ」のみ LINE共有ボタン表示
            if st.session_state.get("last_borrowed_item_id") == i:
                st.markdown(f"[📩 この貸出をLINEで共有]({line_url})")

            st.divider()
            ax, ay, az = st.columns(3)
            if ax.button("📦 アーカイブ", key=f"arc{i}") and login and is_active(member):
                with get_conn() as conn:
                    conn.execute("UPDATE items SET status='アーカイブ' WHERE id=?", (i,))
                st.rerun()

            confirm = ay.checkbox("削除確認", key=f"cf{i}")
            if az.button("🗑️ 削除", key=f"del{i}") and confirm and login and is_active(member):
                with get_conn() as conn:
                    conn.execute("DELETE FROM items WHERE id=?", (i,))
                st.success("削除しました")
                st.rerun()


# ================================================================
# 履歴タブ
# ================================================================
with tab_logs:
    st.subheader("貸出・返却履歴")
    with get_conn() as c:
        rows = c.execute(
            """SELECT i.name,l.borrower,l.start_date,l.due_date,l.returned_date,l.status
               FROM loans l LEFT JOIN items i ON l.item_id=i.id
               ORDER BY l.id DESC"""
        ).fetchall()
    if not rows:
        st.caption("まだ履歴はありません。")
    else:
        for name, b, s, d, r, stt in rows:
            st.caption(
                f"{name or '(削除済み)'} / 借り手:{b} / {s} → {r or '-'} / 返却目安:{d or '-'} / 状態:{stt}"
            )


# ================================================================
# メンバータブ（自分の名前変更＋管理者ツール）
# ================================================================
with tab_mem:
    st.subheader("メンバー")

    st.markdown("### ✏️ 自分の名前を変更")
    if login and is_active(member):
        new_my_name = st.text_input("新しい自分の名前", value=member, key="self_rename")
        if st.button("📝 自分の名前を変更", key="self_rename_btn"):
            if new_my_name and new_my_name != member:
                ok = rename_member(member, new_my_name)
                if ok:
                    st.success(f"{member} → {new_my_name} に変更しました。")
                    st.info("※ 次回からサイドバーの『あなたの名前』も新しい名前でログインしてください。")
                else:
                    st.error("名前の変更に失敗しました。")
    else:
        st.caption("※ 認証済みメンバーだけ自分の名前を変更できます。")

    st.markdown("### メンバー一覧")
    with get_conn() as c:
        ms = c.execute("SELECT name, insta, is_active FROM members ORDER BY name").fetchall()
    for n, i, a in ms:
        st.markdown(f"- **{n}** {'✅' if a else '⛔'} @{i or '-'}")

    st.divider()
    st.subheader("👑 管理者ツール")
    if not is_admin(member):
        st.caption("（admin_users に登録された管理者のみ）")
    else:
        with get_conn() as c:
            members_all = c.execute("SELECT name, insta, is_active FROM members ORDER BY name").fetchall()
        names = [m[0] for m in members_all] or ["(なし)"]

        col1, col2 = st.columns(2)

        # 1) メンバー編集
        with col1:
            st.markdown("### 1) メンバー編集")
            sel = st.selectbox("対象メンバー", names, index=0)
            new_name = st.text_input(
                "新しい表示名（リネーム）", value=sel if sel != "(なし)" else "", key="admin_rename"
            )
            new_insta = st.text_input("Instagram（@不要）", value=get_insta(sel) or "", key="admin_insta")
            new_active = st.checkbox("有効化", value=is_active(sel), key="admin_active")
            if st.button("💾 変更保存", key="admin_save"):
                if sel != "(なし)":
                    if new_name and new_name != sel:
                        ok = rename_member(sel, new_name)
                        if not ok:
                            st.error("名前変更に失敗しました。")
                        sel = new_name
                    upsert_member(sel, new_insta, activate=new_active)
                    st.success("更新しました。")
                    st.rerun()

        # 2) 所有アイテム移管 & 3) 削除
        with col2:
            st.markdown("### 2) 所有アイテムの移管")
            from_m = st.selectbox("移管元", names, index=0, key="admin_from")
            to_m = st.selectbox(
                "移管先", [n for n in names if n != from_m],
                index=0 if len(names) > 1 else 0, key="admin_to"
            )
            if st.button("🔁 移管実行", key="admin_transfer"):
                cnt = transfer_ownership(from_m, to_m)
                st.success(f"{cnt}件のアイテムを {from_m} → {to_m} に移管しました。")
                st.rerun()

            st.markdown("### 3) メンバー削除")
            del_m = st.selectbox("削除対象", names, index=0, key="admin_del")
            confirm = st.text_input("確認用にメンバー名を入力", key="admin_del_confirm")
            st.caption(
                "※ 削除しても既存の貸出・予約・在庫のテキストはそのまま残ります。"
                "完全に消したい場合は、先にリネームしてから削除してください。"
            )
            if st.button("🗑️ メンバー削除", key="admin_del_btn"):
                if confirm != del_m:
                    st.error("確認用の名前が一致しません。")
                else:
                    deleted = delete_member(del_m)
                    st.success(f"{del_m} を削除しました（削除件数: {deleted}）。")
                    st.rerun()


# ================================================================
# CSVタブ（在庫のみ）
# ================================================================
with tab_csv:
    st.subheader("CSV一括登録（在庫）")

    templ = StringIO()
    w = csv.writer(templ)
    w.writerow(["name", "category", "size", "condition", "owner", "location", "note"])
    w.writerow(
        ["700C Front Wheel", "ホイール", "700C/100x12", "美品", "TETSUYA", "自宅A", "ハブDT350"]
    )
    w.writerow(
        ["11s Cassette 11-28", "スプロケット/コグ", "HG 11s", "使用感あり", "TETSUYA", "自宅B", "軽微摩耗"]
    )
    st.download_button(
        "テンプレCSVをダウンロード",
        templ.getvalue(),
        file_name="parts_template.csv",
        mime="text/csv",
    )

    up = st.file_uploader("CSVを選択（UTF-8推奨）", type=["csv"])
    if up and st.button("一括登録を実行"):
        text = up.read().decode("utf-8", "ignore")
        reader = csv.DictReader(StringIO(text))
        count = 0
        with get_conn() as c:
            for row in reader:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                category = (row.get("category") or "").strip() or "その他"
                size = (row.get("size") or "").strip()
                condition = (row.get("condition") or "").strip() or "使用感あり"
                owner = (row.get("owner") or "").strip() or member
                location = (row.get("location") or "").strip()
                note = (row.get("note") or "").strip()
                c.execute(
                    """INSERT INTO items(name,category,size,condition,owner,location,note,status,photo)
                       VALUES(?,?,?,?,?,?,?,'在庫あり',NULL)""",
                    (name, category, size, condition, owner, location, note),
                )
                count += 1
        st.success(f"{count} 件 登録しました。")


# ================================================================
# 💾 バックアップタブ（DB丸ごとバックアップ＆復元）
# ================================================================
with tab_backup:
    st.subheader("💾 DBバックアップ & 復元")

    st.markdown(
        "この機能は **`parts_share.db` をそのままバックアップ／復元** します。"
        "在庫・貸出・メンバー・掲示板など、すべてのデータが含まれます。"
    )

    st.markdown("### 1) バックアップをダウンロード")

    if os.path.exists(DB_PATH):
        with open(DB_PATH, "rb") as f:
            db_bytes = f.read()
        default_name = f"gearswamp_backup_{date.today().isoformat()}.db"
        st.download_button(
            "📥 DBバックアップをダウンロード",
            data=db_bytes,
            file_name=default_name,
            mime="application/octet-stream",
        )
    else:
        st.warning("DBファイルがまだ存在していません。（登録がまだない可能性があります）")

    st.divider()
    st.markdown("### 2) バックアップから復元")

    st.caption(
        "⚠️ **注意**：復元すると現在のDBは上書きされます。元に戻せるように、先にバックアップのダウンロードを推奨します。"
    )

    up_db = st.file_uploader("復元用バックアップファイル（.db）を選択", type=["db"])
    if up_db and st.button("⚠️ このバックアップで復元する"):
        try:
            bytes_data = up_db.read()
            with open(DB_PATH, "wb") as f:
                f.write(bytes_data)
            st.success("バックアップから復元しました。画面を再読み込みします。")
            st.rerun()
        except Exception as e:
            st.error(f"復元中にエラーが発生しました: {e}")



























