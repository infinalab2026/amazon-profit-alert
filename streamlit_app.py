import streamlit as st
import pandas as pd
import re
from datetime import datetime
from difflib import SequenceMatcher

st.set_page_config(
    page_title="Amazon Profit Decline Alert",
    page_icon="📊",
    layout="wide",
)

# ── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .brand-badge { display:inline-block; background:#EBF3FB; color:#1F4E79;
    border-radius:4px; padding:2px 8px; font-size:12px; font-weight:700; }
  .asin-tag { color:#999; font-size:11px; font-family:monospace; }
  .metric-box { background:white; border-radius:10px; padding:18px 22px;
    box-shadow:0 2px 8px rgba(0,0,0,.07); text-align:center; }
  .metric-val { font-size:30px; font-weight:800; color:#C00000; }
  .metric-lbl { font-size:12px; color:#888; margin-top:2px; }
  thead tr th { background:#1F4E79 !important; color:white !important; }
</style>
""", unsafe_allow_html=True)

# ── Analysis logic ────────────────────────────────────────────────────────────

def clean_name(name):
    if not isinstance(name, str): return ""
    name = re.sub(r'\([^)]*\)', '', name)
    name = re.sub(r'\[[^\]]*\]', '', name)
    for p in [r'\b(black|white|blue|red|green|pink|purple|gray|grey|silver|gold|rose)\b',
              r'\b(\d+\s*pack|\d+\s*pcs|\d+\s*piece|\d+\s*count)\b',
              r'\b(large|small|medium|xl|xxl|xs|mini|pro|plus|max|lite)\b',
              r'\b(new|upgraded|improved|v\d+|version\s*\d+)\b',
              r'[\|\-–—].*$']:
        name = re.sub(p, '', name, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', name).strip().lower()

def clean_brand(b):
    if not isinstance(b, str) or b.strip() in ('-', ''): return ''
    return b.split('，')[0].split(',')[0].strip().lower()

def similarity(a, b):
    return SequenceMatcher(None, clean_name(a), clean_name(b)).ratio()

@st.cache_data(show_spinner=False)
def load_and_analyze(file_bytes, filename, threshold, decline_ratio):
    df = pd.read_excel(file_bytes, sheet_name=0, header=None)
    df.columns = ['日期','ASIN','店铺','产品名称','SKU','品牌','销量','销售净毛利']
    df = df[df['日期'] != '日期'].dropna(subset=['日期'])
    df['销量'] = pd.to_numeric(df['销量'], errors='coerce').fillna(0)
    df['销售净毛利'] = pd.to_numeric(df['销售净毛利'], errors='coerce').fillna(0)
    def exm(d):
        m = re.match(r'(\d{4}-\d{2})', str(d))
        return m.group(1) if m else None
    df['月份'] = df['日期'].apply(exm)
    df = df.dropna(subset=['月份'])

    # group by brand + name similarity
    asin_info = df.groupby('ASIN').agg(产品名称=('产品名称','first'), 品牌=('品牌','first')).reset_index()
    asin_info['bc'] = asin_info['品牌'].apply(clean_brand)
    parent = {i: i for i in range(len(asin_info))}
    def find(x):
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    rows = asin_info.to_dict('records')
    for i in range(len(rows)):
        for j in range(i+1, len(rows)):
            bi, bj = rows[i]['bc'], rows[j]['bc']
            if not bi or not bj or bi != bj: continue
            if similarity(rows[i]['产品名称'], rows[j]['产品名称']) >= 0.6:
                parent[find(i)] = find(j)
    glabel = {}
    gmap = {}
    for i, row in enumerate(rows):
        root = find(i)
        if root not in glabel or len(row['产品名称']) > len(glabel[root]):
            glabel[root] = row['产品名称']
        gmap[row['ASIN']] = root
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
            'Brand': sub['brand'].iloc[0],
            'ASINs': sub[sub['月份']==last_month]['asins'].iloc[0] if len(last_row) else sub['asins'].iloc[0],
            'Peak Profit ($)': round(best_p, 2),
            'Peak Month': best_m,
            **{m: round(mp.get(m, 0), 2) for m in complete},
            'Last Month Profit ($)': round(last_p, 2),
            'Decline ($)': round(best_p - last_p, 2),
            'Decline (%)': round(dpct, 1),
        })
    results.sort(key=lambda x: x['Decline ($)'], reverse=True)
    return pd.DataFrame(results), last_month, complete, None


# ── UI ────────────────────────────────────────────────────────────────────────

st.title("📊 Amazon Product Profit Decline Alert")
st.caption("Upload your monthly sales profit report to identify products that performed well historically but declined last month.")

with st.sidebar:
    st.header("⚙️ Settings")
    uploaded = st.file_uploader("Upload sales report (.xls / .xlsx)", type=['xls','xlsx'])
    threshold = st.number_input("Min. peak profit ($)", value=200, min_value=0, step=50)
    decline_pct = st.number_input("Decline threshold (%)", value=50, min_value=1, max_value=99, step=5)
    analyze_btn = st.button("🔍 Analyze", use_container_width=True, type="primary")
    st.divider()
    st.caption("Products where peak profit ≥ threshold AND last month profit dropped by more than the decline threshold are flagged.")

if analyze_btn and uploaded:
    with st.spinner("Analyzing…"):
        result_df, last_month, months, err = load_and_analyze(
            uploaded.read(), uploaded.name, threshold, decline_pct / 100
        )
    if err:
        st.error(err)
    elif result_df is None or result_df.empty:
        st.info("No products matched the criteria. Try lowering the thresholds.")
    else:
        # ── Summary metrics
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Products Need Attention", len(result_df))
        col2.metric("Total Profit Declined", f"${result_df['Decline ($)'].sum():,.0f}")
        col3.metric("Avg Decline Rate", f"{result_df['Decline (%)'].mean():.0f}%")
        col4.metric("Reference Month", last_month)

        st.divider()

        # ── Filters
        fc1, fc2, fc3 = st.columns([3, 2, 2])
        search = fc1.text_input("🔍 Search product / brand / ASIN", placeholder="Type to filter…")
        brands = ["All brands"] + sorted(result_df['Brand'].dropna().unique().tolist())
        brand_sel = fc2.selectbox("Brand", brands)
        top_n = fc3.selectbox("Show", ["All", "Top 20", "Top 50"])

        df_show = result_df.copy()
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

        st.caption(f"Showing **{len(df_show)}** products — click any column header to sort")

        # ── Colour helpers
        def colour_profit(v):
            if v > 0:   return 'color: #375623; font-weight:600'
            if v < 0:   return 'color: #C00000; font-weight:600'
            return 'color: #aaa'

        def colour_decline_pct(v):
            return 'color: #C00000; font-weight:700'

        month_cols = months

        styled = (
            df_show.style
            .applymap(colour_profit,
                      subset=['Peak Profit ($)', 'Last Month Profit ($)', 'Decline ($)'] + month_cols)
            .applymap(colour_decline_pct, subset=['Decline (%)'])
            .format({
                'Peak Profit ($)':       '${:,.0f}',
                'Last Month Profit ($)': '${:,.0f}',
                'Decline ($)':           '${:,.0f}',
                'Decline (%)':           '{:.1f}%',
                **{m: '${:,.0f}' for m in month_cols},
            })
            .set_properties(**{'text-align': 'left'}, subset=['Product Name', 'Brand', 'ASINs'])
            .set_properties(**{'text-align': 'right'}, subset=['Peak Profit ($)', 'Last Month Profit ($)', 'Decline ($)', 'Decline (%)'] + month_cols)
            .set_table_styles([
                {'selector': 'th', 'props': [('background-color','#1F4E79'), ('color','white'),
                                              ('font-weight','700'), ('text-align','center')]},
                {'selector': 'td', 'props': [('vertical-align','top'), ('padding','8px 12px')]},
                {'selector': 'tr:hover td', 'props': [('background-color','#EBF3FB')]},
            ])
            .highlight_max(subset=['Decline ($)'], color='#fff0f0')
        )

        st.dataframe(
            df_show,
            use_container_width=True,
            height=600,
            column_config={
                'Product Name': st.column_config.TextColumn('Product Name', width='large'),
                'Brand': st.column_config.TextColumn('Brand', width='medium'),
                'ASINs': st.column_config.TextColumn('ASINs', width='medium'),
                'Peak Profit ($)': st.column_config.NumberColumn('Peak Profit ($)', format='$%.0f'),
                'Peak Month': st.column_config.TextColumn('Peak Month', width='small'),
                **{m: st.column_config.NumberColumn(m, format='$%.0f') for m in month_cols},
                'Last Month Profit ($)': st.column_config.NumberColumn(f'Last Month ({last_month}) $', format='$%.0f'),
                'Decline ($)': st.column_config.NumberColumn('Decline ($)', format='$%.0f'),
                'Decline (%)': st.column_config.ProgressColumn('Decline (%)', format='%.1f%%', min_value=0, max_value=100),
            },
            hide_index=True,
        )

        # ── Download
        csv = df_show.to_csv(index=False).encode('utf-8-sig')
        st.download_button("⬇️ Download results as CSV", csv,
                           file_name=f"profit_decline_{last_month}.csv", mime='text/csv')

elif not uploaded:
    st.info("👈 Upload a report file in the sidebar to get started.")
