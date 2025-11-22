import streamlit as st
import pandas as pd
from datetime import datetime
from pathlib import Path

# ==========================
# Basic settings
# ==========================
st.set_page_config(
    page_title="Gear Swamp",
    page_icon="🦎",
    layout="wide"
)

DATA_FILE = Path("gear_swamp_data.csv")

# ==========================
# Utility functions
# ==========================
def load_data() -> pd.DataFrame:
    if not DATA_FILE.exists():
        df = pd.DataFrame(
            columns=[
                "created_at",
                "owner_name",
                "instagram_id",
                "category",
                "brand",
                "item_name",
                "size",
                "condition",
                "location",
                "status",
                "note"
            ]
        )
        return df
    df = pd.read_csv(DATA_FILE)
    # Safety: ensure all expected columns exist
    expected_cols = [
        "created_at",
        "owner_name",
        "instagram_id",
        "category",
        "brand",
        "item_name",
        "size",
        "condition",
        "location",
        "status",
        "note"
    ]
    for c in expected_cols:
        if c not in df.columns:
            df[c] = ""
    # Keep column order
    df = df[expected_cols]
    return df


def save_data(df: pd.DataFrame) -> None:
    df.to_csv(DATA_FILE, index=False)


def make_message(row: pd.Series) -> str:
    """A案：コピペ用メッセージ生成"""
    lines = []
    lines.append("【Gear Swamp パーツ相談】")
    if isinstance(row.get("item_name"), str) and row["item_name"]:
        lines.append(f"パーツ名：{row['item_name']}")
    if isinstance(row.get("brand"), str) and row["brand"]:
        lines.append(f"ブランド：{row['brand']}")
    if isinstance(row.get("category"), str) and row["category"]:
        lines.append(f"カテゴリ：{row['category']}")
    if isinstance(row.get("size"), str) and row["size"]:
        lines.append(f"サイズ：{row['size']}")
    if isinstance(row.get("condition"), str) and row["condition"]:
        lines.append(f"状態：{row['condition']}")
    if isinstance(row.get("location"), str) and row["location"]:
        lines.append(f"エリア：{row['location']}")
    if isinstance(row.get("note"), str) and row["note"]:
        lines.append(f"ひとこと：{row['note']}")

    lines.append("")
    lines.append("このパーツについて相談させてください。")
    lines.append("（Gear Swamp 経由）")

    return "\n".join(lines)


# ==========================
# Auth (simple member gate)
# ==========================
def check_member() -> bool:
    # 招待コードは secrets にあればそちらを優先
    invite_code_default = "gearswamp2025"
    invite_code_setting = st.secrets.get("INVITE_CODE", invite_code_default)

    if "is_member" not in st.session_state:
        st.session_state.is_member = False

    st.markdown("### メンバー認証")
    st.markdown(
        """
        Gear Swamp はクローズド運用です。  
        共有された「招待コード」を入力すると、パーツ一覧と登録画面が開きます。
        """
    )

    with st.form("member_form"):
        code_input = st.text_input("招待コード", type="password")
        submitted = st.form_submit_button("認証する")

    if submitted:
        if code_input == invite_code_setting:
            st.session_state.is_member = True
            st.success("認証OK！ Gear Swamp に入れます。")
        else:
            st.session_state.is_member = False
            st.error("認証失敗：招待コードが違います。")

    # 文字が見づらくならないよう、背景は白・文字は黒固定の簡易CSS
    st.markdown(
        """
        <style>
        .stApp {
            background-color: #ffffff;
            color: #111111;
        }
        </style>
        """,
        unsafe_allow_html=True
    )

    return st.session_state.is_member


# ==========================
# UI: Part register
# ==========================
def render_register_page(df: pd.DataFrame) -> pd.DataFrame:
    st.markdown("### パーツ登録（提供できるもの）")

    with st.form("register_form", clear_on_submit=True):
        cols_top = st.columns(3)
        with cols_top[0]:
            owner_name = st.text_input("あなたの名前（ニックネーム可）")
        with cols_top[1]:
            instagram_id = st.text_input("Instagram ID（@なし）")
        with cols_top[2]:
            location = st.text_input("エリア（例：名古屋、京都、オンラインのみ など）")

        cols_mid = st.columns(3)
        with cols_mid[0]:
            category = st.selectbox(
                "カテゴリ",
                ["", "フレーム", "ホイール", "タイヤ", "サドル", "ステム", "ハンドル", "ドライブトレイン", "その他"],
                index=0
            )
        with cols_mid[1]:
            brand = st.text_input("ブランド")
        with cols_mid[2]:
            item_name = st.text_input("パーツ名")

        cols_bottom = st.columns(3)
        with cols_bottom[0]:
            size = st.text_input("サイズ / スペック（例：700×40c, 42T など）")
        with cols_bottom[1]:
            condition = st.selectbox(
                "状態",
                ["", "新品", "ほぼ新品", "美品", "それなりに使用感あり", "ジャンク寄り"],
                index=0
            )
        with cols_bottom[2]:
            status = st.selectbox(
                "ステータス",
                ["貸出可", "交渉中", "貸出中", "終了"],
                index=0
            )

        note = st.text_area("ひとこと（どんな用途向きか、注意点など）", height=80)

        submitted = st.form_submit_button("登録する")

    if submitted:
        if not item_name:
            st.error("パーツ名は必須です。")
        else:
            new_row = {
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "owner_name": owner_name,
                "instagram_id": instagram_id,
                "category": category,
                "brand": brand,
                "item_name": item_name,
                "size": size,
                "condition": condition,
                "location": location,
                "status": status,
                "note": note
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            save_data(df)
            st.success("パーツを登録しました。")

    st.info("※ 登録内容は一覧タブに即時反映されます。")
    return df


# ==========================
# UI: Part list & filter
# ==========================
def render_list_page(df: pd.DataFrame) -> None:
    st.markdown("### パーツ一覧 & A案メッセージ生成")

    if df.empty:
        st.warning("まだ登録されたパーツがありません。")
        return

    # Filter area
    with st.expander("絞り込み", expanded=True):
        cols_filter = st.columns(4)
        with cols_filter[0]:
            keyword = st.text_input("キーワード（ブランド / パーツ名 / メモなど）")
        with cols_filter[1]:
            category_filter = st.selectbox(
                "カテゴリで絞り込み",
                ["すべて"] + sorted([c for c in df["category"].dropna().unique() if c]),
                index=0
            )
        with cols_filter[2]:
            status_filter = st.selectbox(
                "ステータスで絞り込み",
                ["すべて", "貸出可", "交渉中", "貸出中", "終了"],
                index=0
            )
        with cols_filter[3]:
            only_available = st.checkbox("貸出可のみ表示", value=False)

    df_filtered = df.copy()

    if keyword:
        keyword_lower = keyword.lower()
        mask = (
            df_filtered["brand"].fillna("").str.lower().str.contains(keyword_lower)
            | df_filtered["item_name"].fillna("").str.lower().str.contains(keyword_lower)
            | df_filtered["note"].fillna("").str.lower().str.contains(keyword_lower)
        )
        df_filtered = df_filtered[mask]

    if category_filter != "すべて":
        df_filtered = df_filtered[df_filtered["category"] == category_filter]

    if status_filter != "すべて":
        df_filtered = df_filtered[df_filtered["status"] == status_filter]

    if only_available:
        df_filtered = df_filtered[df_filtered["status"] == "貸出可"]

    if df_filtered.empty:
        st.warning("条件に一致するパーツがありません。")
        return

    # Show table + message generator
    for idx, row in df_filtered.reset_index(drop=True).iterrows():
        with st.container():
            st.markdown("---")
            cols = st.columns([3, 2])
            with cols[0]:
                st.markdown(f"**{row['item_name']}**")
                meta_parts = []
                if isinstance(row.get("brand"), str) and row["brand"]:
                    meta_parts.append(row["brand"])
                if isinstance(row.get("category"), str) and row["category"]:
                    meta_parts.append(row["category"])
                if isinstance(row.get("size"), str) and row["size"]:
                    meta_parts.append(row["size"])
                if meta_parts:
                    st.caption(" / ".join(meta_parts))

                if isinstance(row.get("condition"), str) and row["condition"]:
                    st.write(f"状態：{row['condition']}")
                if isinstance(row.get("location"), str) and row["location"]:
                    st.write(f"エリア：{row['location']}")
                if isinstance(row.get("note"), str) and row["note"]:
                    st.write(f"メモ：{row['note']}")

                st.caption(f"登録日時：{row['created_at']}")

            with cols[1]:
                st.write(f"ステータス：**{row['status']}**")
                owner_line = "提供者："
                if isinstance(row.get("owner_name"), str) and row["owner_name"]:
                    owner_line += row["owner_name"]
                else:
                    owner_line += "（名前未登録）"
                st.write(owner_line)

                if isinstance(row.get("instagram_id"), str) and row["instagram_id"]:
                    st.write(f"Instagram：@{row['instagram_id']}")

                btn_key = f"msg_button_{idx}"
                if st.button("A案：相談メッセージを作る（コピペ用）", key=btn_key):
                    msg = make_message(row)
                    st.code(msg, language="text")
                    st.info("このテキストをコピーして、Instagram DM などに貼り付けてください。")

    st.markdown("---")
    st.caption("※ 個人情報は書きすぎないよう注意しつつ、ゆるくシェアしていきましょう。")


# ==========================
# UI: Member info page
# ==========================
def render_member_info_page():
    st.markdown("### メンバー向け案内")
    st.write(
        """
        - Gear Swamp は「信頼できる身内だけ」で使う前提のクローズドなパーツシェアボードです  
        - たとえば Instagram のグループ DM などと組み合わせて使う想定です  
        - A案として「自動通知」は行わず、**『メッセージ文をコピペする』**運用に絞っています  

        #### 使い方ざっくり
        1. 「パーツ登録」タブから自分の余っているパーツを登録  
        2. 「パーツ一覧」タブで気になるパーツを探す  
        3. 行のボタンからメッセージ文を生成して DM などにコピペ  

        迷ったら、とりあえず「余ってるけど捨てづらいもの」を 1 個登録してみるくらいの温度感で。
        """
    )


# ==========================
# Main
# ==========================
def main():
    # Simple global CSS: white background & dark text
    st.markdown(
        """
        <style>
        .stApp {
            background-color: #ffffff;
            color: #111111;
        }
        </style>
        """,
        unsafe_allow_html=True
    )

    st.title("Gear Swamp 🦎")
    st.caption("身内向けバイクパーツ・シェアボード（A案：手動DM運用）")

    # Sidebar navigation
    st.sidebar.header("メニュー")
    page = st.sidebar.radio(
        "ページを選択",
        options=["メンバー認証", "パーツ一覧", "パーツ登録", "メンバー向け案内"],
        index=0
    )

    # Auth gate
    if page == "メンバー認証":
        check_member()
        return

    is_member = st.session_state.get("is_member", False)
    if not is_member:
        st.warning("まず左の『メンバー認証』から招待コードを入力してください。")
        return

    # Data load
    df = load_data()

    if page == "パーツ登録":
        df_new = render_register_page(df)
        # Update session copy if needed
        if not df_new.equals(df):
            df = df_new
    elif page == "パーツ一覧":
        render_list_page(df)
    elif page == "メンバー向け案内":
        render_member_info_page()


if __name__ == "__main__":
    main()


















