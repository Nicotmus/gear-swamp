# ================================================================
# app.py --- Gear Swamp（安定版・編集機能付き）
# 招待制 / Admin管理 / 在庫・貸出・予約 / 掲示板 / CSV / 写真 /
# カテゴリ・所有者・状態フィルタ / 自分の名前変更 / 返却目安90日 / 編集機能
# ================================================================
import os
import csv
import sqlite3
import html
from io import BytesIO, StringIO
from contextlib import contextmanager
from datetime import date, datetime, timedelta
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
SHARED_PASSCODE = st.secrets.get("passcode", "1234")  # 共通パスコード
ADMIN_NAMES = st.secrets.get("admins", "admin").split(",")  # 管理者名（カンマ区切り）

DEFAULT_RETURN_DAYS = 90  # 返却目安日数

CATEGORY_CHOICES = [
    "フレーム",
    "フォーク",
    "ホイール",
    "タイヤ",
    "ドライブトレイン",
    "ブレーキ",
    "ハンドル / ステム",
    "サドル / シートポスト",
    "ラック / キャリア",
    "バッグ / バイクパッキング",
    "ライト / 電装",
    "ウェア / シューズ",
    "その他",
]

STATUS_CHOICES = [
    "在庫あり",
    "貸出中",
    "要確認",
    "廃棄予定",
]

# ================================================================
# DBユーティリティ
# ================================================================
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """必要なテーブルとカラムを作成 / 追加"""
    with get_conn() as conn:
        c = conn.cursor()

        # ユーザー
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE,
                is_admin    INTEGER DEFAULT 0,
                created_at  TEXT
            )
            """
        )

        # パーツ
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS parts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                category    TEXT,
                status      TEXT,
                owner       TEXT,
                location    TEXT,
                note        TEXT,
                image_b64   TEXT,
                created_at  TEXT,
                updated_at  TEXT,
                is_deleted  INTEGER DEFAULT 0
            )
            """
        )

        # 貸出
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS loans (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                part_id         INTEGER,
                borrower_name   TEXT,
                borrower_instagram TEXT,
                start_date      TEXT,
                due_date        TEXT,
                returned_at     TEXT
            )
            """
        )

        # 予約（キュー）
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                part_id         INTEGER,
                reserver_name   TEXT,
                reserver_instagram TEXT,
                created_at      TEXT,
                is_active       INTEGER DEFAULT 1
            )
            """
        )

        # 掲示板
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                author_name TEXT,
                body        TEXT,
                created_at  TEXT
            )
            """
        )

        # 既存DBに不足カラムがあれば追加（雑に守りのALTER）
        # parts.updated_at
        c.execute(
            "PRAGMA table_info(parts)"
        )
        cols = [row[1] for row in c.fetchall()]
        if "updated_at" not in cols:
            c.execute("ALTER TABLE parts ADD COLUMN updated_at TEXT")
        if "is_deleted" not in cols:
            c.execute("ALTER TABLE parts ADD COLUMN is_deleted INTEGER DEFAULT 0")
        if "image_b64" not in cols:
            c.execute("ALTER TABLE parts ADD COLUMN image_b64 TEXT")

        conn.commit()


# ================================================================
# ユーザー関連
# ================================================================
def get_or_create_user(name: str):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE name = ?", (name,))
        row = c.fetchone()
        if row:
            return row

        is_admin = 1 if name in ADMIN_NAMES else 0
        now = datetime.now().isoformat()
        c.execute(
            "INSERT INTO users (name, is_admin, created_at) VALUES (?, ?, ?)",
            (name, is_admin, now),
        )
        conn.commit()
        c.execute("SELECT * FROM users WHERE name = ?", (name,))
        return c.fetchone()


def rename_current_user(old_name: str, new_name: str):
    if not new_name:
        return False
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET name = ? WHERE name = ?", (new_name, old_name))
        # 貸出や予約履歴の名前も変えておく
        c.execute("UPDATE loans SET borrower_name = ? WHERE borrower_name = ?", (new_name, old_name))
        c.execute("UPDATE reservations SET reserver_name = ? WHERE reserver_name = ?", (new_name, old_name))
        c.execute("UPDATE posts SET author_name = ? WHERE author_name = ?", (new_name, old_name))
        conn.commit()
    return True


# ================================================================
# パーツ取得・更新（編集用）
# ================================================================
def get_part_by_id(part_id: int):
    """ID指定でパーツ1件を取得"""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM parts WHERE id = ?", (part_id,))
        row = cur.fetchone()
    return row


def update_part_editable_fields(
    part_id: int,
    name: str,
    category: str,
    status: str,
    owner: str,
    location: str,
    note: str,
):
    """編集画面から更新する項目をUPDATE"""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE parts
               SET name      = ?,
                   category  = ?,
                   status    = ?,
                   owner     = ?,
                   location  = ?,
                   note      = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (name, category, status, owner, location, note, now, part_id),
        )
        conn.commit()


# ================================================================
# パーツ登録・貸出・予約ロジック
# ================================================================
def create_part(name, category, status, owner, location, note, image_b64):
    now = datetime.now().isoformat()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO parts
                (name, category, status, owner, location, note, image_b64, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, category, status, owner, location, note, image_b64, now, now),
        )
        conn.commit()


def get_active_loan_for_part(part_id: int):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            """
            SELECT * FROM loans
             WHERE part_id = ?
               AND returned_at IS NULL
             ORDER BY start_date DESC
             LIMIT 1
            """,
            (part_id,),
        )
        return c.fetchone()


def get_active_reservations_for_part(part_id: int):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            """
            SELECT * FROM reservations
             WHERE part_id = ?
               AND is_active = 1
             ORDER BY created_at ASC
            """,
            (part_id,),
        )
        return c.fetchall()


def lend_part_to_user(part_id: int, user_name: str, insta: str | None):
    """貸出開始（既に貸出中なら何もしない）"""
    if get_active_loan_for_part(part_id):
        return False

    today = date.today()
    due = today + timedelta(days=DEFAULT_RETURN_DAYS)

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO loans
                (part_id, borrower_name, borrower_instagram, start_date, due_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                part_id,
                user_name,
                insta or "",
                today.isoformat(),
                due.isoformat(),
            ),
        )
        # ステータスも「貸出中」に更新
        c.execute(
            "UPDATE parts SET status = ?, updated_at = ? WHERE id = ?",
            ("貸出中", datetime.now().isoformat(), part_id),
        )
        conn.commit()

    return True


def return_part(part_id: int):
    """返却処理"""
    loan = get_active_loan_for_part(part_id)
    if not loan:
        return False

    today = date.today().isoformat()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE loans SET returned_at = ? WHERE id = ?",
            (today, loan["id"]),
        )

        # 予約があればステータスを変えず、なければ「在庫あり」に戻す
        reservations = get_active_reservations_for_part(part_id)
        new_status = "要確認" if reservations else "在庫あり"
        c.execute(
            "UPDATE parts SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, datetime.now().isoformat(), part_id),
        )

        conn.commit()
    return True


def create_reservation(part_id: int, user_name: str, insta: str | None):
    now = datetime.now().isoformat()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO reservations
                (part_id, reserver_name, reserver_instagram, created_at, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (part_id, user_name, insta or "", now),
        )
        # ステータスを「要確認」にする程度に留める
        c.execute(
            "UPDATE parts SET status = ?, updated_at = ? WHERE id = ?",
            ("要確認", datetime.now().isoformat(), part_id),
        )
        conn.commit()


def cancel_reservation(reservation_id: int, user_name: str, is_admin: bool):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,))
        row = c.fetchone()
        if not row:
            return False

        # 自分かAdminのみキャンセル可
        if (row["reserver_name"] != user_name) and (not is_admin):
            return False

        c.execute(
            "UPDATE reservations SET is_active = 0 WHERE id = ?",
            (reservation_id,),
        )
        conn.commit()
    return True


# ================================================================
# 画像関連（DBにはbase64文字列で保存）
# ================================================================
def image_to_base64(img_file) -> str | None:
    if img_file is None:
        return None
    img = Image.open(img_file)
    buf = BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return b64


def base64_to_image(b64: str | None):
    if not b64:
        return None
    data = base64.b64decode(b64.encode("utf-8"))
    return Image.open(BytesIO(data))


# ================================================================
# UI: ログイン
# ================================================================
def require_login():
    st.title("Gear Swamp")

    if "user" in st.session_state:
        return st.session_state["user"]

    st.info("パスコードと名前を入力して入場してください。")

    with st.form("login_form"):
        passcode = st.text_input("パスコード", type="password")
        name = st.text_input("名前（ニックネーム可）")
        submitted = st.form_submit_button("入場")

    if submitted:
        if passcode != SHARED_PASSCODE:
            st.error("パスコードが違います。")
            st.stop()

        if not name.strip():
            st.error("名前を入力してください。")
            st.stop()

        user = get_or_create_user(name.strip())
        st.session_state["user"] = dict(user)
        st.success(f"{name} でログインしました。")
        st.experimental_rerun()

    st.stop()


# ================================================================
# UI: 在庫一覧 ＋ 貸出 / 予約 ＋ 編集
# ================================================================
def render_parts_tab(user):
    st.header("在庫・貸出・予約")

    current_name = user["name"]
    is_admin = bool(user["is_admin"])

    # Instagram IDを任意で保持
    if "insta" not in st.session_state:
        st.session_state["insta"] = ""
    insta = st.text_input("Instagram ID（任意）", value=st.session_state["insta"])
    st.session_state["insta"] = insta

    # ------------------------------------------------------------
    # フィルタ
    # ------------------------------------------------------------
    with st.expander("フィルタ", expanded=False):
        col_f1, col_f2, col_f3 = st.columns(3)
        with col_f1:
            category_filter = st.selectbox(
                "カテゴリ",
                ["すべて"] + CATEGORY_CHOICES,
                key="parts_filter_category",
            )
        with col_f2:
            owner_filter = st.text_input(
                "所有者（部分一致）",
                key="parts_filter_owner",
                placeholder="例）TETSUYA",
            )
        with col_f3:
            status_filter = st.selectbox(
                "状態",
                ["すべて"] + STATUS_CHOICES,
                key="parts_filter_status",
            )

    # ------------------------------------------------------------
    # データ取得
    # ------------------------------------------------------------
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        query = "SELECT * FROM parts WHERE is_deleted = 0"
        params = []

        if category_filter != "すべて":
            query += " AND category = ?"
            params.append(category_filter)

        if status_filter != "すべて":
            query += " AND status = ?"
            params.append(status_filter)

        if owner_filter:
            query += " AND owner LIKE ?"
            params.append(f"%{owner_filter}%")

        query += " ORDER BY created_at DESC"
        c.execute(query, params)
        rows = c.fetchall()

    # ------------------------------------------------------------
    # 編集ターゲット
    # ------------------------------------------------------------
    edit_target_id = st.session_state.get("edit_target_id")

    # ------------------------------------------------------------
    # 一覧表示
    # ------------------------------------------------------------
    if not rows:
        st.info("条件に合うパーツがありません。")
    else:
        for row in rows:
            part_id = row["id"]
            loan = get_active_loan_for_part(part_id)
            reservations = get_active_reservations_for_part(part_id)

            with st.container(border=True):
                top_cols = st.columns([2, 4, 3, 2])
                # 画像
                with top_cols[0]:
                    img = base64_to_image(row["image_b64"])
                    if img:
                        st.image(img, use_container_width=True)
                    else:
                        st.caption("写真なし")

                # 基本情報
                with top_cols[1]:
                    st.markdown(f"**{row['name']}**")
                    st.caption(f"カテゴリ: {row['category']} / 状態: {row['status']}")
                    st.text(f"所有者: {row['owner']}")
                    st.text(f"保管場所: {row['location']}")
                    if row["note"]:
                        st.text(f"メモ: {row['note']}")

                # 貸出・予約情報
                with top_cols[2]:
                    if loan:
                        start = loan["start_date"]
                        due = loan["due_date"]
                        st.warning(f"貸出中: {loan['borrower_name']}")
                        st.caption(f"開始: {start} / 返却目安: {due}")
                    else:
                        st.success("現在は在庫あり")

                    if reservations:
                        st.caption("予約者:")
                        for r in reservations:
                            st.text(f"- {r['reserver_name']}")

                # ボタン類
                with top_cols[3]:
                    # 借りる
                    if not loan:
                        if st.button("借りる", key=f"lend_{part_id}"):
                            ok = lend_part_to_user(part_id, current_name, insta)
                            if ok:
                                st.success("貸出登録しました。")
                                st.rerun()
                            else:
                                st.error("すでに貸出中です。")
                    else:
                        # 返却ボタン（借りた本人 or Admin）
                        if (loan["borrower_name"] == current_name) or is_admin:
                            if st.button("返却する", key=f"return_{part_id}"):
                                return_part(part_id)
                                st.success("返却処理しました。")
                                st.rerun()

                    # 予約ボタン
                    if st.button("予約する", key=f"reserve_{part_id}"):
                        create_reservation(part_id, current_name, insta)
                        st.success("予約を登録しました。")
                        st.rerun()

                    # 自分の予約があればキャンセルボタン表示
                    for r in reservations:
                        if r["reserver_name"] == current_name or is_admin:
                            if st.button(
                                "予約キャンセル",
                                key=f"cancel_res_{r['id']}",
                            ):
                                cancel_reservation(r["id"], current_name, is_admin)
                                st.success("予約をキャンセルしました。")
                                st.rerun()
                                break

                    # 編集ボタン
                    if st.button("編集", key=f"edit_{part_id}"):
                        st.session_state["edit_target_id"] = part_id
                        edit_target_id = part_id

    # ------------------------------------------------------------
    # 編集フォーム
    # ------------------------------------------------------------
    if edit_target_id is not None:
        part = get_part_by_id(edit_target_id)
        st.markdown("---")
        st.subheader(f"パーツ情報の編集（ID: {edit_target_id}）")

        if part is None:
            st.warning("選択したパーツが見つかりませんでした。")
        else:
            with st.form("edit_part_form"):
                name = st.text_input("パーツ名", value=part["name"] or "")

                category = st.selectbox(
                    "カテゴリ",
                    CATEGORY_CHOICES,
                    index=(
                        CATEGORY_CHOICES.index(part["category"])
                        if part["category"] in CATEGORY_CHOICES
                        else 0
                    ),
                )

                status = st.selectbox(
                    "状態",
                    STATUS_CHOICES,
                    index=(
                        STATUS_CHOICES.index(part["status"])
                        if part["status"] in STATUS_CHOICES
                        else 0
                    ),
                )

                owner = st.text_input("所有者", value=part["owner"] or "")
                location = st.text_input("保管場所", value=part["location"] or "")
                note = st.text_area("メモ", value=part["note"] or "")

                col_f1, col_f2 = st.columns(2)
                with col_f1:
                    submitted = st.form_submit_button("保存する")
                with col_f2:
                    cancel = st.form_submit_button("キャンセル")

            if submitted:
                update_part_editable_fields(
                    part_id=edit_target_id,
                    name=name,
                    category=category,
                    status=status,
                    owner=owner,
                    location=location,
                    note=note,
                )
                st.success("パーツ情報を更新しました。")
                st.session_state["edit_target_id"] = None
                st.rerun()

            if cancel:
                st.session_state["edit_target_id"] = None
                st.rerun()


# ================================================================
# UI: パーツ登録
# ================================================================
def render_register_tab(user):
    st.header("パーツ登録")

    with st.form("register_part_form"):
        name = st.text_input("パーツ名")
        category = st.selectbox("カテゴリ", CATEGORY_CHOICES)
        status = st.selectbox("状態", STATUS_CHOICES, index=0)
        owner = st.text_input("所有者", value=user["name"])
        location = st.text_input("保管場所", value="")
        note = st.text_area("メモ", value="", height=80)
        image_file = st.file_uploader("写真（任意）", type=["png", "jpg", "jpeg"])

        submitted = st.form_submit_button("登録")

    if submitted:
        if not name.strip():
            st.error("パーツ名は必須です。")
            return
        img_b64 = image_to_base64(image_file)
        create_part(
            name=name.strip(),
            category=category,
            status=status,
            owner=owner.strip() or user["name"],
            location=location.strip(),
            note=note.strip(),
            image_b64=img_b64,
        )
        st.success("登録しました。")


# ================================================================
# UI: 掲示板
# ================================================================
def render_board_tab(user):
    st.header("掲示板")

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM posts ORDER BY created_at DESC")
        posts = c.fetchall()

    with st.form("post_form"):
        body = st.text_area("ひとことメモ / 情報共有", height=80)
        submitted = st.form_submit_button("投稿")
    if submitted:
        if body.strip():
            with get_conn() as conn:
                c = conn.cursor()
                c.execute(
                    "INSERT INTO posts (author_name, body, created_at) VALUES (?, ?, ?)",
                    (user["name"], body.strip(), datetime.now().isoformat()),
                )
                conn.commit()
            st.success("投稿しました。")
            st.rerun()
        else:
            st.warning("何か書いてください。")

    st.markdown("---")
    if not posts:
        st.info("まだ投稿はありません。")
    else:
        for p in posts:
            with st.container(border=True):
                st.markdown(f"**{p['author_name']}**")
                st.caption(p["created_at"])
                st.write(p["body"])


# ================================================================
# UI: メンバー / アカウント
# ================================================================
def render_members_tab(user):
    st.header("メンバー / アカウント")

    st.subheader("自分の名前変更")
    with st.form("rename_form"):
        new_name = st.text_input("新しい名前", value=user["name"])
        submitted = st.form_submit_button("変更する")
    if submitted:
        new_name = new_name.strip()
        if not new_name:
            st.error("名前を入力してください。")
        else:
            if rename_current_user(user["name"], new_name):
                st.success("名前を変更しました。再読み込みします。")
                st.session_state.pop("user", None)
                st.rerun()
            else:
                st.error("名前の変更に失敗しました。")

    st.markdown("---")
    st.subheader("メンバー一覧")

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM users ORDER BY created_at ASC")
        users = c.fetchall()

    for u in users:
        with st.container(border=True):
            st.markdown(f"**{u['name']}**")
            st.caption(f"権限: {'Admin' if u['is_admin'] else 'Member'} / 登録日: {u['created_at']}")


# ================================================================
# UI: バックアップ（DBダウンロード）
# ================================================================
def render_backup_tab(user):
    st.header("バックアップ / CSV")

    st.subheader("SQLite DBダウンロード")
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "rb") as f:
            db_bytes = f.read()
        st.download_button(
            "parts_share.db をダウンロード",
            data=db_bytes,
            file_name=f"gearswamp_backup_{date.today().isoformat()}.db",
            mime="application/octet-stream",
        )
    else:
        st.warning("DBファイルが見つかりません。")

    st.markdown("---")
    st.subheader("パーツ一覧のCSVダウンロード")

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            "SELECT id, name, category, status, owner, location, note, created_at, updated_at FROM parts WHERE is_deleted = 0"
        )
        rows = c.fetchall()

    if rows:
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(
            ["id", "name", "category", "status", "owner", "location", "note", "created_at", "updated_at"]
        )
        for r in rows:
            writer.writerow(
                [
                    r["id"],
                    r["name"],
                    r["category"],
                    r["status"],
                    r["owner"],
                    r["location"],
                    r["note"],
                    r["created_at"],
                    r["updated_at"],
                ]
            )

        st.download_button(
            "CSVをダウンロード",
            data=output.getvalue().encode("utf-8-sig"),
            file_name=f"gearswamp_parts_{date.today().isoformat()}.csv",
            mime="text/csv",
        )
    else:
        st.info("パーツが登録されていません。")


# ================================================================
# メイン
# ================================================================
def main():
    st.set_page_config(page_title="Gear Swamp", layout="wide")
    init_db()

    user = require_login()

    st.sidebar.markdown(f"👤 ログイン中: **{user['name']}** ({'Admin' if user['is_admin'] else 'Member'})")
    if st.sidebar.button("ログアウト"):
        st.session_state.pop("user", None)
        st.experimental_rerun()

    tab_names = ["在庫", "登録", "掲示板", "メンバー", "バックアップ"]
    tabs = st.tabs(tab_names)

    with tabs[0]:
        render_parts_tab(user)
    with tabs[1]:
        render_register_tab(user)
    with tabs[2]:
        render_board_tab(user)
    with tabs[3]:
        render_members_tab(user)
    with tabs[4]:
        render_backup_tab(user)


if __name__ == "__main__":
    main()
