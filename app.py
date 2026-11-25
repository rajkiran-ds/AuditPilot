"""
app.py

SEO + AEO Website Intelligence Agent: Streamlit front end.

Run with:  streamlit run app.py

app.py only orchestrates the engine modules and renders their output. Scoring,
recommendation, PPT, chat and email logic all live in their own modules.

Integration notes (the modules are the source of truth):
    * crawler.py exposes ``Crawler`` (robots.txt + sitemap discovery, concurrent
      crawl, FeatureEngine extraction) returning a ``CrawlResult``. The sample-site
      fallback is passed as ``fallback_urls``. ``CrawlResult`` is kept in
      ``st.session_state.crawled_pages`` because SEOEngine and AEOEngine need the
      robots and sitemap reports as well as the pages.
    * SEOEngine.audit_site(crawl_result) and
      AEOEngine.audit_site(crawl_result, seo_page_results) return
      ``(dict[url, Result], site_score)``. They are stored as
      ``{"results": ..., "overall_score": ..., <display summaries>}``.
    * AEO is rule-based (no LLM). The optional LLM key in the sidebar is the Groq
      key used by ChatAgent, which builds its retrieval index from the audit.
    * The Email tab calls ``EmailAgent.create_email`` when the agent provides it.
      Otherwise it renders the same executive body with the agent's own body
      builder. Nothing is sent from this app.
    * SEOResult/AEOResult evidence carries ``points_deducted`` but no severity.
      Severity badges are derived from points deducted (see ``_severity``).
"""

from __future__ import annotations

import os
import uuid
import requests
from collections import Counter
from collections.abc import Mapping
from html import escape
from models import AuditResult
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import email_agent as email_module
from aeo import AEOEngine, CRITERIA_LABELS
from chat_agent import ChatAgent
from crawler import SAMPLE_SITE_URLS, Crawler
from email_agent import EmailAgent
from ppt import PowerPointGenerator
from recommendation_engine import RecommendationEngine
from seo import SEOEngine

# --------------------------------------------------------------------------
# Page config & styling
# --------------------------------------------------------------------------
st.set_page_config(
    page_title="SEO + AEO Website Intelligence Agent",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_CSS = """
<style>
    .main .block-container {padding-top: 2rem; max-width: 1200px;}
    div[data-testid="stMetric"] {
        background: #FFFFFF;
        border: 1px solid #E5E7EB;
        border-radius: 12px;
        padding: 14px 16px 8px 16px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    }
    .app-title {font-size: 2rem; font-weight: 800; margin-bottom: 0;}
    .app-subtitle {color: #6B7280; font-size: 1rem; margin-top: 0.2rem;}
    .badge {
        display: inline-block; padding: 2px 10px; border-radius: 999px;
        font-size: 0.75rem; font-weight: 700; color: white; margin-right: 6px;
    }
    .badge-critical {background:#DC2626;}
    .badge-high {background:#EA580C;}
    .badge-medium {background:#D97706;}
    .badge-low {background:#65A30D;}
    section[data-testid="stSidebar"] {background-color: #F9FAFB;}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

SEV_COLOR_MAP = {"Critical": "#DC2626", "High": "#EA580C", "Medium": "#D97706", "Low": "#65A30D"}
SEVERITY_ORDER = ("Critical", "High", "Medium", "Low")
SITE_LEVEL_FACTORS = frozenset({"Sitemap", "Robots.txt"})
GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
CHAT_BASE_URL = getattr(email_module, "CHAT_BASE_URL", "https://company.local/chat")

# NEW: FastAPI Backend URL
API_URL = os.getenv("API_URL", "http://localhost:8080")

HORIZONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("short_term", "🔴 SHORT TERM", ("short_term", "short", "short_term_fixes")),
    ("medium_term", "🟠 MEDIUM TERM", ("medium_term", "mid_term", "medium")),
    ("long_term", "🔵 LONG TERM", ("long_term", "long")),
)

# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------
SESSION_DEFAULTS: dict[str, Any] = {
    # NEW: Auth state
    "token": None,
    "username": None,
    
    # Existing state
    "crawled_pages": None,
    "seo_results": None,
    "aeo_results": None,
    "recommendations": None,
    "site_url": "",
    "sitemap_status": "",
    "used_sample": False,
    "ppt_path": None,
    "chat_history": [],
    "email_draft": "",
}
for _key, _default in SESSION_DEFAULTS.items():
    if _key not in st.session_state:
        st.session_state[_key] = list(_default) if isinstance(_default, list) else _default


# --------------------------------------------------------------------------
# NEW: Authentication Page
# --------------------------------------------------------------------------
def render_auth_page():
    st.markdown("<h2 style='text-align: center; margin-top: 50px;'>🔐 Welcome to SEO + AEO Intelligence</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: gray;'>Please log in or create an account to continue.</p>", unsafe_allow_html=True)
    
    # Center the form
    col1, col2, col3 = st.columns([1, 1.5, 1])
    
    with col2:
        tab1, tab2 = st.tabs(["🔒 Login", "✨ Sign Up"])
        
        with tab1:
            with st.form("login_form"):
                username = st.text_input("Username")
                password = st.text_input("Password", type="password")
                submit_login = st.form_submit_button("Log In", use_container_width=True, type="primary")
                
                if submit_login:
                    if not username or not password:
                        st.error("Please enter both username and password.")
                    else:
                        try:
                            res = requests.post(
                                f"{API_URL}/auth/login", 
                                data={"username": username, "password": password}
                            )
                            if res.status_code == 200:
                                st.session_state.token = res.json()["access_token"]
                                st.session_state.username = username
                                st.rerun()
                            else:
                                st.error("Invalid username or password.")
                        except requests.exceptions.ConnectionError:
                            st.error("Cannot connect to the backend server. Is FastAPI running?")
                            
        with tab2:
            with st.form("signup_form"):
                new_user = st.text_input("Username")
                new_email = st.text_input("Email")
                new_pass = st.text_input("Password", type="password")
                submit_signup = st.form_submit_button("Create Account", use_container_width=True, type="primary")
                
                if submit_signup:
                    if not new_user or not new_email or not new_pass:
                        st.error("Please fill in all fields.")
                    else:
                        try:
                            res = requests.post(
                                f"{API_URL}/auth/signup", 
                                json={"username": new_user, "email": new_email, "password": new_pass}
                            )
                            if res.status_code == 201:
                                st.success("Account created successfully! You can now log in.")
                            else:
                                error_msg = res.json().get("detail", "Signup failed.")
                                st.error(error_msg)
                        except requests.exceptions.ConnectionError:
                            st.error("Cannot connect to the backend server. Is FastAPI running?")

# --------------------------------------------------------------------------
# GATEKEEPER: Force login before app loads
# --------------------------------------------------------------------------
if not st.session_state.token:
    render_auth_page()
    st.stop()  # Stops the rest of the app from loading


# --------------------------------------------------------------------------
# Generic helpers (defensive data access only)
# --------------------------------------------------------------------------
def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first non-None attribute or key among ``names``."""
    if obj is None:
        return default
    for name in names:
        value = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _count(value: Any) -> int:
    """Interpret a value as a count: a number, or the length of a collection."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, (str, bytes)):
        return _to_int(value)
    try:
        return len(value)
    except TypeError:
        return 0


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _severity(points: Any) -> str:
    """Map marks deducted to a severity label for display."""
    deducted = _to_float(points)
    if deducted >= 10:
        return "Critical"
    if deducted >= 6:
        return "High"
    if deducted >= 3:
        return "Medium"
    return "Low"


def _badge(label: str) -> str:
    return f'<span class="badge badge-{escape(label.lower())}">{escape(label.upper())}</span>'


def _priority_label(priority: Any) -> str:
    label = _text(priority).title()
    return label if label in SEVERITY_ORDER else "Medium"


# --------------------------------------------------------------------------
# PageData field helpers
# --------------------------------------------------------------------------
def _status_code(page: Any) -> int:
    return _to_int(_get(page, "status_code", "status"))


def _is_indexed(page: Any) -> bool:
    robots = _text(_get(page, "robots_meta", "robots")).lower()
    return _status_code(page) == 200 and "noindex" not in robots


def _title_length(page: Any) -> int:
    direct = _get(page, "title_length")
    if direct is not None:
        return _count(direct)
    return len(_text(_get(page, "title")))


def _h1_count(page: Any) -> int:
    direct = _get(page, "h1_count")
    if direct is not None:
        return _count(direct)
    headings = _get(page, "headings", "heading_structure", "heading_hierarchy")
    if isinstance(headings, Mapping):
        for key, value in headings.items():
            if str(key).lower() in ("h1", "1"):
                return _count(value)
    h1 = _get(page, "h1_texts", "h1")
    if isinstance(h1, str):
        return 1 if h1.strip() else 0
    return _count(h1)


def _missing_alt(page: Any) -> int:
    direct = _get(page, "images_missing_alt", "missing_alt_count", "images_without_alt")
    if direct is not None:
        return _count(direct)
    images = _get(page, "images")
    if isinstance(images, (list, tuple)):
        return sum(1 for image in images if not _text(_get(image, "alt", "alt_text")))
    return 0


# --------------------------------------------------------------------------
# Result summaries (display and agent-facing views of engine output)
# --------------------------------------------------------------------------
def _build_seo_bundle(crawl_result: Any, results: dict, overall: float) -> dict[str, Any]:
    """Wrap SEOEngine output with the summaries the UI and chat/email agents read."""
    pages = list(crawl_result.pages)
    severity_counts: Counter[str] = Counter({label: 0 for label in SEVERITY_ORDER})
    all_issues: list[dict[str, Any]] = []
    page_audits: list[dict[str, Any]] = []
    seen_site_level: set[tuple[str, str]] = set()

    for url, result in results.items():
        page_issues: list[dict[str, Any]] = []
        for entry in _get(result, "evidence", default=[]) or []:
            factor = _text(_get(entry, "factor"))
            issue_name = _text(_get(entry, "issue"))
            if factor in SITE_LEVEL_FACTORS:
                key = (factor, issue_name)
                if key in seen_site_level:
                    continue
                seen_site_level.add(key)
            severity = _severity(_get(entry, "points_deducted"))
            record = {
                "issue": issue_name,
                "severity": severity,
                "detail": _text(_get(entry, "evidence")),
                "factor": factor,
                "url": "Site-wide" if factor in SITE_LEVEL_FACTORS else url,
            }
            severity_counts[severity] += 1
            all_issues.append(record)
            page_issues.append({**record, "severity": severity.lower()})
        page_audits.append(
            {
                "url": url,
                "score": _to_float(_get(result, "score")),
                "grade": _text(_get(result, "grade")),
                "issues": page_issues,
            }
        )

    top_problem_pages = sorted(page_audits, key=lambda audit: audit["score"])[:10]
    return {
        "results": results,
        "overall_score": overall,
        "total_pages": len(pages),
        "indexed_pages": sum(1 for page in pages if _is_indexed(page)),
        "severity_counts": dict(severity_counts),
        "critical_issue_count": severity_counts["Critical"],
        "all_issues": all_issues,
        "page_audits": page_audits,
        "top_problem_pages": top_problem_pages,
    }


def _build_aeo_bundle(results: dict, overall: float) -> dict[str, Any]:
    """Wrap AEOEngine output with criteria averages (percent of 10 marks)."""
    averages: dict[str, float] = {}
    for key in CRITERIA_LABELS:
        values = [
            _to_float((_get(result, "factor_scores", default={}) or {}).get(key))
            for result in results.values()
        ]
        averages[key] = round(sum(values) / len(values) * 10, 1) if values else 0.0
    return {"results": results, "overall_score": overall, "criteria_averages": averages}


def _horizon_items(recommendations: Any, aliases: tuple[str, ...]) -> list[Any]:
    raw = _get(recommendations, *aliases, default=[])
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw] if raw else []


def _affected_count(item: Any) -> int:
    urls = _get(item, "affected_urls", "affected_pages", "urls")
    if urls is not None:
        return _count(urls)
    return _count(_get(item, "affected_count", "affected_url_count", "affected"))


def _rec_dict(item: Any) -> dict[str, Any]:
    return {
        "title": _text(_get(item, "title", "name", "recommendation", default="Recommendation")),
        "description": _text(_get(item, "description", "detail", "action")),
        "priority": _priority_label(_get(item, "priority", "severity")),
        "rationale": _text(_get(item, "rationale", "reason", "why")),
        "affected_urls": _affected_count(item),
    }


def _recs_for_agents(recommendations: Any) -> dict[str, Any]:
    """Normalise RecommendationEngine output to the dict shape chat/email expect."""
    payload: dict[str, Any] = {}
    for key, _label, aliases in HORIZONS:
        payload[key] = [_rec_dict(item) for item in _horizon_items(recommendations, aliases)]
    payload["mid_term"] = payload["medium_term"]
    summary = _get(recommendations, "executive_summary", "summary")
    if summary:
        payload["executive_summary"] = summary
    return payload


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------
seo_engine = SEOEngine()
aeo_engine = AEOEngine()
recommendation_engine = RecommendationEngine()
ppt_generator = PowerPointGenerator()
email_agent = EmailAgent()


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🧭 Website Intelligence Agent")
    st.caption("Rule-based SEO + AEO audit with executive recommendations, PPT export and AI chat.")
    st.markdown("---")

    website_url = st.text_input("Website URL", placeholder="https://example.com")
    max_pages = st.slider("Pages to crawl (demo cap)", min_value=5, max_value=20, value=20, step=5)

    with st.expander("⚙️ LLM settings (AI chat)"):
        api_key_input = st.text_input(
            "LLM API Key (Groq, optional)",
            type="password",
            help="Used only by the Chat tab. Leave blank for deterministic answers from the audit.",
        )
        model_name = st.selectbox("Model", GROQ_MODELS, index=0)
        effective_key = api_key_input or os.environ.get("GROQ_API_KEY", "")
        if effective_key:
            st.success("LLM-grounded chat enabled.")
        else:
            st.info("No API key set. Chat will use deterministic audit answers.")

    run_clicked = st.button("🚀 Run Audit", type="primary", use_container_width=True)
    st.markdown("---")
    st.caption(
        "Tip: if no sitemap.xml is found, the app automatically falls back "
        "to a sample demo site so you can still see a full report."
    )
    
    # NEW: Logout Section
    st.markdown("---")
    st.markdown(f"👤 **Logged in as:** `{st.session_state.username}`")
    if st.button("🚪 Logout", use_container_width=True):
        st.session_state.token = None
        st.session_state.username = None
        st.rerun()

chat_agent = ChatAgent(api_key=effective_key, model=model_name)


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
st.markdown('<p class="app-title">SEO + AEO Website Intelligence Agent</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="app-subtitle">Sitemap discovery → crawl → SEO audit → AEO audit → '
    "recommendations → executive PowerPoint</p>",
    unsafe_allow_html=True,
)
st.write("")


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------
def run_pipeline(url: str, pages_cap: int) -> None:
    """Crawl, score, and generate recommendations; store everything in session state."""
    with st.status("Running audit pipeline...", expanded=True) as status:
        try:
            st.write("🔎 Discovering sitemap, parsing robots.txt and crawling pages...")
            progress_bar = st.progress(0.0, text="Preparing crawl...")

            def _on_progress(done: int, total: int) -> None:
                progress_bar.progress(done / max(total, 1), text=f"Crawled {done}/{total} pages")

            crawler = Crawler(max_pages=pages_cap)
            crawl_result = crawler.crawl(
                url, fallback_urls=SAMPLE_SITE_URLS, progress_callback=_on_progress
            )

            sitemap_report = crawl_result.sitemap_report
            used_sample = not sitemap_report.exists
            if used_sample:
                st.write("⚠️ No sitemap found (checked `/sitemap.xml` and `robots.txt`).")
                st.write("Falling back to the sample demo site so you can preview the full report.")
                sitemap_status = "No sitemap found — using sample fallback site."
            else:
                st.write(f"✅ Sitemap found with **{sitemap_report.total_urls}** URLs.")
                sitemap_status = f"Sitemap found ({sitemap_report.total_urls} URLs)"
            st.write(f"✅ Crawled **{len(crawl_result.pages)}** pages.")

            st.write("📈 Running SEO audit...")
            seo_page_results, seo_score = seo_engine.audit_site(crawl_result)
            seo_results = _build_seo_bundle(crawl_result, seo_page_results, seo_score)

            st.write("🤖 Running AEO audit...")
            aeo_page_results, aeo_score = aeo_engine.audit_site(crawl_result, seo_page_results)
            aeo_results = _build_aeo_bundle(aeo_page_results, aeo_score)

            st.write("🎯 Generating recommendations...")
            recommendations = recommendation_engine.generate_recommendations(
                seo_results["results"], aeo_results["results"]
            )
            status.update(label="✅ Audit pipeline complete", state="complete", expanded=False)
        except Exception as exc:  # surfaced to the user; the pipeline is not resumable
            status.update(label="Audit pipeline failed", state="error")
            st.error(f"Audit failed: {exc}")
            return

    st.session_state.crawled_pages = crawl_result
    st.session_state.seo_results = seo_results
    st.session_state.aeo_results = aeo_results
    st.session_state.recommendations = recommendations
    st.session_state.site_url = url
    st.session_state.sitemap_status = sitemap_status
    st.session_state.used_sample = used_sample
    st.session_state.ppt_path = None
    st.session_state.chat_history = []
    st.session_state.email_draft = ""


if run_clicked:
    if not website_url.strip():
        st.error("Please enter a website URL.")
    else:
        run_pipeline(website_url.strip(), max_pages)

if st.session_state.seo_results is None:
    st.info("👈 Enter a website URL in the sidebar and click **Run Audit** to get started.")
    st.markdown("#### What this agent does")
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown("**1. Discover**\n\nFinds `robots.txt` and `sitemap.xml` (or falls back to a sample site).")
    c2.markdown("**2. Crawl**\n\nFetches pages concurrently and extracts on-page and AEO signals.")
    c3.markdown("**3. Audit**\n\nScores every page for SEO and AI answer-engine (AEO) readiness with evidence.")
    c4.markdown("**4. Report**\n\nBuilds recommendations, an executive PowerPoint, an email and an AI chat.")
    st.stop()


# --------------------------------------------------------------------------
# Results are available from here on
# --------------------------------------------------------------------------
crawl_result = st.session_state.crawled_pages
seo_results = st.session_state.seo_results
aeo_results = st.session_state.aeo_results
recommendations = st.session_state.recommendations
site_url = st.session_state.site_url
pages = list(crawl_result.pages)

if st.session_state.used_sample:
    st.warning(st.session_state.sitemap_status)
else:
    st.success(st.session_state.sitemap_status)

(tab_dashboard, tab_seo, tab_aeo, tab_recs, tab_chat, tab_email, tab_ppt) = st.tabs(
    [
        "📊 Dashboard",
        "🔍 SEO Audit",
        "🤖 AEO Audit",
        "🎯 Recommendations",
        "💬 Chat",
        "✉️ Email",
        "📑 Export PPT",
    ]
)

# ---------------------------------------------------------------- Dashboard
with tab_dashboard:
    st.subheader("Executive Dashboard")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Overall SEO Score", f"{seo_results['overall_score']}/100")
    m2.metric("Overall AEO Score", f"{aeo_results['overall_score']}/100")
    m3.metric("Indexed Pages", f"{seo_results['indexed_pages']}/{seo_results['total_pages']}")
    m4.metric("Critical Issues", seo_results["critical_issue_count"])

    st.write("")
    g1, g2 = st.columns(2)

    with g1:
        gauge_steps = [
            {"range": [0, 50], "color": "#FEE2E2"},
            {"range": [50, 75], "color": "#FEF3C7"},
            {"range": [75, 100], "color": "#DCFCE7"},
        ]
        gauge_fig = go.Figure()
        gauge_fig.add_trace(
            go.Indicator(
                mode="gauge+number",
                value=seo_results["overall_score"],
                title={"text": "SEO Score"},
                domain={"x": [0, 0.48], "y": [0, 1]},
                gauge={"axis": {"range": [0, 100]}, "bar": {"color": "#4F46E5"}, "steps": gauge_steps},
            )
        )
        gauge_fig.add_trace(
            go.Indicator(
                mode="gauge+number",
                value=aeo_results["overall_score"],
                title={"text": "AEO Score"},
                domain={"x": [0.52, 1], "y": [0, 1]},
                gauge={"axis": {"range": [0, 100]}, "bar": {"color": "#0EA5E9"}, "steps": gauge_steps},
            )
        )
        gauge_fig.update_layout(height=280, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(gauge_fig, use_container_width=True)

    with g2:
        severity_counts = seo_results["severity_counts"]
        bar_fig = go.Figure(
            go.Bar(
                x=list(SEVERITY_ORDER),
                y=[severity_counts.get(label, 0) for label in SEVERITY_ORDER],
                marker_color=[SEV_COLOR_MAP[label] for label in SEVERITY_ORDER],
                text=[severity_counts.get(label, 0) for label in SEVERITY_ORDER],
                textposition="outside",
            )
        )
        bar_fig.update_layout(
            title="Issues by Severity",
            height=280,
            margin=dict(l=10, r=10, t=40, b=10),
            yaxis_title="Issue count",
        )
        st.plotly_chart(bar_fig, use_container_width=True)

    st.write("")
    st.markdown("##### Top 10 Problem Pages")
    problem_df = pd.DataFrame(
        [
            {
                "URL": audit["url"],
                "SEO Score": audit["score"],
                "Grade": audit["grade"],
            }
            for audit in seo_results["top_problem_pages"]
        ]
    )
    if problem_df.empty:
        st.info("No pages were audited.")
    else:
        problem_bar = go.Figure(
            go.Bar(
                x=problem_df["SEO Score"],
                y=problem_df["URL"],
                orientation="h",
                marker_color="#4F46E5",
                text=problem_df["SEO Score"],
                textposition="outside",
            )
        )
        problem_bar.update_layout(
            height=420,
            margin=dict(l=10, r=40, t=10, b=10),
            xaxis_title="SEO Score",
            yaxis=dict(autorange="reversed"),
        )
        st.plotly_chart(problem_bar, use_container_width=True)
        st.dataframe(problem_df, use_container_width=True, hide_index=True)

# --------------------------------------------------------------------- SEO
with tab_seo:
    st.subheader("SEO Audit — Page by Page")
    seo_rows: list[dict[str, Any]] = []
    for page in pages:
        url = _text(_get(page, "url"))
        result = seo_results["results"].get(url)
        seo_rows.append(
            {
                "URL": url,
                "Status": _status_code(page),
                "SEO Score": _to_float(_get(result, "score")),
                "Grade": _text(_get(result, "grade")),
                "Title Length": _title_length(page),
                "Meta Description": "✅" if _text(_get(page, "meta_description")) else "❌",
                "H1 Count": _h1_count(page),
                "Word Count": _count(_get(page, "word_count")),
                "Internal Links": _count(_get(page, "internal_links", "internal_link_count")),
                "Canonical": "✅" if _text(_get(page, "canonical", "canonical_url")) else "❌",
                "Missing ALT": _missing_alt(page),
            }
        )
    st.dataframe(pd.DataFrame(seo_rows), use_container_width=True, hide_index=True)

    st.markdown("##### All Issues Found")
    if not seo_results["all_issues"]:
        st.success("No SEO issues found.")
    for issue in sorted(
        seo_results["all_issues"], key=lambda item: SEVERITY_ORDER.index(item["severity"])
    ):
        st.markdown(
            f'{_badge(issue["severity"])} **{escape(issue["issue"])}** — {escape(issue["detail"])}'
            f'  \n<small>{escape(issue["url"])}</small>',
            unsafe_allow_html=True,
        )

# --------------------------------------------------------------------- AEO
with tab_aeo:
    st.subheader("AEO Audit — AI Answer-Engine Readiness")
    st.caption("Rule-based evaluation. Final AEO = 0.4 × SEO score + semantic score (60 marks).")

    criteria_avgs = aeo_results["criteria_averages"]
    categories = [CRITERIA_LABELS[key] for key in criteria_avgs]
    values = list(criteria_avgs.values())
    if values:
        radar_fig = go.Figure(
            go.Scatterpolar(
                r=values + [values[0]],
                theta=categories + [categories[0]],
                fill="toself",
                name="Site average",
                line_color="#0EA5E9",
            )
        )
        radar_fig.update_layout(
            polar=dict(radialaxis=dict(visible=True, range=[0, 100])),
            showlegend=False,
            height=420,
            margin=dict(l=40, r=40, t=20, b=20),
        )
        st.plotly_chart(radar_fig, use_container_width=True)

    st.markdown("##### Criteria Averages")
    st.dataframe(
        pd.DataFrame(
            {"Criterion": categories, "Average (% of max)": values},
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("##### Page-Level AEO Scores")

    aeo_rows: list[dict[str, Any]] = []

    for url, result in aeo_results["results"].items():

        # Get SEO score from SEO results
        seo_score = _to_float(_get(seo_results["results"][url], "score"))
        seo_score = _to_float(_get(seo_results["results"][url], "score"))

        # Semantic score = sum of the 6 AEO factors (out of 60)
        factor_scores = _get(result, "factor_scores", default={}) or {}
        semantic_score = sum(_to_float(v) for v in factor_scores.values())
        row: dict[str, Any] = {
            "URL": url,
            "SEO Score": round(seo_score, 1),
            "Semantic Score": round(semantic_score, 1),
            "Final AEO Score": _to_float(_get(result, "score")),
            "Grade": _text(_get(result, "grade")),
        }

        for key, label in CRITERIA_LABELS.items():
            row[label] = _to_float(factor_scores.get(key))

        aeo_rows.append(row)
    st.dataframe(
    pd.DataFrame(aeo_rows),
    use_container_width=True,
    hide_index=True,
)


    aeo_issues: list[dict[str, str]] = []
    for url, result in aeo_results["results"].items():
        for entry in _get(result, "evidence", default=[]) or []:
            aeo_issues.append(
                {
                    "issue": _text(_get(entry, "issue")),
                    "detail": _text(_get(entry, "evidence")),
                    "severity": _severity(_get(entry, "points_deducted")),
                    "url": url,
                }
            )
    with st.expander(f"AEO issues & evidence ({len(aeo_issues)})"):
        if not aeo_issues:
            st.success("No AEO issues found.")
        for issue in sorted(aeo_issues, key=lambda item: SEVERITY_ORDER.index(item["severity"])):
            st.markdown(
                f'{_badge(issue["severity"])} **{escape(issue["issue"])}** — {escape(issue["detail"])}'
                f'  \n<small>{escape(issue["url"])}</small>',
                unsafe_allow_html=True,
            )

# ---------------------------------------------------------- Recommendations
with tab_recs:
    st.subheader("Recommendations")
    columns = st.columns(3)
    for column, (_key, heading, aliases) in zip(columns, HORIZONS):
        with column:
            st.markdown(f"#### {heading}")
            items = _horizon_items(recommendations, aliases)
            if not items:
                st.caption("No recommendations for this horizon.")
            for position, item in enumerate(items):
                rec = _rec_dict(item)
                with st.container(border=True):
                    st.markdown(
                        f'{_badge(rec["priority"])} **{escape(rec["title"])}**',
                        unsafe_allow_html=True,
                    )
                    if rec["description"]:
                        st.write(rec["description"])
                    with st.expander("Rationale"):
                        st.write(rec["rationale"] or "No rationale provided.")
                    st.caption(f"Affected URLs: {rec['affected_urls']}")

# --------------------------------------------------------------------- Chat
with tab_chat:
    st.subheader("Chat with the Audit")
    st.caption("Answers are grounded only in the generated audit.")

    header_left, header_right = st.columns([5, 1])
    with header_right:
        if st.button("Clear chat", use_container_width=True):
            st.session_state.chat_history = []

    question = st.chat_input("Ask about scores, issues, pages or recommendations...")
    if question and question.strip():
        st.session_state.chat_history.append({"role": "user", "content": question.strip()})
        with st.spinner("Searching the audit..."):
            try:
                answer = chat_agent.ask(
                    question.strip(),
                    seo_results,
                    aeo_results,
                    _recs_for_agents(recommendations),
                )
            except Exception as exc:
                answer = f"The chat agent could not answer this question: {exc}"
        st.session_state.chat_history.append({"role": "assistant", "content": answer})

    if not st.session_state.chat_history:
        st.info("Try: “Which pages need immediate attention?” or “Summarize the recommendations.”")
    for message in st.session_state.chat_history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

# -------------------------------------------------------------------- Email
with tab_email:
    st.subheader("Executive Email")
    st.caption("Generates the email text only. Nothing is sent from this app.")

    recipient = st.text_input("Recipient Email", placeholder="stakeholder@company.com")
    subject = st.text_input(
        "Subject", value=getattr(EmailAgent, "SUBJECT", "Website Intelligence Audit Completed")
    )

    if st.button("Generate Executive Email", type="primary"):
        if "@" not in recipient:
            st.error("Please enter a valid recipient email address.")
        else:
            ppt_path = st.session_state.ppt_path
            try:
                create_email = getattr(email_agent, "create_email", None)
                if callable(create_email):
                    draft = str(
                        create_email(
                            recipient.strip(),
                            subject,
                            site_url,
                            seo_results,
                            aeo_results,
                            recommendations,
                            ppt_path,
                        )
                    )
                else:
                    audit_payload = {
                        "seo_score": seo_results["overall_score"],
                        "aeo_score": aeo_results["overall_score"],
                        "pages_crawled": seo_results["total_pages"],
                        "critical_issues": [
                            f'{issue["issue"]} ({issue["url"]})'
                            for issue in seo_results["all_issues"]
                            if issue["severity"] == "Critical"
                        ],
                        "top_problem_pages": seo_results["top_problem_pages"],
                        "recommendations": _recs_for_agents(recommendations),
                        "aeo_factor_averages": aeo_results["criteria_averages"],
                    }
                    chat_link = f"{CHAT_BASE_URL}/{uuid.uuid4()}"
                    body = email_agent._build_body(audit_payload, ppt_path or "", chat_link)
                    draft = f"To: {recipient.strip()}\nSubject: {subject}\n\n{body}"
                st.session_state.email_draft = draft
            except Exception as exc:
                st.error(f"Email generation failed: {exc}")

    if st.session_state.email_draft:
        st.text_area("Generated email", value=st.session_state.email_draft, height=460)
    if not st.session_state.ppt_path:
        st.caption("Tip: generate the PowerPoint in the Export PPT tab first so the email can reference it.")

# ---------------------------------------------------------------- Export PPT
with tab_ppt:
    st.subheader("Export Executive PowerPoint")
    st.write("Generates an executive deck with scores, evidence-backed findings and recommendations.")

    if st.button("Generate Executive PowerPoint", type="primary"):
        with st.spinner("Building PowerPoint..."):
            try:
                audit_result = AuditResult(
                    pages=crawl_result.pages,
                    seo_results=seo_results["results"],
                    aeo_results=aeo_results["results"],
                    recommendations=recommendations,
                    overall_seo_score=seo_results["overall_score"],
                )

                st.session_state.ppt_path = ppt_generator.generate(
                audit_result,
                "Executive_AI_Audit.pptx",
               )
                st.success("Executive PowerPoint generated.")
            except Exception as exc:
                st.error(f"PowerPoint generation failed: {exc}")


    ppt_path = st.session_state.ppt_path
    if ppt_path and os.path.isfile(str(ppt_path)):
        with open(str(ppt_path), "rb") as handle:
            st.download_button(
                "⬇️ Download Executive_AI_Audit.pptx",
                data=handle.read(),
                file_name="Executive_AI_Audit.pptx",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )