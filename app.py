# app.py --- Gear Swamp（招待制・LINE通知・リマインダー・CSV一括・アーカイブ/削除・管理者UI）
import os, csv, sqlite3, qrcode, requests
from io import BytesIO, StringIO
from contextlib import contextmanager
from datetime import date, timedelta
from PIL import Image
from dateutil.parser import parse as dt_parse
import streamlit as st

# =========================
# 設定
# =========================
DB_PATH = "parts_share.db"
SHARED_PASSCODE = st.secrets.get("passcode", "1234")
INVITE_CODE     = st.secrets.get("invite_code", "join-123")
LINE_TOKEN      = st.secrets.get("line_notify_token", None)
ADMIN_USERS     = set(st.secrets.get("admin_users", []))

CATEGORIES = [
    "フレーム/フォーク","ヘッドセット","ハンドル/ステム","グリップ/バーテープ",
    "サドル/ピラー","ホイール","ハブ/リム/スポーク","タイヤ/チューブ",
    "ブレーキ（リム/ディスク）","ローター/パッド","シフター/ブレーキレバー",
    "ディレイラー（F/R）","クランク/BB","スプロケット/フリー",
    "チェーン/チェーンリング","ペダル","ケーブル/アウター","小物/ツール","その他"
]

st.set_page_config(
    page_title="Gear Swamp",
    page_icon="icon_gearswamp.png",  # リポジトリ直下に置くとホーム追加アイコンに反映
    layout="wide"
)
st.markdown("<style>.stButton>button{width:100%;padding:.8rem;font-weight:600}</style>", unsafe_allow_html=True)
st.title("🛠️ Gear Swamp Collective")

# =========================
# DBユーティリティ
# =========================
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
    finally:
        conn.commit(); conn.close()

def init_db():
    with get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS members(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE, insta TEXT, is_active INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, category TEXT, size TEXT, condition TEXT,
            owner TEXT, location TEXT, note TEXT, status TEXT DEFAULT '在庫あり', photo BLOB);
        CREATE TABLE IF NOT EXISTS loans(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER, borrower TEXT, start_date TEXT, due_date TEXT,
            reminder_days INTEGER, last_notified TEXT,
            returned_date TEXT, status TEXT DEFAULT '貸出中',
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS reservations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER, reserver TEXT, position INTEGER, reserved_date TEXT,
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, member TEXT, action TEXT, detail TEXT);
        """)

def log(m,a,d):
    with get_conn() as c:
        c.execute("INSERT INTO logs(ts,member,action,detail) VALUES(?,?,?,?)",
                  (str(date.today()),m,a,d))

# =========================
# 共通ツール
# =========================
def img_to_blob(img,max_px=1400):
    if not img: return None
    img=img.convert("RGB"); img.thumbnail((max_px,max_px))
    b=BytesIO(); img.save(b,format="JPEG",quality=85, optimize=True)
    return b.getvalue()

def blob_to_img(blob,thumb_px=900):
    if not blob: return None
    i=Image.open(BytesIO(blob)); i.thumbnail((thumb_px,thumb_px)); return i

def notify_line(msg):
    if not LINE_TOKEN: return False
    try:
        requests.post("https://notify-api.line.me/api/notify",
            headers={"Authorization":f"Bearer {LINE_TOKEN}"},
            data={"message":msg},timeout=5)
        return True
    except: return False

def compute_due(start,days):
    try: s=dt_parse(start).date()
    except: s=date.today()
    return s+timedelta(days=days)

def check_reminders():
    t=date.today(); sent=0
    with get_conn() as c:
        rows=c.execute("""SELECT id,item_id,borrower,start_date,due_date,reminder_days,last_notified
                          FROM loans WHERE status='貸出中'""").fetchall()
        for lid,i,b,s,d,rd,ln in rows:
            if rd is None: continue
            if not d:
                d=compute_due(s,int(rd)); c.execute("UPDATE loans SET due_date=? WHERE id=?",(str(d),lid))
            due=dt_parse(d).date()
            if t>=due and (not ln or dt_parse(ln).date()<t):
                name=c.execute("SELECT name FROM items WHERE id=?",(i,)).fetchone()
                name=name[0] if name else f"item#{i}"
                if notify_line(f"【返却リマインド】{name}\n借り手:{b}\n借用日:{s}\n返却目安:{d}"):
                    c.execute("UPDATE loans SET last_notified=? WHERE id=?",(str(t),lid)); sent+=1
    return sent

# =========================
# メンバー操作
# =========================
def upsert_member(name, insta=None, activate=False):
    if not name: return
    insta=(insta or "").strip().lstrip("@") or None
    with get_conn() as c:
        r=c.execute("SELECT id FROM members WHERE name=?",(name,)).fetchone()
        if r:
            c.execute("UPDATE members SET insta=?, is_active=? WHERE name=?",
                      (insta, 1 if activate else 0, name))
        else:
            c.execute("INSERT INTO members(name,insta,is_active,created_at) VALUES(?,?,?,?)",
                      (name, insta, 1 if activate else 0, str(date.today())))

def is_active(user):
    if not user: return False
    with get_conn() as c:
        r=c.execute("SELECT is_active FROM members WHERE name=?",(user,)).fetchone()
        return bool(r and r[0]==1)

def get_insta(user):
    if not user: return None
    with get_conn() as c:
        r=c.execute("SELECT insta FROM members WHERE name=?",(user,)).fetchone()
        return r[0] if r and r[0] else None

def is_admin(user):
    return bool(user) and user in ADMIN_USERS

# =========================
# 予約/繰上げ通知
# =========================
def notify_reservation(item_id, reserver):
    with get_conn() as c:
        r=c.execute("""SELECT i.name, l.borrower
                       FROM items i
                       LEFT JOIN loans l ON l.item_id=i.id AND l.status='貸出中'
                       WHERE i.id=? ORDER BY l.id DESC LIMIT 1""",(item_id,)).fetchone()
    if not r: return
    name, borrower = r
    notify_line(f"【予約】{name}\n現在:{borrower or '-'}\n予約者:{reserver}")

def auto_assign_next(item_id):
    with get_conn() as c:
        first=c.execute("""SELECT id, reserver FROM reservations
                           WHERE item_id=? ORDER BY position LIMIT 1""",(item_id,)).fetchone()
        if not first:
            c.execute("UPDATE items SET status='在庫あり' WHERE id=?",(item_id,)); return None
        rid,res=first; today=date.today()
        c.execute("""INSERT INTO loans(item_id,borrower,start_date,status)
                     VALUES(?,?,?, '貸出中')""",(item_id,res,str(today)))
        c.execute("UPDATE items SET status='貸出中' WHERE id=?",(item_id,))
        c.execute("DELETE FROM reservations WHERE id=?",(rid,))
        c.execute("UPDATE reservations SET position=position-1 WHERE item_id=? AND position>1",(item_id,))
    notify_line(f"【自動繰上げ】{res} さんへ貸出に切替。")
    return res

# =========================
# Admin ヘルパー
# =========================
def rename_member(old_name, new_name):
    if not old_name or not new_name or old_name==new_name: return False
    with get_conn() as c:
        c.execute("UPDATE members SET name=? WHERE name=?", (new_name, old_name))
        c.execute("UPDATE items SET owner=? WHERE owner=?", (new_name, old_name))
        c.execute("UPDATE loans SET borrower=? WHERE borrower=?", (new_name, old_name))
        c.execute("UPDATE reservations SET reserver=? WHERE reserver=?", (new_name, old_name))
    return True

def transfer_ownership(frm, to):
    if not frm or not to or frm==to: return 0
    with get_conn() as c:
        c.execute("UPDATE items SET owner=? WHERE owner=?", (to, frm))
        cnt = c.total_changes
    return cnt

def delete_member(member_name):
    with get_conn() as c:
        c.execute("DELETE FROM members WHERE name=?", (member_name,))
        deleted = c.total_changes
    return deleted

# =========================
# 初期化
# =========================
init_db()

# =========================
# サイドバー（認証・QR・リマインド）
# =========================
with st.sidebar:
    st.subheader("メンバー認証")
    member = st.text_input("あなたの名前", value="")
    passcode = st.text_input("共通パスコード", type="password")
    login = bool(member) and (passcode == SHARED_PASSCODE)

    insta = st.text_input("Instagram（任意・@不要）", value=get_insta(member) or "")
    invite = st.text_input("招待コード（初回のみ）", type="password")
    if member:
        active_now = is_active(member)
        if not active_now and invite == INVITE_CODE:
            upsert_member(member, insta, True); st.success("参加が有効化されました。")
        else:
            upsert_member(member, insta, active_now)

    if login and is_active(member):
        st.success(f"認証OK：{member}")
    elif member:
        st.warning("未承認です。招待コードを入力してください。")
    else:
        st.caption("閲覧自由／操作には認証と承認が必要です。")

    if is_admin(member):
        st.info("👑 管理者モード")

    st.divider(); st.subheader("リマインダー")
    if st.button("今すぐ送信チェック"):
        st.success(f"{check_reminders()} 件通知しました。")

    st.divider(); st.subheader("アプリ共有（QR）")
    app_url = st.text_input("このアプリのURL（任意）", value="")
    if app_url:
        img=qrcode.make(app_url)
        st.image(img, caption="このQRを仲間に配布", use_column_width=True)
        st.caption("※ スマホで読み取り → ホームに追加で擬似アプリ化")

# =========================
# タブ
# =========================
tab_inv, tab_list, tab_logs, tab_mem, tab_csv = st.tabs(
    ["➕在庫登録","📦在庫/貸出/予約","📜履歴","👥メンバー","📥CSV一括登録"]
)

# -------------------------
# 在庫登録
# -------------------------
with tab_inv:
    st.subheader("在庫登録")
    c1,c2 = st.columns(2)
    with c1:
        name = st.text_input("パーツ名")
        cat  = st.selectbox("カテゴリ", CATEGORIES)
        size = st.text_input("サイズ/規格")
        cond = st.selectbox("状態", ["新品","美品","使用感あり","要整備"])
    with c2:
        owner = st.text_input("所有者", value=member)
        loc   = st.text_input("保管場所")
        note  = st.text_area("備考", height=80)
        pic   = st.file_uploader("写真", type=["jpg","jpeg","png"])

    if st.button("登録", disabled=not(login and is_active(member) and name and owner)):
        blob = img_to_blob(Image.open(pic)) if pic else None
        with get_conn() as c:
            c.execute("""INSERT INTO items(name,category,size,condition,owner,location,note,status,photo)
                         VALUES(?,?,?,?,?,?,?,'在庫あり',?)""",
                      (name,cat,size,cond,owner,loc,note,blob))
        log(member,"ADD_ITEM",name); st.success("登録しました。")

# -------------------------
# 在庫/貸出/予約
# -------------------------
with tab_list:
    st.subheader("在庫一覧")
    kw = st.text_input("キーワード検索", "")
    show_arch = st.checkbox("アーカイブも表示", value=False)

    def list_items():
        with get_conn() as c:
            q = "SELECT id,name,category,owner,status,photo FROM items WHERE 1=1"
            p = []
            if kw:
                q += " AND name LIKE ?"; p.append(f"%{kw}%")
            if not show_arch:
                q += " AND (status IS NULL OR status!='アーカイブ')"
            q += " ORDER BY status DESC, category, name"
            return c.execute(q, p).fetchall()

    for i, nm, cat, owner, status, photo in list_items():
        with st.container(border=True):
            img = blob_to_img(photo)
            if img: st.image(img, use_column_width=True)
            st.markdown(f"**{nm}** / {cat} / 所有: {owner} / 状態: {status}")

            # ボタン群
            a,b,c,d = st.columns(4)

            # 借りる（返却目安 = 7日デフォルト）
            if a.button("📥 借りる", key=f"b{i}") and login and is_active(member) and status!="貸出中":
                today = date.today(); due = compute_due(str(today), 7)
                with get_conn() as conn:
                    conn.execute("""INSERT INTO loans(item_id,borrower,start_date,due_date,reminder_days,status)
                                    VALUES(?,?,?,?,7,'貸出中')""",(i,member,str(today),str(due)))
                    conn.execute("UPDATE items SET status='貸出中' WHERE id=?",(i,))
                log(member,"BORROW",nm); st.success("借用OK"); st.rerun()

            # 返却（予約繰上げ対応）
            if b.button("📤 返却", key=f"r{i}") and login and is_active(member) and status=="貸出中":
                with get_conn() as conn:
                    loan=conn.execute("""SELECT id FROM loans WHERE item_id=? AND status='貸出中'
                                         ORDER BY id DESC LIMIT 1""",(i,)).fetchone()
                    if loan:
                        conn.execute("UPDATE loans SET status='返却済', returned_date=? WHERE id=?",
                                     (str(date.today()), loan[0]))
                nxt = auto_assign_next(i)
                st.success(f"返却完了 {'→ 次:'+nxt if nxt else '(在庫へ戻し)'}"); st.rerun()

            # 予約（最大3名）
            if c.button("⏳ 予約", key=f"s{i}") and login and is_active(member):
                with get_conn() as conn:
                    pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM reservations WHERE item_id=?",(i,)).fetchone()[0]
                    if pos<=3:
                        conn.execute("""INSERT INTO reservations(item_id,reserver,position,reserved_date)
                                        VALUES(?,?,?,?)""",(i,member,pos,str(date.today())))
                        notify_reservation(i,member)
                        st.success(f"{pos}番目で予約完了"); st.rerun()
                    else:
                        st.warning("予約枠がいっぱいです。")

            # 整備/在庫切替（簡易）
            new_status = d.selectbox("整備/在庫切替", ["変更しない","在庫あり","貸出中","整備中"], key=f"st{i}")
            if d.button("🔧 更新", key=f"upd{i}") and login and is_active(member) and new_status!="変更しない":
                with get_conn() as conn:
                    conn.execute("UPDATE items SET status=? WHERE id=?", (new_status, i))
                log(member,"STATUS_UPDATE",f"{nm}=>{new_status}"); st.success("更新しました。"); st.rerun()

            # アーカイブ & 削除（安全に下側へ）
            st.markdown("---")
            colX, colY, colZ = st.columns(3)
            if colX.button("📦 アーカイブ", key=f"arc{i}") and login and is_active(member):
                with get_conn() as conn:
                    conn.execute("UPDATE items SET status='アーカイブ' WHERE id=?", (i,))
                log(member,"ARCHIVE_ITEM",nm); st.success("アーカイブしました。"); st.rerun()

            confirm = colY.checkbox("本当に削除（履歴も消える）", key=f"cf{i}")
            if colZ.button("🗑️ 削除（注意）", key=f"del{i}") and confirm and login and is_active(member):
                with get_conn() as conn:
                    conn.execute("DELETE FROM items WHERE id=?", (i,))
                log(member,"DELETE_ITEM",nm); st.success("削除しました。"); st.rerun()

# -------------------------
# 履歴
# -------------------------
with tab_logs:
    st.subheader("貸出・返却履歴")
    with get_conn() as c:
        rows=c.execute("""SELECT i.name,l.borrower,l.start_date,l.due_date,l.returned_date,l.status
                          FROM loans l LEFT JOIN items i ON l.item_id=i.id
                          ORDER BY l.id DESC""").fetchall()
    if not rows:
        st.caption("まだ履歴はありません。")
    else:
        for name, b, s, d, r, stt in rows:
            st.caption(f"{name or '(削除済み)'} / {b} / {s} → {r or '-'} / 返却目安:{d or '-'} / 状態:{stt}")

# -------------------------
# メンバー（一般+管理者UI）
# -------------------------
with tab_mem:
    st.subheader("メンバー一覧")
    with get_conn() as c:
        ms=c.execute("SELECT name, insta, is_active FROM members ORDER BY name").fetchall()
    for n, i, a in ms:
        st.markdown(f"- **{n}** {'✅' if a else '⛔'} @{i or '-'}")

    st.divider()
    st.subheader("👑 管理者ツール")
    if not is_admin(member):
        st.caption("（管理者のみ操作可能）")
    else:
        with get_conn() as c:
            members_all = c.execute("SELECT name, insta, is_active FROM members ORDER BY name").fetchall()
        names = [m[0] for m in members_all] or ["(なし)"]

        # 1) メンバー編集
        st.markdown("### 1) メンバー編集")
        col1, col2 = st.columns(2)
        with col1:
            sel = st.selectbox("対象メンバー", names, index=0)
            new_name = st.text_input("新しい表示名（リネーム）", value=sel if sel!="(なし)" else "")
            new_insta = st.text_input("Instagram（@不要）", value=get_insta(sel) or "")
            new_active = st.checkbox("有効化（チェック＝参加可）", value=is_active(sel))
            if st.button("💾 変更を保存"):
                if sel!="(なし)":
                    if new_name and new_name != sel:
                        ok = rename_member(sel, new_name)
                        if not ok: st.error("名前変更に失敗しました。")
                        sel = new_name
                    upsert_member(sel, new_insta, activate=new_active)
                    st.success("更新しました。"); st.rerun()

        with col2:
            # 2) 所有アイテム一括移管
            st.markdown("### 2) 所有アイテムの一括移管")
            from_m = st.selectbox("移管元", names, index=0, key="admin_from")
            to_m   = st.selectbox("移管先", [n for n in names if n != from_m], key="admin_to")
            if st.button("🔁 移管を実行"):
                cnt = transfer_ownership(from_m, to_m)
                st.success(f"{cnt}件のアイテム所有者を {from_m} → {to_m} に変更しました。")
                st.rerun()

            # 3) メンバー削除
            st.markdown("### 3) メンバー削除")
            del_m = st.selectbox("削除対象", names, index=0, key="admin_del")
            confirm = st.text_input("確認用にメンバー名を入力", key="admin_del_confirm")
            st.caption("※ 削除しても、既存の貸出・予約・在庫のテキストはそのまま残ります。完全匿名化したい場合は先にリネームしてから削除してください。")
            if st.button("🗑️ メンバー削除"):
                if confirm != del_m:
                    st.error("確認用の名前が一致しません。")
                else:
                    deleted = delete_member(del_m)
                    st.success(f"{del_m} を削除しました（削除件数: {deleted}）。")
                    st.rerun()

# -------------------------
# CSV一括登録
# -------------------------
with tab_csv:
    st.subheader("CSV一括登録（初期在庫向け）")
    templ = StringIO()
    w = csv.writer(templ)
    w.writerow(["name","category","size","condition","owner","location","note"])
    w.writerow(["700C Front Wheel","ホイール","700C/100x12","美品","TETSUYA","自宅A","ハブDT350"])
    w.writerow(["11s Cassette 11-28","スプロケット/フリー","HG 11s","使用感あり","TETSUYA","自宅B","軽微摩耗"])
    st.download_button("テンプレCSVをダウンロード", templ.getvalue(), "parts_template.csv", "text/csv")

    up = st.file_uploader("CSVを選択（UTF-8推奨）", type=["csv"])
    if up and st.button("一括登録を実行"):
        text = up.read().decode("utf-8", "ignore")
        reader = csv.DictReader(StringIO(text))
        count = 0
        with get_conn() as c:
            for row in reader:
                name=(row.get("name") or "").strip()
                if not name: continue
                category=(row.get("category") or "").strip() or "その他"
                size=(row.get("size") or "").strip()
                condition=(row.get("condition") or "").strip() or "使用感あり"
                owner=(row.get("owner") or "").strip() or member
                location=(row.get("location") or "").strip()
                note=(row.get("note") or "").strip()
                c.execute("""INSERT INTO items(name,category,size,condition,owner,location,note,status,photo)
                             VALUES(?,?,?,?,?,?,?,'在庫あり',NULL)""",
                          (name,category,size,condition,owner,location,note))
                count += 1
        log(member or "importer","CSV_IMPORT",f"{count} items")
        st.success(f"{count} 件 登録しました。")


