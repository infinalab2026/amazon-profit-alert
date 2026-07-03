import streamlit as st
import pandas as pd
import json
import re
import time
import requests
from datetime import datetime
from difflib import SequenceMatcher

st.set_page_config(
    page_title="Amazon Profit Decline Alert",
    page_icon="📊",
    layout="wide",
)

st.markdown("""
<style>
  .brand-badge { display:inline-block; background:#EBF3FB; color:#1F4E79;
    border-radius:4px; padding:2px 8px; font-size:12px; font-weight:700; }
  .stDataFrame { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)


# ── Auth ──────────────────────────────────────────────────────────────────────

def check_login():
    if st.session_state.get("authenticated"):
        return

    st.title("🔐 Amazon Profit Alert — Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", use_container_width=True)

    if submitted:
        creds = st.secrets.get("credentials", {})
        if username in creds and creds[username] == password:
            st.session_state.authenticated = True
            st.session_state.username = username
            st.rerun()
        else:
            st.error("Invalid username or password.")
    st.stop()


# ── Supabase persistence ───────────────────────────────────────────────────────

@st.cache_resource
def get_supabase():
    from supabase import create_client
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)


def save_cache(df: pd.DataFrame, last_month: str, months: list):
    try:
        sb = get_supabase()
        payload = {
            "results": df.to_dict("records"),
            "last_month": last_month,
            "months": months,
            "updated_at": datetime.now().isoformat(),
        }
        sb.table("amazon_profit_cache").upsert({"id": 1, "data": json.dumps(payload, ensure_ascii=False)}).execute()
    except Exception as e:
        st.warning(f"Could not save results to database: {e}")


def load_cache():
    try:
        sb = get_supabase()
        resp = sb.table("amazon_profit_cache").select("*").eq("id", 1).execute()
        if resp.data:
            return json.loads(resp.data[0]["data"])
    except Exception as e:
        st.warning(f"Could not load saved results from database: {e}")
    return None


# ── Analysis logic ─────────────────────────────────────────────────────────────

def clean_name(name, brand=''):
    if not isinstance(name, str): return ""
    name = re.sub(r'\([^)]*\)', '', name)
    name = re.sub(r'\[[^\]]*\]', '', name)
    for p in [r'\b(black|white|blue|red|green|pink|purple|gray|grey|silver|gold|rose)\b',
              r'\b(\d+\s*pack|\d+\s*pcs|\d+\s*piece|\d+\s*count)\b',
              r'\b(large|small|medium|xl|xxl|xs|mini|pro|plus|max|lite)\b',
              r'\b(new|upgraded|improved|v\d+|version\s*\d+)\b',
              r'[\|\-–—].*$']:
        name = re.sub(p, '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+', ' ', name).strip().lower()
    # 去掉品牌名，避免品牌前缀虚增相似度
    if brand:
        brand_clean = re.sub(r'^lr\d+-', '', brand.lower()).strip()
        name = re.sub(r'^' + re.escape(brand_clean) + r'\s*', '', name).strip()
    return name

def clean_brand(b):
    if not isinstance(b, str) or b.strip() in ('-', ''): return ''
    return b.split('，')[0].split(',')[0].strip().lower()

def similarity(a, b, brand=''):
    return SequenceMatcher(None, clean_name(a, brand), clean_name(b, brand)).ratio()

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_amazon_title(asin: str) -> str:
    """从 Amazon 产品页抓取标题，结果缓存 24 小时。"""
    try:
        url = f"https://www.amazon.com/dp/{asin}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            # 优先找 <span id="productTitle">
            m = re.search(r'id="productTitle"[^>]*>\s*([^<]{5,300}?)\s*<', resp.text)
            if m:
                return m.group(1).strip()
            # 备用：<title> 标签
            m = re.search(r'<title>([^<]+)</title>', resp.text)
            if m:
                title = m.group(1).strip()
                title = re.sub(r'\s*[:\-–]\s*(Amazon\.com|Amazon).*$', '', title)
                return title.strip()
    except Exception:
        pass
    return ''

def run_analysis(file_bytes, filename, threshold, decline_ratio):
    import io
    df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=0, header=None)
    df.columns = ['日期','ASIN','店铺','产品名称','SKU','品牌','销量','销售净毛利']
    df = df[df['日期'] != '日期'].dropna(subset=['日期'])
    df['销量'] = pd.to_numeric(df['销量'], errors='coerce').fillna(0)
    df['销售净毛利'] = pd.to_numeric(df['销售净毛利'], errors='coerce').fillna(0)

    def exm(d):
        m = re.match(r'(\d{4}-\d{2})', str(d))
        return m.group(1) if m else None
    df['月份'] = df['日期'].apply(exm)
    df = df.dropna(subset=['月份'])

    def best_val(s):
        """取第一个非空、非'-'的值，否则取 first。"""
        valid = s[s.notna() & (s.astype(str).str.strip() != '-') & (s.astype(str).str.strip() != '')]
        return valid.iloc[0] if len(valid) else s.iloc[0]

    asin_info = df.groupby('ASIN').agg(
        产品名称=('产品名称', best_val), 品牌=('品牌', best_val)
    ).reset_index()
    asin_info['bc'] = asin_info['品牌'].apply(clean_brand)

    parent = {i: i for i in range(len(asin_info))}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    rows = asin_info.to_dict('records')
    # 如果两个名称在这些关键词上不同，绝对不合并（白天/夜间/颜色变体等）
    EXCLUSIVE_KEYWORDS = [
        {'day', 'daytime'}, {'night', 'nighttime'},
        {'men', 'women'}, {'male', 'female'},
        {'kids', 'adult', 'adults'},
    ]
    def has_conflict(na, nb):
        wa = set(re.findall(r'\b\w+\b', na.lower()))
        wb = set(re.findall(r'\b\w+\b', nb.lower()))
        for group in EXCLUSIVE_KEYWORDS:
            if (wa & group) != (wb & group):
                return True
        return False

    for i in range(len(rows)):
        for j in range(i+1, len(rows)):
            bi, bj = rows[i]['bc'], rows[j]['bc']
            if not bi or not bj or bi != bj: continue
            ni, nj = rows[i]['产品名称'], rows[j]['产品名称']
            if not isinstance(ni, str) or ni.strip() in ('-', ''): continue
            if not isinstance(nj, str) or nj.strip() in ('-', ''): continue
            if has_conflict(ni, nj): continue
            if similarity(ni, nj, bi) >= 0.82:
                parent[find(i)] = find(j)

    # 取同组中出现次数最多的名称作为组名（而非最长名称）
    from collections import Counter
    group_names: dict = {}
    gmap = {}
    for i, row in enumerate(rows):
        root = find(i)
        name = row['产品名称']
        if isinstance(name, str) and name.strip() not in ('-', ''):
            group_names.setdefault(root, Counter())[name] += 1
        gmap[row['ASIN']] = root
    glabel = {root: cnt.most_common(1)[0][0] for root, cnt in group_names.items()}
    df['产品组ID'] = df['ASIN'].map(gmap)
    df['产品组名称'] = df['产品组ID'].map(glabel)

    months = sorted(df['月份'].unique())
    current_ym = datetime.now().strftime('%Y-%m')
    complete = [m for m in months if m != current_ym]
    if len(complete) < 2:
        return None, None, None, "Need at least 2 complete months of data."
    last_month = complete[-1]

    grp = df.groupby(['产品组ID','产品组名称','月份']).agg(
        profit=('销售净毛利','sum'), vol=('销量','sum'),
        asins=('ASIN', lambda x: ', '.join(sorted(set(x)))),
        brand=('品牌','first')
    ).reset_index()

    results = []
    for pid, sub in grp.groupby('产品组ID'):
        sub = sub.sort_values('月份')
        hist = sub[sub['月份'].isin(complete)]
        if hist.empty: continue
        last_row = hist[hist['月份'] == last_month]
        last_p = last_row['profit'].values[0] if len(last_row) else 0
        best_p = hist['profit'].max()
        best_m = hist.loc[hist['profit'].idxmax(), '月份']
        if best_p < threshold: continue
        if last_p >= best_p * decline_ratio: continue
        if best_m == last_month: continue
        dpct = (best_p - last_p) / best_p * 100 if best_p else 0
        mp = {r['月份']: round(r['profit'], 2) for _, r in sub.iterrows()}
        results.append({
            'Product Name': sub['产品组名称'].iloc[0],
            'Decline ($)': round(best_p - last_p, 2),
            'Decline (%)': round(dpct, 1),
            'Brand': sub['brand'].iloc[0],
            'ASINs': sub[sub['月份']==last_month]['asins'].iloc[0] if len(last_row) else sub['asins'].iloc[0],
            'Peak Profit ($)': round(best_p, 2),
            'Peak Month': best_m,
            **{m: round(mp.get(m, 0), 2) for m in complete},
            'Last Month Profit ($)': round(last_p, 2),
        })
    results.sort(key=lambda x: x['Decline ($)'], reverse=True)
    return pd.DataFrame(results), last_month, complete, None


# ── Render results ─────────────────────────────────────────────────────────────

def render_results(df: pd.DataFrame, last_month: str, months: list, updated_at: str = None):
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Products Need Attention", len(df))
    col2.metric("Total Profit Declined", f"${df['Decline ($)'].sum():,.0f}")
    col3.metric("Avg Decline Rate", f"{df['Decline (%)'].mean():.0f}%")
    col4.metric("Reference Month", last_month)

    if updated_at:
        st.caption(f"Last updated: {updated_at[:16].replace('T', ' ')}")

    st.divider()

    fc1, fc2, fc3 = st.columns([3, 2, 2])
    search   = fc1.text_input("🔍 Search product / brand / ASIN", placeholder="Type to filter…", key="search")
    brands   = ["All brands"] + sorted(df['Brand'].dropna().unique().tolist())
    brand_sel = fc2.selectbox("Brand", brands, key="brand")
    top_n    = fc3.selectbox("Show", ["All", "Top 20", "Top 50"], key="topn")

    df_show = df.copy()
    if search:
        q = search.lower()
        df_show = df_show[
            df_show['Product Name'].str.lower().str.contains(q, na=False) |
            df_show['Brand'].str.lower().str.contains(q, na=False) |
            df_show['ASINs'].str.lower().str.contains(q, na=False)
        ]
    if brand_sel != "All brands":
        df_show = df_show[df_show['Brand'] == brand_sel]
    if top_n == "Top 20": df_show = df_show.head(20)
    elif top_n == "Top 50": df_show = df_show.head(50)

    st.caption(f"Showing **{len(df_show)}** products — click column headers to sort")

    month_cols = months
    df_show = df_show.copy()
    df_show.insert(1, 'Trend', df_show[month_cols].values.tolist())

    st.dataframe(
        df_show,
        use_container_width=True,
        height=600,
        column_config={
            'Product Name': st.column_config.TextColumn('Product Name', width='large'),
            'Trend':        st.column_config.BarChartColumn('Trend', width='small', y_min=None, y_max=None),
            'Brand':        st.column_config.TextColumn('Brand', width='medium'),
            'ASINs':        st.column_config.TextColumn('ASINs', width='medium'),
            'Peak Profit ($)': st.column_config.NumberColumn('Peak Profit ($)', format='$%.0f'),
            'Peak Month':   st.column_config.TextColumn('Peak Month', width='small'),
            **{m: st.column_config.NumberColumn(m, format='$%.0f') for m in month_cols},
            'Last Month Profit ($)': st.column_config.NumberColumn(f'Last Month ({last_month}) $', format='$%.0f'),
            'Decline ($)':  st.column_config.NumberColumn('Decline ($)', format='$%.0f'),
            'Decline (%)':  st.column_config.ProgressColumn('Decline (%)', format='%.1f%%', min_value=0, max_value=100),
        },
        hide_index=True,
    )

    csv = df_show.to_csv(index=False).encode('utf-8-sig')
    st.download_button("⬇️ Download CSV", csv,
                       file_name=f"profit_decline_{last_month}.csv", mime='text/csv')


# ── Main ───────────────────────────────────────────────────────────────────────

check_login()

st.title("📊 Amazon Product Profit Decline Alert")
st.caption(f"Logged in as **{st.session_state.get('username','')}** · "
           f"[Logout](#logout \"Click logout below\")")

if st.button("Logout", key="logout_btn"):
    st.session_state.authenticated = False
    st.rerun()

st.divider()

with st.sidebar:
    st.header("⚙️ Upload & Settings")
    uploaded = st.file_uploader("New sales report (.xls / .xlsx)", type=['xls','xlsx'])
    threshold   = st.number_input("Min. peak profit ($)", value=200, min_value=0, step=50)
    decline_pct = st.number_input("Decline threshold (%)", value=50, min_value=1, max_value=99, step=5)
    analyze_btn = st.button("🔍 Analyze", use_container_width=True, type="primary")
    st.divider()
    st.caption("Results are saved automatically and will appear here on your next visit from any device.")
    if st.button("🗑️ Clear saved results", use_container_width=True):
        try:
            get_supabase().table("amazon_profit_cache").delete().eq("id", 1).execute()
            st.session_state.pop("cached", None)
            st.success("Cache cleared.")
            st.rerun()
        except Exception as e:
            st.error(f"Failed: {e}")

# Run new analysis
if analyze_btn and uploaded:
    with st.spinner("Analyzing…"):
        result_df, last_month, months, err = run_analysis(
            uploaded.read(), uploaded.name, threshold, decline_pct / 100
        )
    if err:
        st.error(err)
    elif result_df is None or result_df.empty:
        st.info("No products matched the criteria. Try lowering the thresholds.")
    else:
        save_cache(result_df, last_month, months)
        st.session_state["cached"] = {
            "results": result_df.to_dict("records"),
            "last_month": last_month,
            "months": months,
            "updated_at": datetime.now().isoformat(),
        }
        st.success(f"Analysis complete — {len(result_df)} products flagged. Results saved.")
        render_results(result_df, last_month, months)

# Show cached results
else:
    cached = st.session_state.get("cached") or load_cache()
    if cached:
        df_cached = pd.DataFrame(cached["results"])
        render_results(df_cached, cached["last_month"], cached["months"], cached.get("updated_at"))
    else:
        st.info("👈 Upload a report in the sidebar to get started. Previous results will appear here automatically on future visits.")
