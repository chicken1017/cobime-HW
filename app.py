# -*- coding: utf-8 -*-
"""
五平台比價 + 熱門商品 + 熱銷榜 (Streamlit)
平台：PChome / momo / 蝦皮 / 酷澎 Coupang / 樂天
執行方式：streamlit run app.py
"""
import re
import time
from urllib.parse import quote, urljoin

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}
TIMEOUT = 15

PCHOME_API = "https://ecshweb.pchome.com.tw/search/v3.3/all/results"
MOMO_API = "https://apisearch.momoshop.com.tw/momoSearchCloud/moec/textSearch"
SHOPEE_API = "https://shopee.tw/api/v4/search/search_items"
COUPANG_BASE = "https://www.tw.coupang.com"
RAKUTEN_BASE = "https://www.rakuten.com.tw"

PLATFORMS = ["PChome", "momo", "蝦皮", "酷澎", "樂天"]

# 抓取排序（只有部分平台支援原生排序，其餘維持平台預設順序）
SORT_OPTIONS = {
    "銷量高到低": "sales",
    "熱門度高到低": "sales",   # 抓銷量排序，再用本地熱門度重排
    "價格低到高": "price_asc",
    "價格高到低": "price_desc",
    "精選排序": "default",
}
PCHOME_SORT = {"sales": "sale/dc", "price_asc": "prc/ac",
               "price_desc": "prc/dc", "default": "rnk/dc"}

PRICE_BINS = [0, 200, 500, 1000, 2000, 5000, float("inf")]
PRICE_LABELS = ["0~200", "201~500", "501~1000", "1001~2000", "2001~5000", "5000以上"]

st.set_page_config(page_title="多平台比價・熱門・熱銷", page_icon="🛒", layout="wide")

NAN = float("nan")


def to_num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.]", "", str(v or ""))
    return float(s) if s else NAN


def parse_sold(text) -> float:
    """'已售出 1.2萬' / '3,500' → 數字"""
    s = str(text or "").replace(",", "")
    m = re.search(r"([\d.]+)\s*(萬|k|K)?", s)
    if not m:
        return NAN
    n = float(m.group(1))
    if m.group(2) == "萬":
        n *= 10000
    elif m.group(2) in ("k", "K"):
        n *= 1000
    return n


def row(platform, name, price, link, img="", origin=NAN, sold=NAN, reviews=NAN):
    return {"平台": platform, "name": name, "price": price, "originPrice": origin,
            "銷量": sold, "評價數": reviews, "連結": link, "圖片": img}


# ---------------------------------------------------------------- 爬蟲（單頁）
@st.cache_data(ttl=600, show_spinner=False)
def fetch_pchome(keyword: str, page: int, sort: str) -> list:
    params = {"q": keyword, "page": page, "sort": PCHOME_SORT.get(sort, "rnk/dc")}
    r = requests.get(PCHOME_API, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [row("PChome", p.get("name", ""), to_num(p.get("price")),
                f"https://24h.pchome.com.tw/prod/{p.get('Id', '')}",
                "https://img.pchome.com.tw/cs" + (p.get("picS") or p.get("picB") or ""),
                origin=to_num(p.get("originPrice")))
            for p in r.json().get("prods", [])]


@st.cache_data(ttl=600, show_spinner=False)
def fetch_momo(keyword: str, page: int, sort: str) -> list:
    payload = {
        "host": "momoshop", "flag": "searchEngine",
        "data": {
            "searchValue": keyword, "currentPage": str(page), "curPage": str(page),
            "cateCode": "", "cateLevel": "-1", "maxPage": "1",
            # 2 = 銷量排序（若 momo 調整參數，此處會自動退回預設順序）
            "searchType": "2" if sort == "sales" else "1",
            "specialGoodsType": "", "priceS": "0", "priceE": "9999999",
            "isBrandCategory": "N", "isBrandSearch": "N",
        },
    }
    headers = {**HEADERS, "Content-Type": "application/json;charset=UTF-8",
               "Referer": "https://www.momoshop.com.tw/",
               "Origin": "https://www.momoshop.com.tw"}
    r = requests.post(MOMO_API, json=payload, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    goods = (r.json().get("rtnData") or {}).get("goodsInfoList") or []
    return [row("momo", g.get("goodsName", ""),
                to_num(g.get("goodsPrice") or g.get("SALE_PRICE")),
                "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code="
                f"{g.get('goodsCode', '')}",
                g.get("imgUrl", ""),
                sold=to_num(g.get("soldCnt") or g.get("goodsSold")),
                reviews=to_num(g.get("ratingCount") or g.get("reviewCount")))
            for g in goods]


@st.cache_data(ttl=600, show_spinner=False)
def fetch_shopee(keyword: str, page: int, sort: str) -> list:
    by = {"sales": "sales", "price_asc": "price", "price_desc": "price"}.get(sort, "relevancy")
    order = "asc" if sort == "price_asc" else "desc"
    params = {"by": by, "keyword": keyword, "limit": 60,
              "newest": (page - 1) * 60, "order": order,
              "page_type": "search", "scenario": "PAGE_GLOBAL_SEARCH", "version": 2}
    headers = {**HEADERS, "Referer": f"https://shopee.tw/search?keyword={keyword}",
               "x-api-source": "pc", "x-requested-with": "XMLHttpRequest"}
    r = requests.get(SHOPEE_API, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    rows = []
    for it in r.json().get("items") or []:
        b = it.get("item_basic") or {}
        rating = b.get("item_rating") or {}
        origin = to_num(b.get("price_before_discount", 0)) / 100000
        rows.append(row(
            "蝦皮", b.get("name", ""), to_num(b.get("price", 0)) / 100000,
            f"https://shopee.tw/product/{b.get('shopid')}/{b.get('itemid')}",
            f"https://down-tw.img.susercontent.com/file/{b.get('image', '')}",
            origin=origin if origin > 0 else NAN,
            sold=to_num(b.get("historical_sold", 0)),
            reviews=to_num(sum(rating.get("rating_count", []) or [0]))))
    return rows


@st.cache_data(ttl=600, show_spinner=False)
def fetch_coupang(keyword: str, page: int, sort: str) -> list:
    params = {"component": "", "q": keyword, "page": page, "channel": "user"}
    if sort == "sales":
        params["sorter"] = "saleCountDesc"
    elif sort == "price_asc":
        params["sorter"] = "salePriceAsc"
    elif sort == "price_desc":
        params["sorter"] = "salePriceDesc"
    r = requests.get(f"{COUPANG_BASE}/np/search", params=params,
                     headers={**HEADERS, "Referer": COUPANG_BASE + "/"}, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []
    for li in soup.select("li.search-product, li[class*='search-product']"):
        name_el = li.select_one("div.name, .name")
        price_el = li.select_one("strong.price-value, .price-value")
        a = li.select_one("a.search-product-link, a[href]")
        if not (name_el and price_el and a):
            continue
        base_el = li.select_one("del.base-price, .base-price")
        cnt_el = li.select_one(".rating-total-count")
        img = li.select_one("img")
        src = ""
        if img:
            src = img.get("data-img-src") or img.get("src") or ""
            if src.startswith("//"):
                src = "https:" + src
        rows.append(row(
            "酷澎", name_el.get_text(strip=True), to_num(price_el.get_text()),
            urljoin(COUPANG_BASE, a.get("href", "")), src,
            origin=to_num(base_el.get_text()) if base_el else NAN,
            reviews=to_num(re.sub(r"[()]", "", cnt_el.get_text())) if cnt_el else NAN))
    return rows


@st.cache_data(ttl=600, show_spinner=False)
def fetch_rakuten(keyword: str, page: int, sort: str) -> list:
    url = f"{RAKUTEN_BASE}/search/{quote(keyword)}/"
    r = requests.get(url, params={"p": page}, headers={**HEADERS, "Referer": RAKUTEN_BASE + "/"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows, seen = [], set()
    for a in soup.select("a[href*='/product/']"):
        href = urljoin(RAKUTEN_BASE, a.get("href", "").split("?")[0])
        if href in seen:
            continue
        # 往上找含價格的商品卡片
        card, hops = a, 0
        while card.parent is not None and hops < 5 and not re.search(r"\$\s*[\d,]+", card.get_text(" ")):
            card, hops = card.parent, hops + 1
        text = card.get_text(" ", strip=True)
        m = re.search(r"(?:NT\$|\$)\s*([\d,]+)", text)
        img = card.find("img")
        name = (a.get("title") or (img.get("alt") if img else "") or a.get_text(strip=True)).strip()
        if not (m and name):
            continue
        seen.add(href)
        src = (img.get("data-src") or img.get("src") or "") if img else ""
        if src.startswith("//"):
            src = "https:" + src
        rows.append(row("樂天", name, to_num(m.group(1)), href, src))
    return rows


def fetch_one(platform: str, keyword: str, page: int, sort: str) -> list:
    return {"PChome": fetch_pchome, "momo": fetch_momo, "蝦皮": fetch_shopee,
            "酷澎": fetch_coupang, "樂天": fetch_rakuten}[platform](keyword, page, sort)


def crawl(keyword: str, pages: int, sort: str, delay: float, platforms: list) -> pd.DataFrame:
    """依序抓取各平台各頁；單一平台失敗只警告，不影響其他平台。"""
    rows, total, done = [], len(platforms) * pages, 0
    progress = st.progress(0.0, text="開始抓取…")
    for plat in platforms:
        for i in range(1, pages + 1):
            progress.progress(min(done / total, 1.0), text=f"{plat}：抓取第 {i}/{pages} 頁…")
            done += 1
            try:
                data = fetch_one(plat, keyword, i, sort)
            except (requests.RequestException, ValueError) as e:
                st.warning(f"⚠️ {plat} 第 {i} 頁抓取失敗：{e}")
                done += pages - i
                break
            if not data:
                st.info(f"{plat} 第 {i} 頁沒有解析到商品（可能被擋或頁面改版），已略過。")
                done += pages - i
                break
            rows.extend(data)
            if i < pages:
                time.sleep(delay)
        time.sleep(0.3)
    progress.progress(1.0, text="抓取完成")
    progress.empty()
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 資料整理 + 熱門度
def add_popularity(df: pd.DataFrame) -> pd.DataFrame:
    """
    熱門度 (0~100)：
      - 平台排名分 / 銷量分（有銷量資料時 70:30，否則 100% 排名分）→ 佔 80 分
      - 折扣幅度（最高計到 5 折）→ 佔 20 分
    注意：排名分以各平台內部排名計算，抓取排序為「銷量高到低」時最具參考價值。
    """
    df = df.copy()
    n = df.groupby("平台")["平台排名"].transform("max").clip(lower=1)
    rank_score = 1 - (df["平台排名"] - 1) / n
    sold_pct = df.groupby("平台")["銷量"].rank(pct=True)
    has_sold = df.groupby("平台")["銷量"].transform(lambda s: s.notna().any())
    base = rank_score.where(~has_sold, 0.7 * rank_score + 0.3 * sold_pct.fillna(0))
    discount = (1 - df["price"] / df["originPrice"]).where(
        df["originPrice"] > df["price"], 0).fillna(0).clip(0, 0.5) / 0.5
    df["折扣%"] = ((1 - df["price"] / df["originPrice"]).where(
        df["originPrice"] > df["price"]) * 100).round(0)
    df["熱門度"] = (base * 80 + discount * 20).round(1)
    return df


def clean(raw: pd.DataFrame, keyword: str, strict: bool, sort_label: str) -> pd.DataFrame:
    df = raw.copy()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["name", "price"])
    df = df[df["price"] > 0].drop_duplicates(subset=["平台", "連結"])
    if strict:
        tokens = [t for t in keyword.lower().split() if t]
        df = df[df["name"].str.lower().apply(lambda s: all(t in s for t in tokens))]
    df = df.reset_index(drop=True)
    df["平台排名"] = df.groupby("平台").cumcount() + 1      # 平台原始順序 = 排名
    df = add_popularity(df)
    if sort_label == "價格低到高":
        df = df.sort_values("price", ascending=True)
    elif sort_label == "價格高到低":
        df = df.sort_values("price", ascending=False)
    elif sort_label == "熱門度高到低":
        df = df.sort_values("熱門度", ascending=False)
    df = df.reset_index(drop=True)
    df.insert(0, "序號", df.index + 1)
    df["價格區間"] = pd.cut(df["price"], bins=PRICE_BINS, labels=PRICE_LABELS,
                        include_lowest=True).astype(str)
    return df


# ---------------------------------------------------------------- 圖表
def line_chart(df, top_n):
    d = df.head(top_n)
    fig = px.line(d, x="序號", y="price", color="平台", markers=True,
                  hover_data={"name": True, "price": True, "序號": False},
                  labels={"price": "價格 (NT$)", "序號": "商品序號"},
                  title=f"商品價格走勢（前 {len(d)} 筆）")
    fig.add_hline(y=df["price"].mean(), line_dash="dash", line_color="red",
                  annotation_text=f"整體平均 {df['price'].mean():,.0f}")
    return fig


def pie_chart(df):
    cnt = df["價格區間"].value_counts().reindex(PRICE_LABELS).dropna().reset_index()
    cnt.columns = ["價格區間", "商品數"]
    fig = px.pie(cnt, names="價格區間", values="商品數", hole=0.3,
                 title="各價格區間商品數占比")
    fig.update_traces(textinfo="percent+label+value")
    return fig


def sunburst_chart(df, top_n):
    d = df.head(top_n).copy()
    d["商品"] = d["序號"].astype(str) + ". " + d["name"].str.slice(0, 18)
    fig = px.sunburst(d, path=["平台", "價格區間", "商品"], values="price",
                      title=f"平台 → 價格區間 → 商品（前 {len(d)} 筆，大小代表價格）")
    fig.update_layout(height=700)
    return fig


def compare_charts(df):
    summary = (df.groupby("平台")["price"]
               .agg(商品數="count", 最低價="min", 平均價="mean", 中位數="median", 最高價="max")
               .round(0).reset_index())
    long = summary.melt(id_vars="平台", value_vars=["最低價", "平均價", "中位數"],
                        var_name="指標", value_name="價格")
    bar = px.bar(long, x="平台", y="價格", color="指標", barmode="group",
                 text_auto=",.0f", title="各平台價格指標比較")
    box = px.box(df, x="平台", y="price", points="outliers", color="平台",
                 hover_data=["name"], labels={"price": "價格 (NT$)"},
                 title="各平台價格分布（箱型圖）")
    return summary, bar, box


def hbar(d, x, title):
    d = d.copy()
    d["商品"] = d["name"].str.slice(0, 26)
    fig = px.bar(d.iloc[::-1], x=x, y="商品", color="平台", orientation="h",
                 hover_data=["name", "price"], title=title)
    fig.update_layout(height=max(360, 34 * len(d)), yaxis_title=None)
    return fig


def product_cards(d, extra):
    for _, r in d.iterrows():
        a, b = st.columns([1, 9])
        if r["圖片"]:
            a.image(r["圖片"], width=80)
        b.markdown(f"**[{r['name']}]({r['連結']})**  \n"
                   f"{r['平台']}｜**NT$ {int(r['price']):,}**｜{extra(r)}")


# ---------------------------------------------------------------- 介面
st.title("🛒 五平台比價・熱門商品・熱銷榜")
st.caption("PChome × momo × 蝦皮 × 酷澎 × 樂天")

with st.sidebar:
    st.header("查詢條件")
    keyword = st.text_input("商品關鍵字", value="滑鼠")
    pages = st.number_input("每平台抓取頁數", min_value=1, max_value=20, value=1, step=1)
    platforms = st.multiselect("比價平台", PLATFORMS, default=PLATFORMS)
    sort_label = st.selectbox("排序方式", list(SORT_OPTIONS.keys()),
                              help="熱門／熱銷榜建議選「銷量高到低」或「熱門度高到低」")
    strict = st.checkbox("只保留標題含所有關鍵字的商品", value=True)
    delay = st.slider("每頁間隔秒數（避免被封鎖）", 0.0, 10.0, 1.0, 0.5)
    st.header("圖表設定")
    chart_type = st.radio("圖表類型", ["折線圖", "圓餅圖", "旭日圖"])
    top_n = st.slider("折線圖／旭日圖顯示筆數", 10, 200, 70, 10)
    rank_n = st.slider("熱門／熱銷榜顯示筆數", 5, 30, 10, 5)
    run = st.button("🔍 查詢比價", type="primary", use_container_width=True)
    st.caption("momo、蝦皮、酷澎、樂天皆有反爬蟲機制，雲端 IP 可能被擋或頁面改版；"
               "失敗的平台會顯示警告，其他平台照常。")

if run:
    if not keyword.strip():
        st.error("請輸入商品關鍵字")
    elif not platforms:
        st.error("請至少選擇一個平台")
    else:
        with st.spinner("查詢中…"):
            raw = crawl(keyword.strip(), int(pages), SORT_OPTIONS[sort_label], delay, platforms)
        df_new = clean(raw, keyword.strip(), strict, sort_label) if not raw.empty else raw
        if df_new.empty:
            st.error("查無資料，請換個關鍵字或取消「嚴格比對」。")
        else:
            st.session_state["df"] = df_new
            st.session_state["kw"] = keyword.strip()

if "df" in st.session_state:
    df, kw = st.session_state["df"], st.session_state["kw"]

    st.subheader(f"「{kw}」查詢結果")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("商品數", f"{len(df):,}")
    c2.metric("平均價", f"{df['price'].mean():,.0f}")
    c3.metric("中位數", f"{df['price'].median():,.0f}")
    c4.metric("最高價", f"{df['price'].max():,.0f}")
    c5.metric("最低價", f"{df['price'].min():,.0f}")

    tab_cmp, tab_hot, tab_sell, tab_chart, tab_top, tab_raw = st.tabs(
        ["⚖️ 平台比價", "🔥 熱門商品", "🏆 熱銷榜", "📈 圖表分析", "🥇 各平台最低價", "📋 原始資料"])

    with tab_cmp:
        summary, bar, box = compare_charts(df)
        best = summary.loc[summary["最低價"].idxmin(), "平台"]
        st.success(f"🏆 最低價出現在 **{best}**：NT$ {int(summary['最低價'].min()):,}")
        st.dataframe(summary, use_container_width=True, hide_index=True)
        st.plotly_chart(bar, use_container_width=True)
        st.plotly_chart(box, use_container_width=True)

    with tab_hot:
        st.markdown("**熱門度**＝平台排名／銷量（80 分）＋折扣幅度（20 分），滿分 100。"
                    "排名以各平台內部順序計算，抓取排序選「銷量高到低」最準。")
        hot = df.sort_values("熱門度", ascending=False).head(rank_n)
        st.plotly_chart(hbar(hot, "熱門度", f"熱門商品 Top {len(hot)}"), use_container_width=True)
        product_cards(hot, lambda r: f"熱門度 {r['熱門度']}"
                      + (f"｜折扣 {int(r['折扣%'])}%" if pd.notna(r["折扣%"]) else ""))
        st.markdown("#### 各平台熱門 Top 3")
        cols = st.columns(len(df["平台"].unique()))
        for col, p in zip(cols, df["平台"].unique()):
            col.markdown(f"**{p}**")
            for _, r in df[df["平台"] == p].nlargest(3, "熱門度").iterrows():
                col.markdown(f"- [{r['name'][:22]}…]({r['連結']})  \n  NT$ {int(r['price']):,}｜{r['熱門度']} 分")

    with tab_sell:
        sold_df = df[df["銷量"].notna() & (df["銷量"] > 0)]
        st.markdown("#### 📦 銷量榜（有提供銷量數據的平台）")
        if sold_df.empty:
            st.info("目前沒有任何平台回傳銷量數據（蝦皮通常有，被擋時則無）。可參考下方「平台熱銷順序」。")
        else:
            top_sold = sold_df.nlargest(rank_n, "銷量")
            st.plotly_chart(hbar(top_sold, "銷量", f"銷量 Top {len(top_sold)}"),
                            use_container_width=True)
            product_cards(top_sold, lambda r: f"銷量 {int(r['銷量']):,}")
        st.markdown("#### 🥇 各平台熱銷順序（取平台排名前幾名）")
        if SORT_OPTIONS.get(sort_label) != "sales":
            st.warning("目前抓取排序不是「銷量高到低」，平台排名不代表熱銷順序。"
                       "請在左側改選「銷量高到低」後重新查詢。")
        for p in df["平台"].unique():
            st.markdown(f"**{p}**")
            product_cards(df[df["平台"] == p].nsmallest(min(5, rank_n), "平台排名"),
                          lambda r: f"平台排名 #{int(r['平台排名'])}")

    with tab_chart:
        if chart_type == "折線圖":
            st.plotly_chart(line_chart(df, top_n), use_container_width=True)
        elif chart_type == "圓餅圖":
            st.plotly_chart(pie_chart(df), use_container_width=True)
        else:
            st.plotly_chart(sunburst_chart(df, top_n), use_container_width=True)

    with tab_top:
        for p in df["平台"].unique():
            st.markdown(f"#### {p}")
            product_cards(df[df["平台"] == p].nsmallest(5, "price"), lambda r: "最低價前五名")

    with tab_raw:
        st.dataframe(
            df, use_container_width=True, hide_index=True,
            column_config={
                "連結": st.column_config.LinkColumn("連結", display_text="前往"),
                "圖片": st.column_config.ImageColumn("圖片", width="small"),
                "price": st.column_config.NumberColumn("價格", format="$%d"),
                "熱門度": st.column_config.ProgressColumn("熱門度", min_value=0, max_value=100, format="%.1f"),
            })

    st.download_button("⬇️ 下載 CSV", data=df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"比價_{kw}.csv", mime="text/csv")
else:
    st.info("請在左側輸入關鍵字、頁數並選擇平台，按下「查詢比價」。")
