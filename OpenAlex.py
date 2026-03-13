"""
LSU OpenAlex Research Dashboard
================================
Run:
    pip install dash plotly requests pandas
    python lsu_dashboard.py

Then open http://localhost:8050 in your browser.
"""

import requests
import pandas as pd
from collections import Counter

import dash
from dash import Dash, dcc, html, dash_table, Input, Output, State
import plotly.express as px
import plotly.graph_objects as go

import datetime

import os
import pickle
import time

import threading

CACHE_FILE = "works_cache.pkl"
CACHE_MAX_AGE_HOURS = 24

# ── Constants ─────────────────────────────────────────────────────────────────
INSTITUTION_ID = "I121820613"
ROR_ID         = "05ect4e57"
API_KEY        = "0YhSOIcS5c8tcbR9XwLDL6"
BASE_URL       = "https://api.openalex.org"

LSU_PURPLE = "#461D7C"
LSU_GOLD   = "#FDD023"
LSU_GOLD_D = "#c9a800"
BG_COLOR   = "#f8f6fb"
WHITE      = "#ffffff"
SURFACE    = "#f0ecf7"
BORDER     = "#ddd5ee"
MUTED      = "#7a6d96"
TEXT       = "#1a1030"
GREEN      = "#16a34a"

OA_COLORS  = ["#1f1f1f",  "#00b300", LSU_GOLD, "#7e4c39", "#3833ea", "#a6a6a6"]

PARAMS_BASE = {"api_key": API_KEY, "mailto": "lsulibrary@lsu.edu"}

# ── Global data storage ──
data_ready = False
df = pd.DataFrame()
institution = {}
funders_df = pd.DataFrame()
authors_gb = []
topics_gb = []
oa_gb = []
type_gb = []

# ── Data fetching ─────────────────────────────────────────────────────────────

def api_get(path, extra_params=None):
    params = {**PARAMS_BASE, **(extra_params or {})}
    r = requests.get(f"{BASE_URL}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_institution():
    return api_get(f"/institutions/{INSTITUTION_ID}")


def fetch_works(start_year=None, end_year=None):
    filters = [f"institutions.ror:{ROR_ID}"]
    if start_year:
        filters.append(f"from_publication_date:{start_year}-01-01")
    if end_year:
        filters.append(f"to_publication_date:{end_year}-12-31")
    filter_string = ",".join(filters)
    all_results = []
    cursor = "*"
    fields = "id,title,publication_year,cited_by_count,primary_location,open_access,type,doi,apc_list,apc_paid,publication_date,authorships,primary_topic"
    while True:
        data = api_get("/works", {
            "filter":   filter_string,
            "per-page": 200,
            "cursor":   cursor,
            "select":   fields,
            "sort":     "publication_year:desc",
        })
        results = data.get("results", [])
        all_results.extend(results)
        total = data.get("meta", {}).get("count", 0)
        print(f"  Fetched {len(all_results)}/{total} works")
        cursor = data.get("meta", {}).get("next_cursor")
        if not results or not cursor:
            break
    return all_results


def fetch_funders():
    data = api_get("/works", {
        "filter": f"institutions.ror:{ROR_ID}",
        "group-by": "funders.id",
        "per-page": 10,
    })
    items = data.get("group_by", [])
    rows = []
    for g in items:
        name = g.get("key_display_name") or ""
        if not name:
            funder_id = g["key"].split("/")[-1]
            try:
                funder_data = api_get(f"/funders/{funder_id}")
                name = funder_data.get("display_name", g["key"])
            except Exception:
                name = g["key"]
        rows.append({"name": name, "count": g["count"]})
    return pd.DataFrame(rows)


def fetch_group_by(group_by_field, per_page=25):
    data = api_get("/works", {
        "filter": f"institutions.ror:{ROR_ID}",
        "group-by": group_by_field,
        "per-page": per_page,
    })
    return data.get("group_by", [])


def works_to_df(works):
    rows = []
    for w in works:
        oa = w.get("open_access") or {}
        apc_list = w.get("apc_list") or {}
        apc_paid = w.get("apc_paid") or {}
        source = ((w.get("primary_location") or {}).get("source") or {})
        
        authorships = w.get("authorships") or []
        authors = []
        for a in authorships:
            institutions = a.get("institutions") or []
            is_lsu = any(
                (inst.get("ror") or "") == f"https://ror.org/{ROR_ID}" or 
                (inst.get("id") or "").endswith(INSTITUTION_ID)
                for inst in institutions
            )
            if is_lsu and a.get("author", {}).get("display_name"):
                authors.append(a["author"]["display_name"])
                
        topic = (w.get("primary_topic") or {}).get("display_name", "")
        
        rows.append({
            "id":        w.get("id", ""),
            "title":     w.get("title") or "Untitled",
            "year":      w.get("publication_year"),
            "citations": w.get("cited_by_count", 0),
            "journal":   source.get("display_name", "") or "",
            "oa":        "Yes" if oa.get("is_oa") else "No",
            "oa_status": oa.get("oa_status", "unknown"),
            "type":      w.get("type", ""),
            "doi":       w.get("doi", ""),
            "apc_list":  apc_list.get("value_usd"),
            "apc_paid":  apc_paid.get("value_usd"),
            "pub_date":  w.get("publication_date", ""),
            "publisher": source.get("host_organization_name", "") or "",
            "authors": "|".join(authors),
            "topic":   topic,
        })
def gb_to_df(gb):
    return pd.DataFrame([{"name": g["key_display_name"] or g["key"], "count": g["count"]} for g in gb])

# ── Load all data once at startup ─────────────────────────────────────────────

CACHE_FILE = "works_cache.pkl"
CACHE_MAX_AGE_HOURS = 24
CURRENT_YEAR = datetime.date.today().year

def load_or_fetch_works():
    if os.path.exists(CACHE_FILE):
        age_hours = (time.time() - os.path.getmtime(CACHE_FILE)) / 3600
        if age_hours < CACHE_MAX_AGE_HOURS:
            print("Loading works from cache...")
            with open(CACHE_FILE, "rb") as f:
                return pickle.load(f)
    print("Fetching works from OpenAlex...")
    data = fetch_works(start_year=2020, end_year=CURRENT_YEAR)
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(data, f)
    return data

data_ready = False
df = pd.DataFrame()
institution = {}
funders_df = pd.DataFrame()
authors_gb = []
topics_gb = []
oa_gb = []
type_gb = []
pubs_year    = pd.DataFrame()             
top_journals = pd.DataFrame()
top_cited    = pd.DataFrame()
authors_df   = pd.DataFrame()
topics_df    = pd.DataFrame()
oa_df        = pd.DataFrame()
type_df      = pd.DataFrame()

def load_all_data():
    global df, institution, funders_df, authors_gb, topics_gb, oa_gb, type_gb
    global pubs_year, top_journals, top_cited, authors_df, topics_df, oa_df, type_df
    global data_ready

    print("Fetching institution info…")
    institution = fetch_institution()
    print("Fetching works…")
    works_raw = load_or_fetch_works()
    df = works_to_df(works_raw)

    print("Fetching group-by data…")
    funders_df  = fetch_funders()
    authors_gb  = fetch_group_by("authorships.author.id", 10)
    topics_gb   = fetch_group_by("primary_topic.id", 10)
    oa_gb       = fetch_group_by("open_access.oa_status")
    type_gb     = fetch_group_by("type")

    pubs_year = (
        df[df["year"].notna() & (df["year"] >= 2000)]
        .groupby("year").size().reset_index(name="count")
        .sort_values("year")
    )
    top_journals = (
        df[df["journal"] != ""]
        .groupby("journal").size().reset_index(name="count")
        .sort_values("count", ascending=False)
        .head(12)
    )
    top_cited  = df.sort_values("citations", ascending=False).head(25).copy()
    authors_df = gb_to_df(authors_gb)
    topics_df  = gb_to_df(topics_gb)
    oa_df      = pd.DataFrame([{"name": g["key"], "count": g["count"]} for g in oa_gb])
    type_df    = gb_to_df(type_gb)

    data_ready = True
    print(f"Loaded {len(df)} works.\n")

threading.Thread(target=load_all_data, daemon=True).start()

# ── Figures ───────────────────────────────────────────────────────────────────
FONT = dict(family="Georgia, serif", color=TEXT)
LAYOUT_BASE = dict(
    font=FONT,
    plot_bgcolor=WHITE,
    paper_bgcolor=WHITE,
    margin=dict(l=10, r=10, t=30, b=10),
    xaxis=dict(showgrid=False, linecolor=BORDER, tickcolor=BORDER, tickfont=dict(color=MUTED, size=11)),
    yaxis=dict(showgrid=True, gridcolor=BORDER, linecolor=BORDER, tickfont=dict(color=MUTED, size=11)),
)


def fig_pubs_year():
    fig = px.bar(pubs_year, x="year", y="count", color_discrete_sequence=[LSU_PURPLE])
    fig.update_layout(**LAYOUT_BASE, title=None)
    fig.update_traces(marker_line_width=0)
    return fig

def fig_journals():
    fig = px.bar(
        top_journals.sort_values("count"), x="count", y="journal",
        orientation="h", color_discrete_sequence=[LSU_GOLD_D],
    )
    layout = {
        **LAYOUT_BASE,
        "yaxis": dict(showgrid=False, linecolor=BORDER, tickfont=dict(color=TEXT, size=11)),
        "xaxis": dict(showgrid=True, gridcolor=BORDER, linecolor=BORDER, tickfont=dict(color=MUTED, size=11)),
        "title": None,
        "height": 340,
    }
    fig.update_layout(**layout)
    fig.update_traces(marker_line_width=0)
    return fig


def fig_oa_pie():
    fig = px.pie(
        oa_df, names="name", values="count",
        color_discrete_sequence=OA_COLORS,
        hole=0.4,
    )
    fig.update_layout(font=FONT, paper_bgcolor=WHITE, margin=dict(l=10, r=10, t=10, b=10),
                      legend=dict(font=dict(size=12)))
    fig.update_traces(textinfo="none", textfont_size=12)
    return fig


def fig_types():
    fig = px.pie(
        type_df, names="name", values="count",
        color_discrete_sequence=OA_COLORS,
        hole=0.4,
    )
    fig.update_traces(textinfo="none")
    fig.update_layout(font=FONT, paper_bgcolor=WHITE, margin=dict(l=10, r=10, t=10, b=10),
                      legend=dict(font=dict(size=12)))
    return fig


def fig_oa_trend():
    fig = px.bar(pubs_year.tail(15), x="year", y="count", color_discrete_sequence=[GREEN])
    fig.update_layout(**LAYOUT_BASE, title=None)
    fig.update_traces(marker_line_width=0)
    return fig


# ── Styles ────────────────────────────────────────────────────────────────────
S = {
    "card": {
        "background": WHITE,
        "border": f"1px solid {BORDER}",
        "borderRadius": "12px",
        "padding": "18px 20px",
        "boxShadow": "0 1px 4px rgba(70,29,124,0.06)",
    },
    "stat_card": lambda color: {
        "background": WHITE,
        "border": f"1px solid {BORDER}",
        "borderRadius": "12px",
        "padding": "16px 18px",
        "borderLeft": f"4px solid {color}",
        "boxShadow": "0 1px 4px rgba(70,29,124,0.06)",
        "flex": "1",
    },
    "label": {
        "color": MUTED,
        "fontSize": "11px",
        "fontWeight": "600",
        "letterSpacing": "0.07em",
        "textTransform": "uppercase",
    },
    "value": {
        "color": TEXT,
        "fontSize": "28px",
        "fontWeight": "800",
        "lineHeight": "1.1",
        "margin": "4px 0 2px",
    },
    "sub": {"color": MUTED, "fontSize": "12px"},
    "section_title": {
        "color": LSU_PURPLE,
        "fontSize": "15px",
        "fontWeight": "800",
        "letterSpacing": "-0.02em",
        "marginBottom": "4px",
    },
    "section_sub": {"color": MUTED, "fontSize": "12px", "marginBottom": "12px"},
    "tab_style": {
        "fontFamily": "Georgia, serif",
        "fontSize": "13px",
        "padding": "10px 16px",
        "color": MUTED,
        "borderTop": "none",
        "borderLeft": "none",
        "borderRight": "none",
        "borderBottom": "3px solid transparent",
        "background": WHITE,
    },
    "tab_selected": {
        "fontFamily": "Georgia, serif",
        "fontSize": "13px",
        "padding": "10px 16px",
        "color": LSU_PURPLE,
        "fontWeight": "700",
        "borderTop": "none",
        "borderLeft": "none",
        "borderRight": "none",
        "borderBottom": f"3px solid {LSU_PURPLE}",
        "background": f"{LSU_PURPLE}12",
    },
}


def stat_card(label, value, sub, color):
    return html.Div([
        html.Div(style={"display": "flex", "justifyContent": "space-between"}, children=[
            html.Span(label, style=S["label"]),
        ]),
        html.Div(str(value), style=S["value"]),
        html.Div(sub, style=S["sub"]),
    ], style=S["stat_card"](color))


def section_header(title, sub=None):
    children = [html.H2(title, style=S["section_title"])]
    if sub:
        children.append(html.P(sub, style=S["section_sub"]))
    return html.Div(children)


# ── Table columns ─────────────────────────────────────────────────────────────
WORKS_COLUMNS = [
    {"name": "#",          "id": "_row",      "type": "numeric"},
    {"name": "Title",      "id": "title",     "type": "text",    "presentation": "markdown"},
    {"name": "Year",       "id": "year",      "type": "numeric"},
    {"name": "Citations",  "id": "citations", "type": "numeric"},
    {"name": "Journal",    "id": "journal",   "type": "text"},
    {"name": "OA",         "id": "oa",        "type": "text"},
    {"name": "Type",       "id": "type",      "type": "text"},
]

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
        "fontSize": "13px",
        "padding": "8px 12px",
        "border": f"1px solid {BORDER}",
        "backgroundColor": WHITE,
        "color": TEXT,
        "textAlign": "left",
        "overflow": "hidden",
        "textOverflow": "ellipsis",
        "maxWidth": "10",
    },
    style_cell_conditional=[
        {"if": {"column_id": "_row"},      "width": "42px",  "textAlign": "center", "color": MUTED},
        {"if": {"column_id": "year"},      "width": "70px",  "textAlign": "center", "color": MUTED},
        {"if": {"column_id": "citations"}, "width": "90px",  "textAlign": "right",  "fontWeight": "700"},
        {"if": {"column_id": "oa"},        "width": "55px",  "textAlign": "center"},
        {"if": {"column_id": "type"},      "width": "90px",  "textAlign": "center", "color": MUTED},
        {"if": {"column_id": "journal"},   "width": "180px"},
        {"if": {"column_id": "title"},     "maxWidth": "380px"},
    ],
    style_data_conditional=[
        {"if": {"row_index": "odd"}, "backgroundColor": "#faf9fc"},
        {"if": {"filter_query": "{oa} = 'Yes'", "column_id": "oa"}, "color": GREEN, "fontWeight": "700"},
    ],
    page_size=20,
    sort_action="native",
    filter_action="native",
    filter_options={"case": "insensitive"},
    markdown_options={"link_target": "_blank"},
)


def make_works_table(data_df, table_id):
    tdf = data_df.copy().reset_index(drop=True)
    tdf["_row"] = tdf.index + 1
    # Make title a markdown link
    tdf["title"] = tdf.apply(
        lambda r: f"[{r['title']}]({r['id']})" if r["id"] else r["title"], axis=1
    )
    return dash_table.DataTable(
        id=table_id,
        columns=WORKS_COLUMNS,
        data=tdf.to_dict("records"),
        **TABLE_STYLE,
    )

RECENT_COLUMNS = [
    {"name": "Work Title",  "id": "title",     "type": "text", "presentation": "markdown"},
    {"name": "Type",        "id": "type",       "type": "text"},
    {"name": "Status",      "id": "oa_status",  "type": "text"},
    {"name": "Date",        "id": "pub_date",   "type": "text"},
    {"name": "APC",         "id": "apc_display","type": "text"},
]

def make_recent_table(data_df, table_id):
    tdf = data_df.copy().sort_values("pub_date", ascending=False).head(50)
    tdf["title"] = tdf.apply(
        lambda r: f"[{r['title']}]({r['id']})" if r["id"] else r["title"], axis=1
    )
    tdf["apc_display"] = tdf["apc_list"].apply(
        lambda v: f"${int(v):,}" if pd.notna(v) else "—"
    )

    OA_BADGE_COLORS = {
        "gold":     {"backgroundColor": "#fef9c3", "color": "#854d0e"},
        "green":    {"backgroundColor": "#dcfce7", "color": "#166534"},
        "hybrid":   {"backgroundColor": "#e0f2fe", "color": "#075985"},
        "bronze":   {"backgroundColor": "#ffedd5", "color": "#9a3412"},
        "closed":   {"backgroundColor": "#f3f4f6", "color": "#374151"},
        "unknown":  {"backgroundColor": "#f3f4f6", "color": "#6b7280"},
    }

    return dash_table.DataTable(
        id=table_id,
        columns=RECENT_COLUMNS,
        data=tdf.to_dict("records"),
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
            "fontSize": "13px",
            "padding": "8px 12px",
            "border": f"1px solid {BORDER}",
            "backgroundColor": WHITE,
            "color": TEXT,
            "textAlign": "left",
            "whiteSpace": "normal",
            "height": "auto",
        },
        style_cell_conditional=[
            {"if": {"column_id": "title"},
             "maxWidth": "500px",
             "whiteSpace": "normal",
             "height": "auto",
             "lineHeight": "1.4",
             "overflow": "hidden",
            },
            {"if": {"column_id": "type"},        "width": "80px",  "textAlign": "center", "color": MUTED},
            {"if": {"column_id": "oa_status"},   "width": "90px",  "textAlign": "center"},
            {"if": {"column_id": "pub_date"},    "width": "110px", "textAlign": "center", "color": MUTED},
            {"if": {"column_id": "apc_display"}, "width": "90px",  "textAlign": "right",  "fontWeight": "700"},
        ],
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "#faf9fc"},
            # OA status badge colors
            *[
                {"if": {"filter_query": f'{{oa_status}} = "{status}"', "column_id": "oa_status"},
                 "backgroundColor": colors["backgroundColor"],
                 "color": colors["color"],
                 "borderRadius": "999px",
                 "fontWeight": "600",
                 "fontSize": "11px"}
                for status, colors in OA_BADGE_COLORS.items()
            ],
        ],
        page_size=25,
        sort_action="native",
        markdown_options={"link_target": "_blank"},
    )

# ── Layout ────────────────────────────────────────────────────────────────────
app = Dash(
    __name__,
    suppress_callback_exceptions=True,
    external_stylesheets=["https://fonts.googleapis.com/css2?family=IM+Fell+English&display=swap"],
)
app.title = "LSU OpenAlex Dashboard"

inst_name    = institution.get("display_name", "Louisiana State University")
inst_works   = institution.get("works_count", 0)
inst_cites   = institution.get("cited_by_count", 0)
inst_img     = institution.get("image_url", "")

app.layout = html.Div(style={"minHeight": "100vh", "background": BG_COLOR, "fontFamily": "Georgia, serif", "color": TEXT}, children=[

    # ── Header ──
    html.Div(style={
        "background": LSU_PURPLE, "padding": "0 28px", "height": "58px",
        "display": "flex", "alignItems": "center", "gap": "14px",
    }, children=[
        html.Div("LSU", style={
            "width": "36px", "height": "36px", "background": LSU_GOLD,
            "borderRadius": "7px", "display": "flex", "alignItems": "center",
            "justifyContent": "center", "fontWeight": "900", "fontSize": "13px",
            "color": LSU_PURPLE, "flexShrink": "0",
        }),
        html.Div([
            html.Div("Louisiana State University", style={"fontWeight": "700", "fontSize": "15px", "color": "white"}),
            html.Div("OpenAlex Research Dashboard", style={"fontSize": "11px", "color": "rgba(255,255,255,0.62)"}),
        ]),
        html.Div(style={"marginLeft": "auto", "display": "flex", "gap": "16px", "alignItems": "center", "fontSize": "11px", "color": "rgba(255,255,255,0.5)"}, children=[
            html.Span(f"ROR: {ROR_ID}"),
            html.Span("|", style={"opacity": ".4"}),
            html.Span(f"ID: {INSTITUTION_ID}"),
            html.Span("● Live", style={"color": LSU_GOLD, "fontWeight": "600"}),
        ]),
    ]),

    # ── Gold bar ──
    html.Div(style={"height": "4px", "background": f"linear-gradient(90deg,{LSU_GOLD},{LSU_GOLD_D})"}),

    # ── Institution strip ──
    html.Div(style={
        "maxWidth": "1240px", "margin": "0 auto", "padding": "16px 28px 0",
    }, children=[
        html.Div(style={
            "display": "flex", "gap": "12px", "alignItems": "center",
            "background": WHITE, "border": f"1px solid {BORDER}",
            "borderRadius": "11px", "padding": "11px 16px",
        }, children=[
            html.Img(src=inst_img, style={"height": "38px", "objectFit": "contain"}) if inst_img else html.Span(),
            html.Div([
                html.Span(inst_name, style={"fontWeight": "700", "fontSize": "14px", "color": LSU_PURPLE}),
                html.Span(" · Baton Rouge, LA", style={"color": MUTED, "fontSize": "12px"}),
            ], style={"flex": "1"}),
            html.Div(style={"display": "flex", "gap": "20px", "fontSize": "12px"}, children=[
                html.Span(["All-time works: ", html.Strong(f"{inst_works:,}", style={"color": TEXT})], style={"color": MUTED}),
                html.Span(["All-time citations: ", html.Strong(f"{inst_cites:,}", style={"color": TEXT})], style={"color": MUTED}),
            ]),
        ]),
    ]),

    # ── Tabs ──
    html.Div(style={"maxWidth": "1240px", "margin": "0 auto", "padding": "0 28px"}, children=[
        html.Div(style={"padding": "14px 0 24px"}, children=[
            html.Label("Filter by Year:", style={"color": MUTED, "fontSize": "12px", "fontWeight": "600", "letterSpacing": "0.07em", "textTransform": "uppercase"}),
            dcc.RangeSlider(
                id="year-slider",
                min=2020,
                max=CURRENT_YEAR,
                step=1,
                value=[2020, CURRENT_YEAR],
                marks={str(y): {"label": str(y), "style": {"color": MUTED, "fontSize": "11px"}}
                       for y in range(2020, CURRENT_YEAR + 1, 2)},
                tooltip={"always_visible": False},
            ),
        ]),
        dcc.Interval(id="load-interval", interval=3000, n_intervals=0),
        dcc.Tabs(id="tabs", value="overview", style={"borderBottom": f"1px solid {BORDER}"}, children=[
            dcc.Tab(label="Overview",          value="overview",        style=S["tab_style"], selected_style=S["tab_selected"]),
            dcc.Tab(label="Recent Publications",      value="publications",    style=S["tab_style"], selected_style=S["tab_selected"]),
            dcc.Tab(label="Top Cited",         value="top-cited",       style=S["tab_style"], selected_style=S["tab_selected"]),
            dcc.Tab(label="Authors & Topics",  value="authors-topics",  style=S["tab_style"], selected_style=S["tab_selected"]),
        ]),
        html.Div(id="tab-content", style={"paddingTop": "22px", "paddingBottom": "60px"}),
    ]),

    # ── Footer ──
    html.Div(style={
        "borderTop": f"3px solid {LSU_GOLD}",
        "background": LSU_PURPLE,
        "padding": "13px 28px",
        "display": "flex",
        "justifyContent": "space-between",
        "alignItems": "center",
    }, children=[
        html.Span("Louisiana State University · OpenAlex Research Dashboard", style={"color": "rgba(255,255,255,0.5)", "fontSize": "12px"}),
        html.Span(["Data via ", html.A("OpenAlex", href="https://openalex.org", target="_blank", style={"color": LSU_GOLD}), " · CC0"], style={"color": "rgba(255,255,255,0.4)", "fontSize": "12px"}),
    ]),
])


# ── Tab callback ──────────────────────────────────────────────────────────────
@app.callback(
    Output("tab-content", "children"),
    Input("tabs", "value"),
    Input("year-slider", "value"),
    Input("load-interval", "n_intervals")
)
def render_tab(tab, year_range, n):
    if not data_ready:
        return html.Div([
            html.H3("Loading data from OpenAlex...", 
                    style={"textAlign": "center", "color": LSU_PURPLE, "marginTop": "100px"}),
            html.P("This may take a few minutes on first load.", 
                   style={"textAlign": "center", "color": MUTED}),
        ])
        
    top_n = 5

    top_publishers = (
        fdf[fdf["publisher"] != ""]
        .groupby("publisher")
        .size()
        .sort_values(ascending=False)
        .head(top_n)
        .index.tolist()
    )

    pub_trend = (
        fdf[fdf["publisher"].isin(top_publishers)]
        .groupby(["year", "publisher"])
        .size()
        .reset_index(name="count")
        .sort_values("year")
    )

    trend = (
        fdf.groupby("year")
        .agg(
            total=("id", "count"),
            oa_all=("oa", lambda x: (x == "Yes").sum()),
            gold=("oa_status", lambda x: (x == "gold").sum()),
            hybrid=("oa_status", lambda x: (x == "hybrid").sum()),
        )
        .reset_index()
        .sort_values("year")
    )

    def f_fig_apc_by_year():
        apc_year = (
            fdf[fdf["apc_list"].notna()]
            .groupby("year")["apc_list"]
            .sum()
            .reset_index(name="apc_sum")
            .sort_values("year")
        )

        apc_year["label"] = apc_year["apc_sum"].apply(
            lambda v: f"${v/1_000_000:.1f}M" if v >= 1_000_000 else f"${v/1_000:.0f}K"
        )

        fig = px.bar(
            apc_year,
            x="year",
            y="apc_sum",
            text="label",
            color_discrete_sequence=["#6aaa5e"],
            labels={"apc_sum": "Estimated APC Sum (USD)", "year": ""},
        )
        fig.update_layout(
            **LAYOUT_BASE,
            title=None,
            height=340,
        )
        fig.update_yaxes(tickprefix="$", tickformat=",", title=None)
        fig.update_traces(marker_line_width=0, showlegend=False, textposition="inside", textfont=dict(color="white"), textangle=0,)
        fig.update_xaxes(dtick=1, tickformat="d", title=None)
        return fig
    
    def f_fig_funders():
        f_funders = (
            fdf.groupby("funder").size().reset_index(name="count")
            .sort_values("count", ascending=False)
            .head(10)
        ) if "funder" in fdf.columns else funders_df.head(10)

        fig = px.bar(
            f_funders.sort_values("count"),
            x="count",
            y="name" if "name" in f_funders.columns else "funder",
            orientation="h",
            color_discrete_sequence=[LSU_PURPLE],
            labels={"count": "", "name": ""},
        )
        layout = {
            **LAYOUT_BASE,
            "yaxis": dict(showgrid=False, linecolor=BORDER, tickfont=dict(color=TEXT, size=11)),
            "xaxis": dict(showgrid=True, gridcolor=BORDER, linecolor=BORDER, tickfont=dict(color=MUTED, size=11)),
            "title": None,
            "height": 340,
        }
        fig.update_layout(**layout)
        fig.update_traces(marker_line_width=0, hovertemplate="%{y}: %{x:,}<extra></extra>",)
        return fig

    def f_fig_pub_trends():
        fig = go.Figure()

        lines = [
            ("total",  "Total",     "#461D7C", "solid"),
            ("oa_all", "OA (All)",  "#16a34a", "solid"),
            ("gold",   "Gold OA",   "#FDD023", "dash"),
            ("hybrid", "Hybrid OA", "#7c3aed", "dot"),
        ]

        for col, name, color, dash_style in lines:
            fig.add_trace(go.Scatter(
                x=trend["year"],
                y=trend[col],
                name=name,
                mode="lines",
                line=dict(color=color, dash=dash_style, width=2.5),
            ))

        fig.update_layout(
            **LAYOUT_BASE,
            title=None,
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                font=dict(size=12),
            ),
            hovermode="x unified",
            height=320,     
        )
        fig.update_xaxes(dtick=1, tickformat="d")
        return fig
    
    def f_fig_publisher_trends():
        fig = go.Figure()
        colors = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
                  "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"]

        for i, publisher in enumerate(top_publishers):
            pdata = pub_trend[pub_trend["publisher"] == publisher]
            fig.add_trace(go.Scatter(
                x=pdata["year"],
                y=pdata["count"],
                name=publisher,
                mode="lines+markers",
                line=dict(color=colors[i % len(colors)], width=2.5),
                marker=dict(size=6),
            ))

        fig.update_layout(
            **LAYOUT_BASE,
            title=None,
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                font=dict(size=11),
            ),
            hovermode="x unified",
            height=350,
            
        )
        
        fig.update_yaxes(rangemode="tozero")
        fig.update_xaxes(dtick=1, tickformat="d")
        
        return fig

    f_total_cit  = int(fdf["citations"].sum())
    f_avg_cit    = round(fdf["citations"].mean(), 1) if len(fdf) else 0
    f_oa_count   = int((fdf["oa"] == "Yes").sum())
    f_oa_pct     = round(f_oa_count / len(fdf) * 100) if len(fdf) else 0
    apc_sum      = int(fdf["apc_list"].sum(skipna=True))
    apc_coverage = fdf["apc_list"].notna().sum()

    f_pubs_year = (
        fdf[fdf["year"].notna()]
        .groupby("year").size().reset_index(name="count")
        .sort_values("year")
    )
    f_top_journals = (
        fdf[fdf["journal"] != ""]
        .groupby("journal").size().reset_index(name="count")
        .sort_values("count", ascending=False)
        .head(12)
    )
    f_top_cited = fdf.sort_values("citations", ascending=False).head(25).copy()
    f_oa_df = (
        fdf.groupby("oa").size().reset_index(name="count")
        .rename(columns={"oa": "name"})
    )
    f_type_df = (
        fdf[fdf["type"] != ""]
        .groupby("type").size().reset_index(name="count")
        .rename(columns={"type": "name"})
        .sort_values("count", ascending=False)
    )

    # ── Filtered figures ──
    def f_fig_pubs_year():
        fig = px.bar(f_pubs_year, x="year", y="count", 
                     color_discrete_sequence=[LSU_PURPLE],
                     labels={"count": "", "journal": ""},
                     text_auto=True)
        fig.update_layout(**LAYOUT_BASE, title=None)
        fig.update_traces(marker_line_width=0)
        fig.update_xaxes(dtick=1, tickformat="d")
        return fig

    def f_fig_journals():
        journals = f_top_journals.copy()
        journals["journal"] = journals["journal"].apply(lambda x: x[:40] + "..." if len(x) > 40 else x)
        
        fig = px.bar(
            journals.sort_values("count"), x="count", y="journal",
            orientation="h", color_discrete_sequence=[LSU_GOLD_D],
            labels={"count": "", "journal": ""},
        )
        layout = {
            **LAYOUT_BASE,
            "yaxis": dict(showgrid=False, linecolor=BORDER, tickfont=dict(color=TEXT, size=11)),
            "xaxis": dict(showgrid=True, gridcolor=BORDER, linecolor=BORDER, tickfont=dict(color=MUTED, size=11)),
            "title": None,
            "height": 340,
        }
        fig.update_layout(**layout)
        fig.update_traces(marker_line_width=0, hovertemplate="%{y}: %{x:,}<extra></extra>",)
        return fig

    def f_fig_oa_pie():
        fig = px.pie(f_oa_df, names="name", values="count",
                    color_discrete_map={
                         "Yes": "#16a34a",   # ← green for Yes
                         "No":  "#a6a6a6",   # ← gray for No
                     },
                     hole=0.4)
        fig.update_layout(font=FONT, paper_bgcolor=WHITE,
                          margin=dict(l=10, r=10, t=10, b=10),
                          legend=dict(font=dict(size=12)))
        fig.update_traces(textinfo="percent+label", textfont_size=12, texttemplate="%{percent:.0%}",)
        return fig

    def f_fig_types():
        fig = px.pie(f_type_df, names="name", values="count",
                     color_discrete_sequence=OA_COLORS, hole=0.4)
        fig.update_layout(font=FONT, paper_bgcolor=WHITE,
                          margin=dict(l=10, r=10, t=10, b=10),
                          legend=dict(font=dict(size=12)))
        fig.update_traces(textinfo="none")
        return fig

    def f_fig_oa_trend():
        fig = px.bar(f_pubs_year.tail(15), x="year", y="count",
                     color_discrete_sequence=[GREEN])
        fig.update_layout(**LAYOUT_BASE, title=None)
        fig.update_traces(marker_line_width=0)
        return fig

    # ══ OVERVIEW ══
    if tab == "overview":
        return html.Div([
            html.Div(style={"display": "flex", "gap": "14px", "marginBottom": "18px"}, children=[
                stat_card("Works",    f"{len(fdf):,}",    f"{year_min}–{year_max}",    LSU_PURPLE),
                stat_card("Total Citations", f"{f_total_cit:,}", "across filtered works",     LSU_GOLD_D),
                stat_card("Avg Citations",   f"{f_avg_cit}",     "per work",                  "#7c3aed"),
                stat_card("Open Access",     f"{f_oa_pct}%",     f"{f_oa_count} OA works",    GREEN),
            ]),
            html.Div(style={"display": "grid", "gridTemplateColumns": "2fr 1fr 1fr", "gap": "16px", "marginBottom": "18px"}, children=[
                html.Div(style=S["card"], children=[
                    section_header("Publications per Year", f"{year_min}–{year_max}"),
                    dcc.Graph(figure=f_fig_pubs_year(), config={"displayModeBar": False}, style={"height": "230px"}),
                ]),
                html.Div(style=S["card"], children=[
                    section_header("Types"),
                    dcc.Graph(figure=f_fig_types(), config={"displayModeBar": False}, style={"height": "230px"}),
                ]),
                html.Div(style=S["card"], children=[
                    section_header("OA Status"),
                    dcc.Graph(figure=f_fig_oa_pie(), config={"displayModeBar": False}, style={"height": "230px"}),
                ]),
            ]),
            html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "16px", "marginBottom": "18px"}, children=[
                html.Div(style=S["card"], children=[
                section_header("Estimated APCs by Year (USD)"),
                dcc.Graph(figure=f_fig_apc_by_year(), config={"displayModeBar": False}, style={"height": "340px"}),
                html.P(
                    ["Reflects data from ", html.A("OpenAPC", href="https://openapc.net", target="_blank", style={"color": LSU_PURPLE}),
                     " and APC list pricing for gold & hybrid OA articles. Actual APC costs may differ due to read & publish agreements, APC waivers, and other factors."],
                    style={"fontSize": "11px", "color": MUTED, "marginTop": "8px", "fontStyle": "italic"},
                ),
                ]),
                html.Div(style=S["card"], children=[
                    section_header("Publication Trends", f"OA breakdown & top {top_n} publishers · {year_min}–{year_max}"),
                    dcc.Tabs(style={"marginBottom": "12px"}, children=[
                        dcc.Tab(label="OA Trends", style=S["tab_style"], selected_style=S["tab_selected"], children=[
                            dcc.Graph(figure=f_fig_pub_trends(), config={"displayModeBar": False}, style={"height": "320px"}),
                        ]),
                        dcc.Tab(label="By Publisher", style=S["tab_style"], selected_style=S["tab_selected"], children=[
                            dcc.Graph(figure=f_fig_publisher_trends(), config={"displayModeBar": False}, style={"height": "350px"}),
                        ]),
                    ]),
                ]),
            ]),
            html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "16px", "marginBottom": "18px"}, children=[
                html.Div(style=S["card"], children=[
                    section_header("Publication by Venue", "Most frequent journals / sources"),
                    dcc.Graph(figure=f_fig_journals(), config={"displayModeBar": False}, style={"height": "340px"}),
                ]),
                html.Div(style=S["card"], children=[
                section_header("Publications by Funding Agencies", "Top 10 funders"),
                dcc.Graph(figure=f_fig_funders(), config={"displayModeBar": False}, style={"height": "340px"}),
                ]),
            ]),
        ])

    # ══ PUBLICATIONS ══
    elif tab == "publications":
        return html.Div([
            section_header("Recent Publications", f"{len(fdf):,} works · {year_min}–{year_max} · use column filters to search"),
            html.Div(style=S["card"], children=[
                make_recent_table(fdf, "works-table"),
            ]),
        ])

    # ══ TOP CITED ══
    elif tab == "top-cited":
        return html.Div([
            section_header("Top 25 Most Cited Works", f"Sorted by citation count · {year_min}–{year_max}"),
            html.Div(style=S["card"], children=[
                make_works_table(f_top_cited, "cited-table"),
            ]),
        ])

    # ══ AUTHORS & TOPICS ══
    elif tab == "authors-topics":
        # Build filtered authors from fdf
        author_counts = {}
        for _, row in fdf.iterrows():
            for author in str(row["authors"]).split("|"):
                if author:
                    author_counts[author] = author_counts.get(author, 0) + 1
        f_authors_df = (
            pd.DataFrame(list(author_counts.items()), columns=["name", "count"])
            .sort_values("count", ascending=False)
            .head(10)
        )

        # Build filtered topics from fdf
        f_topics_df = (
            fdf[fdf["topic"] != ""]
            .groupby("topic").size().reset_index(name="count")
            .rename(columns={"topic": "name"})
            .sort_values("count", ascending=False)
            .head(10)
        )
        
        def group_table(data, table_id):
            tdata = [{"#": i+1, "Name": r["name"], "Works": f"{r['count']:,}"} for i, r in data.iterrows()]
            return dash_table.DataTable(
                id=table_id,
                columns=[
                    {"name": "#",     "id": "#",     "type": "numeric"},
                    {"name": "Name",  "id": "Name",  "type": "text"},
                    {"name": "Works", "id": "Works", "type": "text"},
                ],
                data=tdata,
                style_table={"borderRadius": "10px", "border": f"1px solid {BORDER}"},
                style_header={
                    "backgroundColor": SURFACE, "color": LSU_PURPLE,
                    "fontWeight": "700", "fontSize": "11px", "textTransform": "uppercase",
                    "letterSpacing": "0.06em", "border": f"1px solid {BORDER}",
                    "fontFamily": "Georgia, serif",
                },
                style_cell={
                    "fontFamily": "Georgia, serif", "fontSize": "13px",
                    "padding": "8px 12px", "border": f"1px solid {BORDER}",
                    "backgroundColor": WHITE, "color": TEXT, "textAlign": "left",
                },
                style_cell_conditional=[
                    {"if": {"column_id": "#"},     "width": "44px", "textAlign": "center", "color": MUTED},
                    {"if": {"column_id": "Works"}, "width": "80px", "textAlign": "right",  "fontWeight": "700"},
                ],
                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": "#faf9fc"},
                ],
                page_size=25,
                sort_action="native",
            )

        return html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "18px"}, children=[
            html.Div([
                section_header("Top Authors", f"By publication count · {year_min}–{year_max}"),
                html.Div(style=S["card"], children=[group_table(f_authors_df, "authors-table")]),
            ]),
            html.Div([
                section_header("Top Research Topics", f"Primary topics · {year_min}–{year_max}"),
                html.Div(style=S["card"], children=[group_table(f_topics_df, "topics-table")]),
            ]),
        ])

    return html.Div("Select a tab.")


# ── Run ───────────────────────────────────────────────────────────────────────
port = int(os.environ.get("PORT", 8050))

if __name__ == "__main__":
    threading.Thread(target=load_all_data, daemon=True).start()
    app.run(debug=False, host="0.0.0.0", port=port)
