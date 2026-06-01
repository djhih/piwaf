#!/usr/bin/env python3
"""PiWAF 儀表板：讀 SQLite，呈現 WAF 防禦決策的可視化。"""
import json
import os
from collections import Counter

import pandas as pd
import sqlite3
import streamlit as st
from streamlit_autorefresh import st_autorefresh

DB_PATH = os.environ.get("DB_PATH", "/data/piwaf.db")
THRESHOLD = int(os.environ.get("ANOMALY_THRESHOLD", "5"))

st.set_page_config(page_title="PiWAF Observatory", layout="wide")
st_autorefresh(interval=5000, key="refresh")   # 每 5 秒自動更新


@st.cache_data(ttl=4)
def load() -> pd.DataFrame:
    # 唯讀開啟，配合 parser 的 WAL，可邊寫邊讀
    uri = f"file:{DB_PATH}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        return pd.DataFrame()
    try:
        df = pd.read_sql_query("SELECT * FROM events", con)
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()
    return df


st.title("🛡️ PiWAF Observatory")
st.caption("BunkerWeb（ModSecurity + OWASP CRS）防禦決策可視化")

df = load()
if df.empty:
    st.info("目前沒有資料。請啟動服務並送出流量：`docker compose run --rm generator`")
    st.stop()

# ── 側邊欄篩選 ──
with st.sidebar:
    st.header("篩選")
    modes = sorted(df["mode"].dropna().unique())
    scenarios = sorted(df["scenario"].dropna().unique())
    sel_mode = st.multiselect("WAF 模式", modes, default=modes)
    sel_scen = st.multiselect("情境 scenario", scenarios, default=scenarios)

view = df[df["mode"].isin(sel_mode) & df["scenario"].isin(sel_scen)]
if view.empty:
    st.warning("此篩選條件下沒有資料。")
    st.stop()

# ── KPI ──
total = len(view)
blocked = int(view["blocked"].sum())
would = int(view["would_block"].sum())
attacks = int((view["category"] != "None").sum())
c1, c2, c3, c4 = st.columns(4)
c1.metric("總 request", total)
c2.metric("實際阻擋 (403)", blocked, f"{blocked/total*100:.0f}%")
c3.metric(f"分數≥{THRESHOLD}（本來會擋）", would, f"{would/total*100:.0f}%")
c4.metric("可疑/攻擊", attacks, f"{attacks/total*100:.0f}%")

st.divider()
col_l, col_r = st.columns(2)

# ── 攻擊類型分布 ──
with col_l:
    st.subheader("攻擊類型分布")
    cat = view[view["category"] != "None"]["category"].value_counts()
    if not cat.empty:
        st.bar_chart(cat)
    else:
        st.write("（無攻擊事件）")

# ── 阻擋 vs 放行 ──
with col_r:
    st.subheader("阻擋 / 放行")
    outcome = view["blocked"].map({1: "Blocked (403)", 0: "Passed"}).value_counts()
    st.bar_chart(outcome)

col_l2, col_r2 = st.columns(2)

# ── Top 觸發規則 ──
with col_l2:
    st.subheader("Top 觸發規則")
    rc = Counter()
    for raw in view["rule_ids"].dropna():
        rc.update(json.loads(raw))
    if rc:
        top = pd.Series(dict(rc.most_common(10))).sort_values(ascending=False)
        st.bar_chart(top)
    else:
        st.write("（無規則觸發）")

# ── Top 目標端點 ──
with col_r2:
    st.subheader("Top 目標端點")
    ep = view["path"].value_counts().head(10)
    st.bar_chart(ep)

# ── Anomaly score 分布 ──
st.subheader("Anomaly Score 分布")
score_hist = view["anomaly_score"].value_counts().sort_index()
st.bar_chart(score_hist)

# ── 時間線 ──
st.subheader("事件時間線")
tl = view.copy()
tl["ts"] = pd.to_datetime(tl["ts"], errors="coerce")
tl = tl.dropna(subset=["ts"]).set_index("ts")
if not tl.empty:
    per = tl.resample("10s").agg(total=("id", "count"), blocked=("blocked", "sum"))
    st.line_chart(per)

# ── 模式比較（Detection vs Blocking）──
if len(sel_mode) > 1 or df["mode"].nunique() > 1:
    st.subheader("模式比較：Detection vs Blocking")
    cmp = df.groupby("mode").agg(
        requests=("id", "count"),
        actually_blocked=("blocked", "sum"),
        would_block=("would_block", "sum"),
        avg_score=("anomaly_score", "mean"),
    ).round(2)
    st.dataframe(cmp, use_container_width=True)

# ── 明細 ──
with st.expander("原始事件明細"):
    st.dataframe(
        view[["ts", "client_ip", "method", "path", "status",
              "anomaly_score", "category", "n_rules", "scenario", "mode"]]
        .sort_values("ts", ascending=False),
        use_container_width=True,
    )
