"""
LSU OpenAlex Research Dashboard
================================
Run:
    pip install dash plotly requests pandas python-dotenv
    python lsu_dashboard.py
"""
import datetime
import os
import pickle
import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Literal

from dotenv import load_dotenv
import requests
import pandas as pd

import dash
from dash import Dash, dcc, html, dash_table, Input, Output
import plotly.express as px
import plotly.graph_objects as go

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

INSTITUTION_ID, ROR_ID = "I121820613", "05ect4e57"
API_KEY = os.getenv("OPENALEX_API_KEY")
BASE_URL = "https://api.openalex.org"
PORT = int(os.environ.get("PORT", 8050))

HOME_DIR = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME_DIR, "openalex_data")
os.makedirs(DATA_DIR, exist_ok=True)
CACHE_FILE = os.path.join(DATA_DIR, "works_cache.pkl")
DB_FILE = os.path.join(DATA_DIR, "works.db") 
CACHE_MAX_AGE_HOURS = 24

CURRENT_YEAR = datetime.date.today().year

# Colors
LSU_PURPLE, LSU_GOLD, LSU_GOLD_D = "#461D7C", "#FDD023", "#c9a800"
BG_COLOR, WHITE, SURFACE = "#f8f6fb", "#ffffff", "#f0ecf7"
BORDER, MUTED, TEXT, GREEN = "#ddd5ee", "#7a6d96", "#1a1030", "#16a34a"

AXIS_FONT = dict(family="Roboto, sans-serif", size=12, color=MUTED)
OA_COLORS = [LSU_PURPLE, LSU_GOLD, GREEN, "#a6a6a6", "#8b5cf6", "#f97316"]

PARAMS_BASE = {"mailto": "lsulibrary@lsu.edu"}
if API_KEY:
    PARAMS_BASE["api_key"] = API_KEY

# ── Global state ──────────────────────────────────────────────────────────────
data_ready, df, institution, funders_df = False, pd.DataFrame(), {}, pd.DataFrame()
session = requests.Session()
session.params.update(PARAMS_BASE)
_api_cache = {}

# ── API helpers ───────────────────────────────────────────────────────────────
def api_get(path, extra_params=None):
    params = {**PARAMS_BASE, **(extra_params or {})}
    key = (path, tuple(sorted(params.items())))
    if key in _api_cache:
        return _api_cache[key]
    r = session.get(f"{BASE_URL}{path}", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    _api_cache[key] = data
    return data

def fetch_institution():
    return api_get(f"/institutions/{INSTITUTION_ID}")

def fetch_works(start_year: int = 2020, end_year: int = CURRENT_YEAR) -> List[Dict]:
    filter_string = f"institutions.ror:{ROR_ID},from_publication_date:{start_year}-01-01,to_publication_date:{end_year}-12-31"
    fields = "id,title,publication_year,cited_by_count,primary_location,open_access,type,doi,apc_list,apc_paid,publication_date,authorships,primary_topic"
    all_results, cursor = [], "*"
    
    while True:
        params = {"filter": filter_string, "per-page": 200, "cursor": cursor, "select": fields, "sort": "publication_year:desc"}
        data = None
        for attempt in range(5):
            try:
                data = api_get("/works", params)
                break
            except requests.HTTPError as e:
                if e.response.status_code >= 500:
                    print(f"  Warning: OpenAlex 500, retrying...")
                    time.sleep(1.5)
                else:
                    raise
        if not data:
            break
            
        results = data.get("results", [])
        all_results.extend(results)
        print(f"  Fetched {len(all_results)}/{data.get('meta', {}).get('count', 0)} works...")
        
        cursor = data.get("meta", {}).get("next_cursor")
        if not results or not cursor or cursor == "*":
            break
        time.sleep(0.12)
    
    return all_results

def fetch_funders():
    data = api_get("/works", {"filter": f"institutions.ror:{ROR_ID}", "group-by": "funders.id", "per-page": 10})
    
    def resolve(g):
        name = g.get("key_display_name")
        if not name:
            try:
                name = api_get(f"/funders/{g['key'].split('/')[-1]}").get("display_name", g["key"])
            except:
                name = g["key"]
        return {"name": name, "count": g["count"]}
    
    with ThreadPoolExecutor(max_workers=5) as ex:
        rows = list(ex.map(resolve, data.get("group_by", [])))
    return pd.DataFrame(rows)

def works_to_df(works):
    rows = []
    for w in works:
        oa, apc_list = w.get("open_access") or {}, w.get("apc_list") or {}
        source = (w.get("primary_location") or {}).get("source") or {}
        authors = [
            a["author"]["display_name"]
            for a in (w.get("authorships") or [])
            if a.get("author", {}).get("display_name") and any(
                inst.get("ror") == f"https://ror.org/{ROR_ID}" or (inst.get("id") or "").endswith(INSTITUTION_ID)
                for inst in (a.get("institutions") or [])
            )
        ]
        rows.append({
            "id": w.get("id", ""), "title": w.get("title") or "Untitled", "year": w.get("publication_year"),
            "citations": w.get("cited_by_count", 0), "journal": source.get("display_name", "") or "",
            "oa": "Yes" if oa.get("is_oa") else "No", "oa_status": oa.get("oa_status", "unknown"),
            "type": w.get("type", ""), "doi": w.get("doi", ""), "apc_list": apc_list.get("value_usd"),
            "pub_date": w.get("publication_date", ""), "publisher": source.get("host_organization_name", "") or "",
            "authors": "|".join(filter(None, authors)), "topic": (w.get("primary_topic") or {}).get("display_name", ""),
        })
    return pd.DataFrame(rows)

def load_or_fetch_works():
    if os.path.exists(CACHE_FILE):
        if (time.time() - os.path.getmtime(CACHE_FILE)) / 3600 < CACHE_MAX_AGE_HOURS:
            print("  Loading works from cache...")
            with open(CACHE_FILE, "rb") as f:
                return pickle.load(f)
    print("  Fetching works from OpenAlex...")
    raw = fetch_works()
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(raw, f)
    return raw

def load_all_data():
    global institution, funders_df, df, data_ready
    print("Fetching institution info...")
    institution = fetch_institution()
    print("Fetching funders...")
    funders_df = fetch_funders()
    print("Loading works...")
    df = works_to_df(load_or_fetch_works())
    df = df.loc[:, df.columns.notnull()].rename(columns=lambda x: str(x).strip())
    conn = sqlite3.connect(DB_FILE)
    df.to_sql("works", conn, if_exists="replace", index=False)
    conn.close()
    data_ready = True
    print(f"Ready - {len(df):,} works loaded.\n")


# ── Styling ───────────────────────────────────────────────────────────────────
FONT = dict(family="Georgia, serif", color=TEXT)
LAYOUT_BASE = dict(font=FONT, plot_bgcolor=WHITE, paper_bgcolor=WHITE)

S = {
    "card": {"background": WHITE, "border": f"1px solid {BORDER}", "borderRadius": "26px",
             "padding": "14px 22px 20px", "boxShadow": "0 1px 4px rgba(70,29,124,0.06)",},
    "stat_card": lambda c: {"background": WHITE, "border": f"1px solid {BORDER}", "borderRadius": "12px",
                            "padding": "20px 18px", "minHeight": "90px", "borderLeft": f"4px solid {c}", 
                            "boxShadow": "0 1px 4px rgba(70,29,124,0.06)", "flex": "1"},
    "label": {"color": MUTED, "fontSize": "12px", "fontWeight": "600", "letterSpacing": "0.07em", "textTransform": "uppercase"},
    "value": {"color": TEXT, "fontSize": "36px", "fontWeight": "800", "lineHeight": "1.1", "margin": "4px 0 2px"},
    "sub": {"color": MUTED, "fontSize": "13px"},
    "section_title": {"color": LSU_PURPLE, "fontSize": "22px", "lineHeight": "1.1", "fontWeight": "800",
                      "letterSpacing": "-0.02em", "marginTop": "0px", "marginBottom": "2px"},
    "section_sub": {"color": MUTED, "fontSize": "13px", "marginBottom": "8px"},
    "tab_style": {"fontFamily": "Georgia, serif", "fontSize": "15px", "padding": "14px 28px", "color": MUTED,
                  "flex": "1", "textAlign": "center", "border": "none", "borderBottom": "3px solid transparent", "background": WHITE},
    "tab_selected": {"fontFamily": "Georgia, serif", "fontSize": "15px", "padding": "14px 28px", "color": LSU_PURPLE,
                     "flex": "1", "textAlign": "center", "fontWeight": "700", "border": "none",
                     "borderBottom": f"3px solid {LSU_PURPLE}", "background": f"{LSU_PURPLE}12"},
}

def stat_card(label, value, sub, color):
    return html.Div([
        html.Div(html.Span(label, style=S["label"]), style={"display": "flex", "justifyContent": "space-between"}),
        html.Div(str(value), style=S["value"]),
        html.Div(sub, style=S["sub"]),
    ], style=S["stat_card"](color))

def section_header(title, sub=None):
    children = [html.H2(title, style={**S["section_title"], "marginBottom": "6px"})]
    if sub:
        children.append(html.P(sub, style={**S["section_sub"], "marginBottom": "16px"}))  
    else:
        children.append(html.Div(style={"height": "16px"}))
    return html.Div(children)

# ── Chart helpers ─────────────────────────────────────────────────────────────
def apply_hover_and_axes(fig, orientation="v", unified=False):
    fig.update_layout(
        hovermode="x unified" if unified else "closest",
        hoverlabel=dict(font_color="white", font_size=16, font_family="Roboto, sans-serif",
                       bordercolor="#2b2b2b", namelength=-1),
        xaxis=dict(showspikes=False, tickfont=AXIS_FONT),
        yaxis=dict(showspikes=False, tickfont=AXIS_FONT),
    )
    template = "%{y}<br>■ %{x:,}<extra></extra>" if orientation == "h" else "%{x}<br>■ %{y:,}<extra></extra>"
    fig.update_traces(hovertemplate=template, marker_line_width=0)
    return fig

def apply_bar_layout(
    fig,
    *,
    x_range=None,
    x_dtick=None,
    left_margin=170,
    hover: bool | Literal["label"] = False,
):
    fig.update_layout(
        paper_bgcolor=WHITE,
        plot_bgcolor=WHITE,
        margin=dict(l=left_margin, r=8, t=6, b=8),
        bargap=0.35,
        hovermode="closest" if hover else False,
    )

    fig.update_xaxes(
        showgrid=True,
        gridcolor="#e5e7eb",
        showline=True,
        linecolor=BORDER,
        linewidth=1,
        zeroline=False,
        title=None,
        range=x_range,
        dtick=x_dtick,
        tickfont=AXIS_FONT,
    )

    fig.update_yaxes(
        showgrid=False,
        automargin=True,
        ticklabelstandoff=14,
        title=None,
        tickfont=AXIS_FONT,
    )

    if hover == "label":
        fig.update_traces(
            hovertemplate="%{customdata}<extra></extra>"
        )

    return fig



# ── Tables ────────────────────────────────────────────────────────────────────
OA_BADGE_COLORS = {
    "gold": {"backgroundColor": "#fef9c3", "color": "#854d0e"},
    "green": {"backgroundColor": "#dcfce7", "color": "#166534"},
    "hybrid": {"backgroundColor": "#e0f2fe", "color": "#075985"},
    "bronze": {"backgroundColor": "#ffedd5", "color": "#9a3412"},
    "closed": {"backgroundColor": "#f3f4f6", "color": "#374151"},
    "unknown": {"backgroundColor": "#f3f4f6", "color": "#6b7280"},
}

WORKS_COLUMNS = [
    {"name": "#",          "id": "_row",      "type": "numeric"},
    {"name": "Title",      "id": "title",     "type": "text",    "presentation": "markdown"},
    {"name": "Year",       "id": "year",      "type": "numeric"},
    {"name": "Citations",  "id": "citations", "type": "numeric"},
    {"name": "Journal",    "id": "journal",   "type": "text"},
    {"name": "OA",         "id": "oa",        "type": "text"},
    {"name": "Type",       "id": "type",      "type": "text"},
]

RECENT_COLUMNS = [
    {"name": "Work Title", "id": "title", "type": "text", "presentation": "markdown"},
    {"name": "Type",       "id": "type",        "type": "text"},
    {"name": "Status",     "id": "oa_status",   "type": "text"},
    {"name": "Date",       "id": "pub_date",    "type": "text"},
    {"name": "APC",        "id": "apc_display", "type": "text"},
]

BASE_TABLE_STYLE = {
    "style_table": {"borderRadius": "10px", "border": f"1px solid {BORDER}", "overflow": "hidden"},
    "style_header": {"backgroundColor": "#f3f1f8", "color": LSU_PURPLE, "fontWeight": "700", "fontSize": "10.5px",
                    "textTransform": "uppercase", "letterSpacing": "0.08em", "borderBottom": f"1px solid {BORDER}",
                    "fontFamily": "Georgia, serif", "padding": "6px 12px"},
    "style_cell": {"fontFamily": "Georgia, serif", "fontSize": "13px", "padding": "7px 12px", "border": "none",
                  "borderBottom": "1px solid #e6e2f0", "backgroundColor": WHITE, "color": TEXT, "textAlign": "left"},
    "style_data_conditional": [{"if": {"row_index": "odd"}, "backgroundColor": "#faf9fc"}],
}

TABLE_STYLE = dict(
    style_table={"overflowX": "auto", "borderRadius": "10px", "border": f"1px solid {BORDER}"},
    style_header={
        "backgroundColor": SURFACE,
        "color": LSU_PURPLE,
        "fontWeight": "700",
        "fontSize": "11px",
        "textTransform": "uppercase",
        "letterSpacing": "0.06em",
        "border": f"1px solid {BORDER}",
        "fontFamily": "Georgia, serif",
    },
    style_cell={
        "fontFamily": "Georgia, serif",
        "fontSize": "16px",
        "padding": "6px 12px",
        "border": f"1px solid {BORDER}",
        "backgroundColor": WHITE,
        "color": TEXT,
        "textAlign": "left",
        "overflow": "hidden",
        "textOverflow": "ellipsis",
        "maxWidth": "10",
        "lineHeight": "1.2",
    },
    style_cell_conditional=[
        
        {"if": {"column_id": "_row"}, "width": "44px", "maxWidth": "44px", "textAlign": "center", "padding": "0px", "color": MUTED,},
        {"if": {"column_id": "year"},      "width": "70px",  "textAlign": "center", "color": MUTED},
        {"if": {"column_id": "citations"}, "width": "65px",  "maxWidth": "65px", "textAlign": "right",  "fontWeight": "700"},
        {"if": {"column_id": "oa"},        "width": "55px",  "textAlign": "center"},
        {"if": {"column_id": "type"},      "width": "90px",  "textAlign": "center", "color": MUTED},
        {"if": {"column_id": "journal"},   "width": "140px", "maxWidth": "140px", "overflow": "hidden", "textOverflow": "ellipsis",},
        {"if": {"column_id": "title"},     "width": "300px", "maxWidth": "340px", "overflow": "hidden", "textOverflow": "ellipsis",},
    ],
    style_data_conditional=[
        {"if": {"row_index": "odd"}, "backgroundColor": "#faf9fc"},
        {"if": {"filter_query": "{oa} = 'Yes'", "column_id": "oa"}, "color": GREEN, "fontWeight": "700"},
    ],
    
    css=[
        {
            "selector": "td a",
            "rule": """
                font-family: Georgia, serif;
                font-size: 16px;
                font-weight: 500;
                color: #2563eb;
                text-decoration: none;
            """,
        },
        {
            "selector": "td a:hover",
            "rule": "text-decoration: underline;",
        },
    ],
    
    page_size=20,
    sort_action="native",
    filter_options={"case": "insensitive"},
    markdown_options={"link_target": "_blank"},
)

def make_recent_table(data_df: pd.DataFrame, table_id: str):
    tdf = data_df.copy().sort_values("pub_date", ascending=False).head(50)
    tdf["title"] = tdf.apply(
        lambda r: f"[{r['title']}]({r['id']})" if r["id"] else r["title"], axis=1
    )
    tdf["apc_display"] = tdf["apc_list"].apply(
        lambda v: f"${int(v):,}" if pd.notna(v) else "---"
    )
    return dash_table.DataTable(
        id=table_id,
        columns=RECENT_COLUMNS,
        data=tdf.to_dict("records"),
        style_table={"overflowX": "auto", "borderRadius": "10px",
                     "border": f"1px solid {BORDER}"},
        style_header={
            "backgroundColor": SURFACE, "color": LSU_PURPLE, "fontWeight": "700",
            "fontSize": "11px", "textTransform": "uppercase", "letterSpacing": "0.06em",
            "border": f"1px solid {BORDER}", "fontFamily": "Georgia, serif",
        },
        style_cell={
            "fontFamily": "Georgia, serif", "fontSize": "13px", "padding": "5px 12px",
            "border": f"1px solid {BORDER}", "backgroundColor": WHITE, "color": TEXT,
            "textAlign": "left", "overflow": "hidden", "textOverflow": "ellipsis", "maxWidth":"10",
        },
        style_cell_conditional=[
            {"if": {"column_id": "title"},      "width": "380px", "maxWidth": "380px", "textOverflow": "ellipsis",},
            {"if": {"column_id": "type"},       "width": "80px",  "textAlign": "center", "color": MUTED},
            {"if": {"column_id": "oa_status"},  "width": "55px",  "textAlign": "center"},
            {"if": {"column_id": "pub_date"},   "width": "105px", "textAlign": "center", "color": MUTED},
            {"if": {"column_id": "apc_display"},"width": "85px",  "textAlign": "right",  "fontWeight": "700"},
        ],
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "#faf9fc"},
            *[
                {
                    "if": {"filter_query": f'{{oa_status}} = "{status}"',
                           "column_id": "oa_status"},
                    "backgroundColor": colors["backgroundColor"],
                    "color": colors["color"],
                    "borderRadius": "999px",
                    "fontWeight": "600",
                    "fontSize": "11px",
                }
                for status, colors in OA_BADGE_COLORS.items()
            ],
        ],    
        css=[
            {
                "selector": "td a",
                "rule": """
                    font-family: Georgia, serif;
                    font-size: 16px;
                    font-weight: 500;
                    color: #2563eb;
                    text-decoration: none;
                """,
            },
            {
                "selector": "td a:hover",
                "rule": "text-decoration: underline;",
            },
        ],

        page_size=25,
        sort_action="native",
        markdown_options={"link_target": "_blank"},
    )

def make_cited_table(data_df, table_id):
    tdf = data_df.copy().reset_index(drop=True)
    tdf["_row"] = tdf.index + 1

    tdf["title"] = tdf.apply(
        lambda r: f"[{r['title']}]({r['id']})" if r["id"] else r["title"],
        axis=1,
    )

    return dash_table.DataTable(
        id=table_id,
        columns=WORKS_COLUMNS,
        data=tdf.to_dict("records"),
        **TABLE_STYLE,
    )


# ── Charts ────────────────────────────────────────────────────────────────────
def fig_pubs_year(fdf):
    pubs = fdf.groupby("year").size().reset_index(name="count")
    fig = px.bar(pubs, x="year", y="count", color_discrete_sequence=[LSU_PURPLE], labels={"count": "", "year": ""}, text_auto=True)
    fig.update_layout(**LAYOUT_BASE, title=None, height=260, margin=dict(l=10, r=10, t=10, b=0), hovermode=False,)
    fig.update_traces(marker_line_width=0, showlegend=False, textposition="inside", texttemplate="<b>%{y:,}</b>",
                     textfont=dict(family="Roboto, sans-serif", size=13, color="white",), hoverinfo="skip",)
    fig.update_xaxes(dtick=1, tickformat="d", ticklabelstandoff=10, showgrid=False, showline=True,
                    linecolor=BORDER, linewidth=1, title=None, tickfont=AXIS_FONT)
    fig.update_yaxes(rangemode="tozero", dtick=500, tickformat=",", showgrid=True, gridcolor="#e5e7eb",
                    showline=True, linecolor=BORDER, linewidth=1, title=None, tickfont=AXIS_FONT)
    return fig

def fig_oa_pie(fdf):
    oa = fdf.groupby("oa").size().reset_index(name="count")
    oa["name"] = oa["oa"].map({"Yes": "Open Access", "No": "Closed"}).fillna("Unknown")
    fig = px.pie(oa, names="name", values="count", color="name",
                color_discrete_map={"Open Access": GREEN, "Closed": "#a6a6a6", "Unknown": "#d1d5db"}, hole=0.4)
    fig.update_layout(font=FONT, paper_bgcolor=WHITE, margin=dict(l=0, r=0, t=10, b=10),
                     legend=dict(orientation="h", x=0.5, y=-0.15, xanchor="center", yanchor="top",
                                font=AXIS_FONT, tracegroupgap=15))
    fig.update_traces(hovertemplate="%{label}<br>%{value:,} (%{percent:.0%})<extra></extra>", textinfo="percent", textfont_size=13, marker=dict(line=dict(width=0)))
    return fig

def fig_types(fdf):
    KEEP = {"article", "preprint", "book-chapter"}
    tdf = fdf[fdf["type"] != ""].copy()
    tdf["type_grouped"] = tdf["type"].where(tdf["type"].isin(KEEP), "other",)
    grouped = tdf.groupby("type_grouped").size().reset_index(name="count").sort_values("count", ascending=False)
    color_map = {"article": LSU_PURPLE, "preprint": LSU_GOLD, "book-chapter": GREEN, "other": "#a6a6a6"}
    fig = px.pie(grouped, names="type_grouped", values="count", color="type_grouped", color_discrete_map=color_map, hole=0.4)
    fig.update_layout(font=FONT, paper_bgcolor=WHITE, margin=dict(l=0, r=0, t=10, b=60),
                     legend=dict(orientation="h", x=0.5, y=-0.25, xanchor="center", yanchor="top",
                                font=AXIS_FONT, tracegroupgap=8, itemwidth=30))
    fig.update_traces(hovertemplate="%{label}<br>%{value:,} (%{percent:.0%})<extra></extra>", textinfo="none", marker=dict(line=dict(width=0)))
    return fig

def fig_journals(fdf):
    top = fdf[fdf["journal"] != ""].groupby("journal").size().nlargest(10).reset_index(name="count").copy()
    MAX_JOURNAL_NAME_LENGTH = 45
    top["journal_trunc"] = top["journal"].apply(
        lambda x: x if len(x) <= MAX_JOURNAL_NAME_LENGTH 
        else x[:MAX_JOURNAL_NAME_LENGTH].rstrip() + "…")
    fig = px.bar(top.sort_values("count"), x="count", y="journal_trunc", orientation="h",
                color_discrete_sequence=[LSU_GOLD_D], labels={"count": "", "journal_trunc": ""})
    fig.update_traces(width=0.55, marker=dict(opacity=0.95))
    return apply_bar_layout(fig, x_dtick=100, left_margin=180,)

def fig_funders_chart():
    if funders_df.empty:
        return go.Figure()
    fdf_f = funders_df.head(10).copy()
    fdf_f["name_trunc"] = fdf_f["name"].apply(lambda x: x if len(x) <= 42 else x[:42].rstrip() + "…")
    fig = px.bar(fdf_f.sort_values("count"), x="count", y="name_trunc", orientation="h",
                color_discrete_sequence=[LSU_PURPLE], labels={"count": "", "name_trunc": ""})
    fig.update_traces(width=0.55, marker=dict(opacity=0.95),)
    return apply_bar_layout(fig, x_range=[0, 8000], x_dtick=1000, left_margin=180, hover=False)

def fig_apc_by_year(fdf: pd.DataFrame):
    apc = (fdf[fdf["apc_list"].notna()]
             .groupby("year")["apc_list"].sum()
             .reset_index(name="apc_sum").sort_values("year"))
    apc["label"] = apc["apc_sum"].apply(
        lambda v: f"${v/1_000_000:.1f}M" if v >= 1_000_000 else f"${v/1_000:.0f}K"
    )
    fig = px.bar(apc, x="year", y="apc_sum", text="label",
                 color_discrete_sequence=["#6aaa5e"],
                 labels={"apc_sum": "", "year": ""})
    fig.update_layout(**{**LAYOUT_BASE,
                         "margin": dict(l=10, r=10, t=10, b=10),},
                      title=None, height=380)
    fig = apply_hover_and_axes(fig, orientation="v")
    fig.update_xaxes(dtick=1, tickformat="d", ticklabelstandoff=10, showgrid=False, showline=True,
                    linecolor=BORDER, linewidth=1, title=None)
    fig.update_yaxes(showgrid=True, gridcolor="#e5e7eb", showline=True, linecolor=BORDER, linewidth=1,
                    tickformat="~s", tickprefix="$", rangemode="tozero", title=None)
    fig.update_traces(marker_line_width=0, width=0.65, showlegend=False, hovertemplate="<b>%{x}</b><br>$%{y:,}<extra></extra>",
                     textposition="inside", texttemplate="<b>%{text}</b>", textfont=dict(color="white", size=14, family="Roboto, sans-serif"))
    return fig

def build_trend_data(fdf):
    trend = fdf.groupby("year").agg(total=("id", "count"), oa_all=("oa", lambda x: (x == "Yes").sum()),
                                    gold=("oa_status", lambda x: (x == "gold").sum()),
                                    hybrid=("oa_status", lambda x: (x == "hybrid").sum())).reset_index().sort_values("year")
    pub_trend = fdf[fdf["publisher"] != ""].groupby(["year", "publisher"]).size().reset_index(name="count")
    top_publishers = pub_trend.groupby("publisher")["count"].sum().nlargest(5).index.tolist()
    return trend, pub_trend, top_publishers

def f_fig_pub_trends(trend):
    fig = go.Figure()
    for col, name, color, dash in [("total", "Total", "#461D7C", "solid"), ("oa_all", "OA (All)", "#16a34a", "solid"),
                                    ("gold", "Gold OA", "#FDD023", "dash"), ("hybrid", "Hybrid OA", "#7c3aed", "dot")]:
        fig.add_trace(go.Scatter(x=trend["year"], y=trend[col], name=name, mode="lines",
                                line=dict(color=color, dash=dash, width=2.5)))
    fig.update_layout(**LAYOUT_BASE, title=None, height=350, margin=dict(l=40, r=20, t=90, b=40),
                     legend=dict(orientation="h", x=0.5, xanchor="center", y=1.08, yanchor="bottom",
                                title=None, font=AXIS_FONT))
    fig = apply_hover_and_axes(fig, orientation="v")
    fig.update_xaxes(dtick=1, tickformat="d", ticklabelstandoff=10, showgrid=False, showline=True,
                    linecolor=BORDER, linewidth=1)
    fig.update_yaxes(rangemode="tozero", dtick=500, tickformat=",", showgrid=True, gridcolor="#e5e7eb",
                    showline=True, linecolor=BORDER, linewidth=1)
    return fig

def f_fig_publisher_trends(pub_trend, top_publishers):
    fig = go.Figure()
    colors = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f"]
    for i, publisher in enumerate(top_publishers):
        pdata = pub_trend[pub_trend["publisher"] == publisher]
        fig.add_trace(go.Scatter(x=pdata["year"], y=pdata["count"], name=publisher, mode="lines+markers",
                                line=dict(color=colors[i % len(colors)], width=2.5),
                                marker=dict(size=6, color=colors[i % len(colors)], line=dict(width=0))))
    fig.update_layout(title=None, height=320, paper_bgcolor=WHITE, plot_bgcolor=WHITE,
                     margin=dict(l=40, r=20, t=20, b=40),
                     legend=dict(orientation="h", x=0.5, y=1.08, xanchor="center", yanchor="bottom", font=AXIS_FONT))
    fig = apply_hover_and_axes(fig, orientation="v")
    fig.update_xaxes(dtick=1, tickformat="d", showgrid=False, showline=False, zeroline=False)
    fig.update_yaxes(rangemode="tozero", tickformat=",", showgrid=True, gridcolor="#e5e7eb", showline=False, zeroline=False)
    return fig

# ── App ───────────────────────────────────────────────────────────────────────
app = Dash(__name__, suppress_callback_exceptions=True,
          external_stylesheets=["https://fonts.googleapis.com/css2?family=IM+Fell+English&display=swap"])
app.title = "LSU OpenAlex Dashboard"

app.index_string = """<!DOCTYPE html><html><head>{%metas%}<title>{%title%}</title>{%favicon%}{%css%}<style>
.rc-slider-rail{background-color:#ddd5ee;height:4px}.rc-slider-track,.rc-slider-track-1,.rc-slider-track-2{background-color:#461D7C!important;height:4px}
.rc-slider-handle{width:14px;height:14px;background-color:#461D7C!important;border:2px solid #fff!important;margin-top:-5px;box-shadow:0 0 0 2px rgba(70,29,124,0.15)}
.rc-slider-handle:hover,.rc-slider-handle:focus,.rc-slider-handle:active{border-color:#461D7C!important;box-shadow:0 0 0 4px rgba(70,29,124,0.25)!important}
.rc-slider-dot-active{border-color:#461D7C!important;background-color:#461D7C!important}.rc-slider-dot{border-color:#ddd5ee}
.rc-slider-tooltip,.dash-slider .show-value,input[type=number]{display:none!important}
</style></head><body>{%app_entry%}<footer>{%config%}{%scripts%}{%renderer%}</footer></body></html>"""

app.layout = html.Div(style={"minHeight": "100vh", "background": BG_COLOR, "fontFamily": "Georgia, serif", "color": TEXT}, children=[
    html.Div(style={"background": LSU_PURPLE, "padding": "0 28px", "height": "58px", "display": "flex", "alignItems": "center", "gap": "14px"}, children=[
        html.Img(src="https://commons.wikimedia.org/w/index.php?title=Special:Redirect/file/Louisiana%20State%20University%20%28logo%29.svg",
                style={"height": "36px", "objectFit": "contain", "filter": "brightness(0) invert(1)"}),
        html.Div([html.Div("Louisiana State University", style={"fontWeight": "700", "fontSize": "16px", "color": "white"}),
                 html.Div("OpenAlex Research Dashboard", style={"fontSize": "13px", "color": "rgba(255,255,255,0.62)"})]),
    ]),
    html.Div(style={"height": "4px", "background": f"linear-gradient(90deg,{LSU_GOLD},{LSU_GOLD_D})"}),
    html.Div(style={"maxWidth": "1240px", "margin": "0 auto", "padding": "0 28px"}, children=[
        html.Div(style={"padding": "10px 0 24px", "marginBottom": "32px"}, children=[
            dcc.RangeSlider(id="year-slider", min=2020, max=CURRENT_YEAR, step=1, value=[2020, CURRENT_YEAR],
                          marks={str(y): {"label": str(y), "style": {"color": MUTED, "fontSize": "18px"}}
                                for y in range(2020, CURRENT_YEAR + 1, 2)}, updatemode="mouseup"),
        ]),
        html.Div(style={"display": "flex", "justifyContent": "center"}, children=[
            dcc.Tabs(id="tabs", value="overview", style={"borderBottom": f"1px solid {BORDER}", "width": "100%"},
                    parent_style={"width": "100%"}, children=[
                dcc.Tab(label="Overview", value="overview", style=S["tab_style"], selected_style=S["tab_selected"]),
                dcc.Tab(label="Recent Publications", value="publications", style=S["tab_style"], selected_style=S["tab_selected"]),
                dcc.Tab(label="Top Cited", value="top-cited", style=S["tab_style"], selected_style=S["tab_selected"]),
                dcc.Tab(label="Authors & Topics", value="authors-topics", style=S["tab_style"], selected_style=S["tab_selected"]),
            ]),
        ]),
        html.Div(id="tab-content", style={"paddingTop": "22px", "paddingBottom": "60px"}),
    ]),
    html.Div(style={"borderTop": f"3px solid {LSU_GOLD}", "background": LSU_PURPLE, "padding": "13px 28px",
                   "display": "flex", "justifyContent": "space-between", "alignItems": "center"}, children=[
        html.Span("Louisiana State University · OpenAlex Research Dashboard", style={"color": "rgba(255,255,255,0.5)", "fontSize": "12px"}),
        html.Span(["Data via ", html.A("OpenAlex", href="https://openalex.org", target="_blank", style={"color": LSU_GOLD}),],
                 style={"color": "rgba(255,255,255,0.4)", "fontSize": "12px"}),
    ]),
])

@app.callback(Output("tab-content", "children"), Input("tabs", "value"), Input("year-slider", "value"))
def render_tab(tab, year_range):
    if not data_ready or df.empty:
        return html.Div([html.H3("Loading data from OpenAlex...", style={"textAlign": "center", "color": LSU_PURPLE, "marginTop": "100px"}),
                        html.P("This may take a few minutes on first load.", style={"textAlign": "center", "color": MUTED})])
    
    year_min, year_max = year_range
    fdf = df[(df["year"] >= year_min) & (df["year"] <= year_max)].copy()
    total, f_total_cit = len(fdf), int(fdf["citations"].sum())
    f_avg_cit = round(fdf["citations"].mean(), 1) if total else 0
    f_oa_count, f_oa_pct = int((fdf["oa"] == "Yes").sum()), round(int((fdf["oa"] == "Yes").sum()) / total * 100) if total else 0
    
    if tab == "overview":
        return html.Div([
            html.Div(style={"display": "flex", "gap": "20px", "marginBottom": "24px"}, children=[
                stat_card("Works", f"{total:,}", f"{year_min}-{year_max}", LSU_PURPLE),
                stat_card("Total Citations", f"{f_total_cit:,}", "across filtered works", LSU_GOLD_D),
                stat_card("Avg Citations", f"{f_avg_cit}", "per work", "#7c3aed"),
                stat_card("Open Access", f"{f_oa_pct}%", f"{f_oa_count} OA works", GREEN),
            ]),
            html.Div(style={"display": "grid", "gridTemplateColumns": "2fr 1fr 1fr", "gap": "20px", "marginBottom": "24px", "alignItems": "stretch",}, children=[
                html.Div(style=S["card"], children=[section_header("Publications per Year"),
                                                    dcc.Graph(figure=fig_pubs_year(fdf), config={"displayModeBar": False}, style={"height": "230px"})]),
                html.Div(style=S["card"], children=[section_header("Work Types"),
                                                    dcc.Graph(figure=fig_types(fdf), config={"displayModeBar": False}, style={"height": "230px"})]),
                html.Div(style=S["card"], children=[section_header("OA Status"),
                                                    dcc.Graph(figure=fig_oa_pie(fdf), config={"displayModeBar": False}, style={"height": "230px"})]),
            ]),
            html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "20px", "marginBottom": "24px"}, children=[
                html.Div(style=S["card"], children=[
                    section_header("Estimated APCs by Year"),
                    dcc.Graph(figure=fig_apc_by_year(fdf), config={"displayModeBar": False}, style={"height": "380px"}),
                    html.P(["Reflects data from ", html.A("OpenAPC", href="https://openapc.net", target="_blank", style={"color": LSU_PURPLE}),
                           " and APC list pricing. Actual costs may differ."],
                          style={"fontSize": "11px", "color": MUTED, "marginTop": "8px", "fontStyle": "italic"}),
                ]),
                html.Div(style=S["card"], children=[
                    section_header("Publication Trends", f"OA breakdown & top 5 publishers · {year_min}–{year_max}"),
                    dcc.Tabs(id="trends-tabs", value="oa-trends", style={"marginBottom": "6px"}, children=[
                        dcc.Tab(label="OA Trends", value="oa-trends", style=S["tab_style"], selected_style=S["tab_selected"]),
                        dcc.Tab(label="By Publisher", value="by-publisher", style=S["tab_style"], selected_style=S["tab_selected"]),
                    ]),
                    html.Div(id="trends-chart-container"),
                ]),
            ]),
            html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "20px", "marginBottom": "24px"}, children=[
                html.Div(style=S["card"], children=[section_header("Top Publication Venues", "Most frequent journals / sources"),
                                                    dcc.Graph(figure=fig_journals(fdf), config={"displayModeBar": False}, style={"height": "340px"})]),
                html.Div(style=S["card"], children=[section_header("Publications by Funding Agency", "Top 10 funders"),
                                                    dcc.Graph(figure=fig_funders_chart(), config={"displayModeBar": False}, style={"height": "340px"})]),
            ]),
        ])
    
    if tab == "publications":
        return html.Div(style=S["card"], children=[section_header("Recent Publications"),
            make_recent_table(fdf, "works-table"),],)
    
    if tab == "top-cited":
        return html.Div(style=S["card"],children=[section_header("Top 20 Most Cited Works"),
                        make_cited_table(fdf.nlargest(20, "citations"), "cited-table")])
    
    if tab == "authors-topics":
        top_authors = fdf["authors"].dropna().str.split("|").explode().str.strip().value_counts().head(10).reset_index()
        top_authors.columns = ["author", "count"]
        top_topics = fdf["topic"].replace("", pd.NA).dropna().value_counts().head(10).reset_index()
        top_topics.columns = ["topic", "count"]
        
        def group_table(data, name_col, table_id):
            return dash_table.DataTable(
                id=table_id,
                columns=[
                    {"name": "#",     "id": "#",     "type": "numeric"},
                    {"name": "Name",  "id": "Name",  "type": "text"},
                    {"name": "Works", "id": "Works", "type": "text"},
                ],
                data=[
                    {"#": i + 1, "Name": r[name_col], "Works": f"{r['count']:,}"}
                    for i, r in data.iterrows()
                ],

                # ── Table container ─────────────────────────
                style_table={
                    "borderRadius": "10px",
                    "border": f"1px solid {BORDER}",
                    "overflow": "hidden",
                },

                # ── Header ──────────────────────────────────
                style_header={
                    "backgroundColor": SURFACE,
                    "color": LSU_PURPLE,
                    "fontWeight": "800",
                    "fontSize": "16px",           
                    "textTransform": "uppercase",
                    "letterSpacing": "0.08em",
                    "borderBottom": f"1px solid {BORDER}",
                    "fontFamily": "Georgia, serif",
                    "padding": "8px 12px",
                },

                # ── Cells ───────────────────────────────────
                style_cell={
                    "fontFamily": "Georgia, serif",
                    "fontSize": "18px",             
                    "padding": "10px 12px",          
                    "border": "none",
                    "borderBottom": "1px solid #e6e2f0",
                    "backgroundColor": WHITE,
                    "color": TEXT,
                    "textAlign": "left",
                    "whiteSpace": "nowrap",
                },

                # ── Column-specific tweaks ──────────────────
                style_cell_conditional=[
                    {
                        "if": {"column_id": "#"},
                        "width": "48px",
                        "textAlign": "center",
                        "color": MUTED,
                        "fontSize": "13px",
                    },
                    {
                        "if": {"column_id": "Works"},
                        "width": "90px",
                        "textAlign": "right",
                        "fontWeight": "800",         
                        "fontSize": "15px",
                    },
                ],

                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": "#faf9fc"},
                ],

                page_size=10,
                sort_action="native",
            )

        return html.Div(
            style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "18px"},
            children=[
                html.Div(
                    style=S["card"],
                    children=[
                        section_header(
                            "Top Authors",
                            f"By publication count · {year_min}–{year_max}",
                        ),
                        group_table(top_authors, "author", "authors-table"),
                    ],
                ),
                html.Div(
                    style=S["card"],
                    children=[
                        section_header(
                            "Top Research Topics",
                            f"Primary topics · {year_min}–{year_max}",
                        ),
                        group_table(top_topics, "topic", "topics-table"),
                    ],
                ),
            ],
        )

    return html.Div("Select a tab.")

@app.callback(Output("trends-chart-container", "children"), Input("trends-tabs", "value"), Input("year-slider", "value"))
def update_trends_chart(trends_tab, year_range):
    if not data_ready or df.empty:
        return html.Div()
    year_min, year_max = year_range
    fdf = df[(df["year"] >= year_min) & (df["year"] <= year_max)].copy()
    trend, pub_trend, top_publishers = build_trend_data(fdf)
    fig = f_fig_pub_trends(trend) if trends_tab == "oa-trends" else f_fig_publisher_trends(pub_trend, top_publishers)
    return dcc.Graph(figure=fig, config={"displayModeBar": False}, style={"height": "320px"})

# ── Server entry point (required for cPanel / Passenger) ──────────────────────
server = app.server


# ── Background data loading (safe for Passenger) ──────────────────────────────
from threading import Thread, Lock

_data_lock = Lock()
_data_started = False

@server.before_first_request
def start_background_loader():
    """
    Start background data loading exactly once.
    Safe for cPanel Passenger (avoids duplicate threads).
    """
    global _data_started
    with _data_lock:
        if not _data_started:
            _data_started = True
            Thread(target=load_all_data, daemon=True).start()

