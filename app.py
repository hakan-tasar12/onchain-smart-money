"""Smart Money Dashboard — open-source on-chain smart money tracker built with Streamlit."""
import json
import os
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.wallet import analyze_wallet, load_watchlist

load_dotenv()

st.set_page_config(
    page_title="Smart Money",
    page_icon="🔍",
    layout="wide",
)

# ── Nansen/Arkham-dark visual layer (style only; no logic changes) ─────────────
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap');

    :root {
        --bg: #0B0E14; --card: #151A23; --card2:#1A212C; --border: #232A36;
        --text: #E6EDF3; --muted: #8B97A7;
        --cyan: #19C3FF; --green: #00E29A; --red: #FF4D6D; --amber: #FFB020;
    }

    html, body, [class*="css"], .stApp { font-family: 'Inter', sans-serif; }
    .stApp { background: radial-gradient(1200px 600px at 15% -10%, #11161F 0%, #0B0E14 55%); }
    .block-container { padding-top: 1.4rem; max-width: 1280px; }
    [data-testid="stMetricValue"], code, .mono { font-family: 'JetBrains Mono', monospace !important; }

    /* ── Header bar ────────────────────────────────────────────── */
    .app-header { display:flex; align-items:center; justify-content:space-between;
        padding: 14px 20px; margin-bottom: 18px;
        background: linear-gradient(90deg, #141A24 0%, #10151D 100%);
        border: 1px solid var(--border); border-radius: 16px; }
    .brand { display:flex; align-items:center; gap:14px; }
    .brand .logo { filter: drop-shadow(0 0 6px rgba(25,195,255,0.4)); line-height:1; }
    .brand .name { font-size: 20px; font-weight: 800; letter-spacing: 0.5px; color: var(--text); }
    .brand .tag { font-size: 12px; color: var(--muted); margin-top: 1px; }
    .updated { font-size: 12px; color: var(--muted); font-family:'JetBrains Mono',monospace;
        border:1px solid var(--border); border-radius:20px; padding:5px 12px; background:var(--card); }

    /* ── KPI strip ─────────────────────────────────────────────── */
    .kpi-strip { display:grid; grid-template-columns: repeat(5, 1fr); gap:10px; margin-bottom: 20px; }
    .kpi { background: var(--card); border:1px solid var(--border); border-radius:14px; padding:14px 16px; }
    .kpi .k-label { font-size: 0.68rem; text-transform:uppercase; letter-spacing:0.7px;
        color: var(--muted); font-weight:600; margin-bottom:6px; }
    .kpi .k-value { font-size: 1.4rem; font-weight:700; font-family:'JetBrains Mono',monospace; color: var(--text); }
    .kpi .k-value.cyan { color: var(--cyan); }
    .kpi .k-value.green { color: var(--green); }
    .kpi .k-value.amber { color: var(--amber); }
    @media (max-width: 900px) { .kpi-strip { grid-template-columns: repeat(2, 1fr); } }

    /* ── Cards (st.container(border=True)) ─────────────────────── */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: var(--card); border:1px solid var(--border) !important;
        border-radius:16px; padding: 6px 18px 14px; margin-bottom: 6px; }
    .sec-head { font-size: 0.95rem; font-weight:700; color: var(--text);
        letter-spacing:0.2px; margin: 8px 0 2px; }
    .sec-sub { font-size: 0.78rem; color: var(--muted); margin-bottom: 8px; }

    /* Metric cards (inside tabs) */
    [data-testid="stMetric"] { background: var(--card2); border:1px solid var(--border);
        border-radius:14px; padding:14px 16px; }
    [data-testid="stMetricLabel"] p { color: var(--muted); font-size: 0.7rem;
        text-transform: uppercase; letter-spacing: 0.6px; font-weight: 600; }
    [data-testid="stMetricValue"] { color: var(--cyan); font-weight: 600; font-size:1.5rem; }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid var(--border); }
    .stTabs [data-baseweb="tab"] { background: transparent; border-radius: 10px 10px 0 0;
        color: var(--muted); font-weight: 600; padding: 9px 16px; }
    .stTabs [aria-selected="true"] { background: var(--card); color: var(--text);
        border: 1px solid var(--border); border-bottom: 2px solid var(--cyan); }

    .stDataFrame { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
    .stCaption, [data-testid="stCaptionContainer"] p { color: var(--muted) !important; }
    [data-testid="stNotification"] { border-radius: 10px; }
    #MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; height:0; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── DB context (single load for KPI strip + tabs) ─────────────────────────────
try:
    from src.db import (
        get_all_wallets,
        get_latest_scores,
        get_pnl_coverage,
        get_recent_alerts,
        get_recent_token_accumulations,
    )
    db_wallets = {w["address"]: w["label"] for w in get_all_wallets()}
    scores = get_latest_scores()
except Exception:
    db_wallets, scores = {}, []

# Real pipeline timestamp (scores.computed_at is date-only: "2026-06-12")
if scores:
    _last_date = max(s.get("computed_at", "") for s in scores)
    try:
        from datetime import date as _date_cls
        last_pipeline_str: str | None = _date_cls.fromisoformat(_last_date).strftime("%d %b")
    except Exception:
        last_pipeline_str = _last_date
else:
    last_pipeline_str = None

# ── Watchlist ──────────────────────────────────────────────────────────────────
try:
    wallets = load_watchlist("watchlist.txt")
except FileNotFoundError:
    st.error("watchlist.txt not found.")
    st.stop()
if not wallets:
    st.warning("watchlist.txt is empty.")
    st.stop()


# ── Table styler helpers ───────────────────────────────────────────────────────
def _color_signal(val):
    s = str(val).lower()
    if "🟢" in str(val) or "accum" in s or s.strip() == "buy":
        return "color:#00E29A; font-weight:600"
    if "🔴" in str(val) or "distrib" in s or s.strip() == "sell":
        return "color:#FF4D6D; font-weight:600"
    return ""


def _color_dir(val):
    s = str(val).upper()
    if s in ("IN", "↓"):
        return "color:#00E29A; font-weight:600"
    if s in ("OUT", "↑"):
        return "color:#FF4D6D; font-weight:600"
    return ""


def _color_composite(val):
    try:
        v = max(0.0, min(100.0, float(val)))
    except (TypeError, ValueError):
        return ""
    alpha = 0.10 + 0.45 * (v / 100.0)
    return f"background-color: rgba(25,195,255,{alpha:.2f}); font-weight:700; color:#E6EDF3"


@st.cache_data(ttl=300, show_spinner=False)
def _cached_analyze(address: str) -> dict:
    return analyze_wallet(address)


# ── Header bar ────────────────────────────────────────────────────────────────
_ts_label = f"Pipeline: {last_pipeline_str}" if last_pipeline_str else "Pipeline: Never run"
st.markdown(
    f"""
    <div class="app-header">
      <div class="brand">
        <svg class="logo" width="28" height="28" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
          <line x1="8" y1="3" x2="8" y2="25" stroke="#19C3FF" stroke-width="1.5" stroke-linecap="round"/>
          <rect x="5" y="9" width="6" height="10" rx="1" fill="#19C3FF"/>
          <line x1="20" y1="2" x2="20" y2="24" stroke="#00E29A" stroke-width="1.5" stroke-linecap="round"/>
          <rect x="17" y="5" width="6" height="12" rx="1" fill="#00E29A"/>
        </svg>
        <div>
          <div class="name">SMART MONEY</div>
          <div class="tag">open-source on-chain smart money analytics — Etherscan, CoinGecko</div>
        </div>
      </div>
      <div class="updated">⟳ {_ts_label}</div>
    </div>
    """,
    unsafe_allow_html=True,
)

if not os.getenv("ETHERSCAN_API_KEY"):
    st.warning(
        "ETHERSCAN_API_KEY missing. Create a .env file and add your key. Get one at etherscan.io/apis."
    )

# ── Global KPI strip (from DB, fast) ──────────────────────────────────────────
n_tracked = len(wallets)
n_scored = len(scores)
total_pnl = sum((s.get("realized_pnl") or 0) for s in scores)
covs = []
for s in scores:
    try:
        c = get_pnl_coverage(s["wallet_address"]).get("coverage")
        if c is not None:
            covs.append(c)
    except Exception:
        pass
avg_cov = f"{sum(covs) / len(covs) * 100:.0f}%" if covs else "—"
try:
    n_signals = len(get_recent_token_accumulations(hours=24))
except Exception:
    n_signals = 0

pnl_cls = "green" if total_pnl > 0 else ("" if total_pnl == 0 else "amber")
pnl_str = f"${total_pnl:,.0f}" if scores else "—"

st.markdown(
    f"""
    <div class="kpi-strip">
      <div class="kpi"><div class="k-label">Tracked Wallets</div><div class="k-value cyan">{n_tracked}</div></div>
      <div class="kpi"><div class="k-label">Scored</div><div class="k-value">{n_scored}</div></div>
      <div class="kpi"><div class="k-label">Total Realized PnL</div><div class="k-value {pnl_cls}">{pnl_str}</div></div>
      <div class="kpi"><div class="k-label">Active Signals 24h</div><div class="k-value cyan">{n_signals}</div></div>
      <div class="kpi"><div class="k-label">Avg. PnL Coverage</div><div class="k-value amber">{avg_cov}</div></div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Tabs ───────────────────────────────────────────────────────────────────────
tab_wallet, tab_aggregate, tab_scores, tab_alerts = st.tabs([
    "👤 Wallet", "🌐 Overview", "🏆 Scores", "🔔 Alerts"
])

# ════════════════════════════════════════════════════════════════════════════════
# TAB 1 — Single wallet analysis
# ════════════════════════════════════════════════════════════════════════════════
with tab_wallet:
    col_sel, _ = st.columns([1, 3])
    with col_sel:
        labels = [w["label"] for w in wallets]
        selected_label = st.selectbox("Select wallet", labels, label_visibility="collapsed")
    selected_wallet = next(w for w in wallets if w["label"] == selected_label)

    with st.spinner(f"Loading {selected_label}..."):
        data = _cached_analyze(selected_wallet["address"])

    txn_df = data["transactions"]
    token_df = data["token_transfers"]
    holdings = data["holdings"]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("ETH Balance", f"{data['eth_balance']:,.4f}")
    m2.metric("ETH ≈ USD", f"${data['eth_value_usd']:,.0f}")
    m3.metric("ETH Txns", len(txn_df))
    m4.metric("Token Transfers", len(token_df))

    # Holdings
    with st.container(border=True):
        st.markdown('<div class="sec-head">Token Net Positions</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sec-sub">Net flow is inbound minus outbound across the last 50 transfers. '
            'USD value uses the live price. For actual realized PnL, check the Scores tab.</div>',
            unsafe_allow_html=True,
        )
        if holdings.empty:
            st.info("No token transfers found.")
        else:
            real = holdings[~holdings["is_spam"]]
            spam_n = int(holdings["is_spam"].sum())
            if real.empty:
                st.info("No priced token positions — only dust / airdrops.")
            else:
                display = real[["token", "net_flow", "price_usd", "value_usd", "signal"]].rename(columns={
                    "token": "Token", "net_flow": "Net Flow", "price_usd": "Price",
                    "value_usd": "Value", "signal": "Signal",
                })
                styled = display.style.format({
                    "Net Flow": "{:,.2f}", "Price": "${:,.4f}", "Value": "${:,.0f}",
                }).map(_color_signal, subset=["Signal"])
                st.dataframe(styled, width="stretch", hide_index=True)
            if spam_n:
                st.caption(f"🧹 {spam_n} spam / dust / unpriced token(s) hidden (phishing patterns + unlisted).")

    # ETH transfers
    with st.container(border=True):
        st.markdown('<div class="sec-head">Recent ETH Transfers</div>', unsafe_allow_html=True)
        if txn_df.empty:
            st.info("No ETH transfers found.")
        else:
            eth_view = txn_df[["timestamp", "direction", "value_eth", "from", "to"]].rename(columns={
                "timestamp": "Time", "direction": "Dir", "value_eth": "ETH",
                "from": "From", "to": "To",
            })
            st.dataframe(
                eth_view.style.format({"ETH": "{:,.4f}"}).map(_color_dir, subset=["Dir"]),
                width="stretch", hide_index=True,
            )

    # Token transfers
    with st.container(border=True):
        st.markdown('<div class="sec-head">Recent Token Transfers</div>', unsafe_allow_html=True)
        if token_df.empty:
            st.info("No token transfers found.")
        else:
            tok_view = token_df[["timestamp", "token", "direction", "value", "from", "to"]].rename(columns={
                "timestamp": "Time", "token": "Token", "direction": "Dir",
                "value": "Amount", "from": "From", "to": "To",
            })
            st.dataframe(
                tok_view.style.map(_color_dir, subset=["Dir"]),
                width="stretch", hide_index=True,
            )
    st.caption(f"Address: `{selected_wallet['address']}`")

# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — Consensus overview (DB-backed, no live HTTP)
# ════════════════════════════════════════════════════════════════════════════════
with tab_aggregate:
    with st.container(border=True):
        st.markdown('<div class="sec-head">Smart Money Consensus Signals</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sec-sub">Tokens that at least 2 tracked wallets have been buying over the last 7 days. '
            'Pulled from the local DB — no live requests. Updates daily at 02:00 UTC.</div>',
            unsafe_allow_html=True,
        )
        _tab2_raw = get_recent_token_accumulations(hours=168)
        if not _tab2_raw:
            st.info("No consensus accumulation signal in the last 7 days. Run `run_daily.py` to populate.")
        else:
            _tab2_rows = []
            for _r in _tab2_raw:
                _addrs = (_r["wallets"] or "").split(",")
                _labels = ", ".join(
                    db_wallets.get(a.strip(), a.strip()[:10] + "...") for a in _addrs if a.strip()
                )
                _tab2_rows.append({
                    "Token": f"${_r['symbol']}" if _r["symbol"] else _r["contract"][:10] + "...",
                    "Consensus": int(_r["wallet_count"]),
                    "Accumulating Wallets": _labels,
                    "Contract": _r["contract"][:12] + "...",
                })
            _top = _tab2_raw[0]
            _top_tok = f"${_top['symbol']}" if _top["symbol"] else _top["contract"][:10] + "..."
            st.success(
                f"Top signal right now: {_top_tok} ({int(_top['wallet_count'])} wallets, last 7 days)"
            )
            st.dataframe(pd.DataFrame(_tab2_rows), width="stretch", hide_index=True)
        _src_lbl = f"Last updated: {last_pipeline_str}" if last_pipeline_str else "Pipeline never run"
        st.caption(f"Source: SQLite pipeline — {_src_lbl}")

# ════════════════════════════════════════════════════════════════════════════════
# TAB 3 — Score leaderboard
# ════════════════════════════════════════════════════════════════════════════════
with tab_scores:
    with st.container(border=True):
        st.markdown('<div class="sec-head">Wallet Score Leaderboard</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sec-sub">Composite score updated daily. '
            'Weights: WinRate 35%, Realized PnL 30%, EarlyEntry 20%, Diversity 15%. '
            'All scores are percentile-normalized. PnL uses CoinGecko daily-close prices, so it\'s approximate.</div>',
            unsafe_allow_html=True,
        )
        if not scores:
            st.info("No scores yet. Run `run_daily.py` to populate.")
        else:
            rows = []
            for s in scores:
                label = db_wallets.get(s["wallet_address"], s["wallet_address"][:10] + "...")
                cov = get_pnl_coverage(s["wallet_address"])
                cov_str = f"{cov['coverage'] * 100:.0f}%" if cov["coverage"] is not None else "—"
                rows.append({
                    "Wallet": label,
                    "WinRate": f"{s['win_rate'] * 100:.0f}%" if s["win_rate"] is not None else "—",
                    "Realized PnL": f"${s['realized_pnl']:,.0f}" if s["realized_pnl"] is not None else "—",
                    "EarlyEntry": f"{s['early_entry'] * 100:.0f}%" if s["early_entry"] is not None else "—",
                    "Diversity": f"{s['diversity'] * 100:.0f}%" if s["diversity"] is not None else "—",
                    "Composite": round(s["composite"], 1) if s["composite"] is not None else None,
                    "Trades": s["trade_count"],
                    "Coverage": cov_str,
                })
            score_df = pd.DataFrame(rows)
            st.dataframe(
                score_df.style.map(_color_composite, subset=["Composite"]),
                width="stretch", hide_index=True,
            )
            st.caption(
                "Coverage is the share of sells that have a known cost basis. "
                "A low number means the wallet was active before the 12-month data window — "
                "its realized PnL is understated because those older sells have no entry price. "
                "They're excluded, not set to zero."
            )
            if scores[0]["composite"] is not None:
                top = scores[0]
                top_label = db_wallets.get(top["wallet_address"], top["wallet_address"][:10] + "...")
                st.success(f"Top score: {top_label} — {top['composite']:.1f}/100")

# ════════════════════════════════════════════════════════════════════════════════
# TAB 4 — Alert history
# ════════════════════════════════════════════════════════════════════════════════
with tab_alerts:
    with st.container(border=True):
        st.markdown('<div class="sec-head">Consensus Alert History</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sec-sub">Telegram alerts fired when 2 or more tracked wallets accumulate the same token.</div>',
            unsafe_allow_html=True,
        )
        try:
            alerts = get_recent_alerts(limit=50)
        except Exception:
            alerts = []

        if not alerts:
            st.info("No alerts yet. Run `run_hourly.py` — an alert fires when 2 or more wallets accumulate the same token.")
        else:
            alert_rows = []
            for a in alerts:
                try:
                    wallet_list = json.loads(a["wallets_json"])
                    wallets_str = ", ".join(db_wallets.get(w, w[:10] + "...") for w in wallet_list)
                except Exception:
                    wallets_str = a["wallets_json"]
                alert_rows.append({
                    "Time": a["sent_at"][:19].replace("T", " "),
                    "Token": f"${a['symbol']}" if a["symbol"] else "?",
                    "Contract": a["contract"][:10] + "...",
                    "Wallets": wallets_str,
                    "Telegram": "✅" if a["telegram_ok"] else "❌",
                })
            st.dataframe(pd.DataFrame(alert_rows), width="stretch", hide_index=True)
            st.caption(f"Showing last {len(alerts)} alerts.")
