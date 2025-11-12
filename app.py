# app.py --- Gear Swamp (スマホ最適・LINE通知・招待制・リマインダー・CSV登録対応)

import os, csv, sqlite3, qrcode, requests
from io import BytesIO, StringIO
from contextlib import contextmanager
from datetime import date, timedelta
from PIL import Image
from dateutil.parser import parse as dt_parse
import streamlit as st

DB_PATH = "parts_share.db"
SHARED_PASSCODE = st.secrets.get("passcode", "1234")
INVITE_CODE = st.secrets.get("invite_code", "join-123")
LINE_TOKEN = st.secrets.get("line_notify_token", None)

CATEGORIES = [
    "フレーム/フォーク","ヘッドセット","ハンドル/ステム","グリップ/バーテープ",
    "サドル/ピラー","ホイール","ハブ/リム/スポーク","タイヤ/チューブ",
    "ブレーキ（リム/ディスク）","ローター/パッド","シフター/ブレーキレバー",
    "ディレイラー（F/R）","クランク/BB","スプロケット/フリー",
    "チェーン/チェーンリング","ペダル","ケーブル/アウター","小物/ツール","その他"
]

# --- DBユーティリティ ---
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

# --- ツール類 ---
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

def notify_reservation(item_id,reserver):
    with get_conn() as c:
        r=c.execute("SELECT i.name,l.borrower FROM items i "
                    "LEFT JOIN loans l ON l.item_id=i.id AND l.status='貸出中' "
                    "WHERE i.id=? ORDER BY l.id DESC LIMIT 1",(item_id,)).fetchone()
    if not r: return
    name,borrower=r; notify_line(f"【予約】{name}\n現在:{borrower or '-'}\n予約者:{reserver}")

def auto_assign_next(item_id):
    with get_conn() as c:
        first=c.execute("SELECT id,reserver FROM reservations WHERE item_id=? ORDER BY position LIMIT 1",(item_id,)).fetchone()
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

def upsert_member(name,insta=None,activate=False):
    if not name: return
    insta=(insta or "").strip().lstrip("@") or None
    with get_conn() as c:
        r=c.execute("SELECT id FROM members WHERE name=?",(name,)).fetchone()
        if r: c.execute("UPDATE members SET insta=?,is_active=? WHERE name=?",
                        (insta,1 if activate else 0,name))
        else: c.execute("INSERT INTO members(name,insta,is_active,created_at)VALUES(?,?,?,?)",
                        (name,insta,1 if activate else 0,str(date.today())))
def is_active(n):
    with get_conn() as c:
        r=c.execute("SELECT is_active FROM members WHERE name=?",(n,)).fetchone()
        return bool(r and r[0]==1)
def get_insta(n):
    with get_conn() as c:
        r=c.execute("SELECT insta FROM members WHERE name=?",(n,)).fetchone()
        return r[0] if r and r[0] else None

def compute_due(start,days):
    try: s=dt_parse(start).date()
    except: s=date.today()
    return s+timedelta(days=days)
def check_reminders():
    t=date.today(); sent=0
    with get_conn() as c:
        rows=c.execute("SELECT id,item_id,borrower,start_date,due_date,reminder_days,last_notified "
                       "FROM loans WHERE status='貸出中'").fetchall()
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

# --- UI設定 ---
st.set_page_config(page_title="Gear Swamp",page_icon="icon_gearswamp.png",layout="wide")
st.markdown("<style>.stButton>button{width:100%;padding:0.8rem;font-weight:600;}</style>",unsafe_allow_html=True)
st.title("🛠️ Gear Swamp Collective")

init_db()

# --- サイドバー ---
with st.sidebar:
    st.subheader("メンバー認証")
    member=st.text_input("名前",value="")
    pw=st.text_input("パスコード",type="password")
    login=member and pw==SHARED_PASSCODE
    insta=st.text_input("Instagram（任意・@不要）",value=get_insta(member) or "")
    invite=st.text_input("招待コード（初回のみ）",type="password")
    if member:
        act=is_active(member)
        if not act and invite==INVITE_CODE:
            upsert_member(member,insta,True); st.success("有効化しました。")
        else: upsert_member(member,insta,act)
    if login and is_active(member): st.success(f"認証OK：{member}")
    elif member: st.warning("未承認。招待コードを入力。")
    else: st.caption("閲覧自由／操作には認証が必要。")

    st.divider(); st.subheader("リマインダー")
    if st.button("今すぐ送信チェック"):
        st.success(f"{check_reminders()} 件通知しました。")

    st.divider(); st.subheader("QR共有")
    url=st.text_input("アプリURL",value="")
    if url:
        img=qrcode.make(url); st.image(img,caption="ホーム追加でアプリ化",use_column_width=True)

# --- タブ構成 ---
tabs=st.tabs(["➕在庫登録","📦在庫/貸出/予約","📜履歴","👥メンバー","📥CSV一括登録"])
tab_inv,tab_list,tab_logs,tab_mem,tab_csv=tabs

# --- 在庫登録 ---
with tab_inv:
    st.subheader("在庫登録")
    c1,c2=st.columns(2)
    with c1:
        name=st.text_input("パーツ名")
        cat=st.selectbox("カテゴリ",CATEGORIES)
        size=st.text_input("サイズ")
        cond=st.selectbox("状態",["新品","美品","使用感あり","要整備"])
    with c2:
        owner=st.text_input("所有者",value=member)
        loc=st.text_input("保管場所")
        note=st.text_area("備考",height=80)
        pic=st.file_uploader("写真",type=["jpg","jpeg","png"])
    if st.button("登録",disabled=not(login and is_active(member) and name and owner)):
        blob=img_to_blob(Image.open(pic)) if pic else None
        with get_conn() as c:
            c.execute("""INSERT INTO items(name,category,size,condition,owner,location,note,status,photo)
                         VALUES(?,?,?,?,?,?,?,'在庫あり',?)""",
                         (name,cat,size,cond,owner,loc,note,blob))
        log(member,"ADD",name); st.success("登録しました。")

# --- 在庫/貸出/予約 ---
with tab_list:
    st.subheader("在庫一覧")
    kw=st.text_input("キーワード検索","")
    def items():
        with get_conn() as c:
            q="SELECT id,name,category,owner,status,photo FROM items WHERE 1=1"
            p=[]
            if kw: q+=" AND name LIKE ?"; p.append(f"%{kw}%")
            q+=" ORDER BY status DESC,category"; return c.execute(q,p).fetchall()
    for i,name,cat,owner,status,photo in items():
        with st.container(border=True):
            img=blob_to_img(photo)
            if img: st.image(img,use_column_width=True)
            st.markdown(f"**{name}** / {cat} / {owner} / 状態:{status}")
            a,b,c=st.columns(3)
            if a.button("借りる",key=f"b{i}") and login and is_active(member):
                today=date.today(); due=compute_due(str(today),7)
                with get_conn() as conn:
                    conn.execute("INSERT INTO loans(item_id,borrower,start_date,due_date,reminder_days,status)"
                                 "VALUES(?,?,?,?,7,'貸出中')",(i,member,str(today),str(due)))
                    conn.execute("UPDATE items SET status='貸出中' WHERE id=?",(i,))
                log(member,"BORROW",name); st.success("借用OK"); st.rerun()
            if b.button("返却",key=f"r{i}") and login:
                with get_conn() as conn:
                    loan=conn.execute("SELECT id FROM loans WHERE item_id=? AND status='貸出中' ORDER BY id DESC LIMIT 1",(i,)).fetchone()
                    if loan: conn.execute("UPDATE loans SET status='返却済',returned_date=? WHERE id=?",(str(date.today()),loan[0]))
                nxt=auto_assign_next(i)
                st.success(f"返却完了{'→次:'+nxt if nxt else ''}"); st.rerun()
            if c.button("予約",key=f"s{i}") and login:
                with get_conn() as conn:
                    n=conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM reservations WHERE item_id=?",(i,)).fetchone()[0]
                    if n<=3:
                        conn.execute("INSERT INTO reservations(item_id,reserver,position,reserved_date)VALUES(?,?,?,?)",(i,member,n,str(date.today())))
                        notify_reservation(i,member); st.success(f"{n}番目で予約完了"); st.rerun()

# --- 履歴 ---
with tab_logs:
    st.subheader("貸出履歴")
    with get_conn() as c:
        rows=c.execute("SELECT i.name,l.borrower,l.start_date,l.due_date,l.returned_date,l.status "
                       "FROM loans l LEFT JOIN items i ON l.item_id=i.id ORDER BY l.id DESC").fetchall()
    for n,b,s,d,r,stt in rows:
        st.caption(f"{n} / {b} / {s}→{r or '-'} / 状態:{stt}")

# --- メンバー ---
with tab_mem:
    st.subheader("メンバー一覧")
    with get_conn() as c:
        ms=c.execute("SELECT name,insta,is_active FROM members ORDER BY name").fetchall()
    for n,i,a in ms:
        st.markdown(f"- **{n}** {'✅' if a else '⛔'} @{i or '-'}")

# --- CSV一括登録 ---
with tab_csv:
    st.subheader("CSV登録")
    templ=StringIO(); csv.writer(templ).writerows([
        ["name","category","size","condition","owner","location","note"],
        ["700C Front Wheel","ホイール","700C","美品","TETSUYA","自宅","DT350"]
    ])
    st.download_button("テンプレCSVをDL",templ.getvalue(),"template.csv","text/csv")
    up=st.file_uploader("CSVをアップロード",type="csv")
    if up and st.button("登録実行"):
        text=up.read().decode("utf-8","ignore")
        r=csv.DictReader(StringIO(text)); ctn=0
        with get_conn() as c:
            for row in r:
                if not row.get("name"): continue
                c.execute("""INSERT INTO items(name,category,size,condition,owner,location,note,status,photo)
                             VALUES(?,?,?,?,?,?,?,'在庫あり',NULL)""",
                          (row["name"],row.get("category"),row.get("size"),row.get("condition"),
                           row.get("owner"),row.get("location"),row.get("note")))
                ctn+=1
        st.success(f"{ctn} 件登録しました。")
