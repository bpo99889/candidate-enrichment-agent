"""
Candidate Enrichment Agent — Streamlit web app (test version).

BYOK model: the buyer pastes their OWN SalesQL API key and/or ContactOut API
token. Keys live only in this browser session's memory (Streamlit
session_state) — they are never written to disk, never logged, and never
sent anywhere except the vendors' own API endpoints.

Demo mode runs the full workflow on fictional data with zero keys/credits.
"""

import pandas as pd
import streamlit as st

from demo_data import DEMO_CANDIDATES, demo_enrich_list
from enrich import EnrichmentAuthError, EnrichmentCreditError, enrich_list
from workbook import build_workbook

st.set_page_config(page_title="Candidate Enrichment Agent", layout="wide")

ALIASES = {
    "first_name": ["first name", "firstname", "fname", "given name", "first"],
    "last_name": ["last name", "lastname", "lname", "surname", "family name", "last"],
    "company": ["company", "company name", "employer", "organisation", "organization"],
    "title": ["title", "job title", "position", "role"],
    "linkedin_url": ["linkedin url", "linkedin", "linkedin profile", "profile url",
                     "linkedin link", "url"],
    "company_domain": ["company domain", "domain", "website", "company website",
                       "web site", "email domain"],
}


def normalize_header(h: str) -> str:
    return str(h).strip().lower()


def auto_map(columns):
    """Map uploaded headers -> our fields. Returns {field: header_or_None}."""
    norm = {normalize_header(c): c for c in columns}
    mapping = {}
    for field, aliases in ALIASES.items():
        found = None
        for a in aliases:
            if a in norm:
                found = norm[a]
                break
        mapping[field] = found
    return mapping


def to_candidates(df: pd.DataFrame, mapping: dict):
    cands = []
    for _, row in df.iterrows():
        cand = {}
        for field, header in mapping.items():
            cand[field] = "" if header is None else (
                "" if pd.isna(row[header]) else str(row[header]).strip()
            )
        cands.append(cand)
    return cands


st.title("🔍 Candidate Enrichment Agent")
st.caption("Test version — enrich candidate lists with verified contact details, "
           "then download a 3-sheet Excel workbook.")

mode = st.sidebar.radio(
    "Mode",
    ["Demo mode (no keys needed)", "Live mode (your API keys)"],
    help="Demo mode uses fictional data so you can test the full workflow free.",
)

salesql_key = contactout_key = None
if mode.startswith("Live"):
    st.sidebar.subheader("Your API keys (BYOK)")
    st.sidebar.caption("Keys stay in this browser session only — never stored or logged.")
    salesql_key = st.sidebar.text_input(
        "SalesQL API key", type="password",
        help="SalesQL dashboard → Settings → API Access. Needs Professional or Premium plan.",
    ) or None
    contactout_key = st.sidebar.text_input(
        "ContactOut API token", type="password",
        help="ContactOut dashboard → API. Needs a paid plan (Basic $49/mo and up).",
    ) or None
    if not (salesql_key or contactout_key):
        st.sidebar.warning("Paste at least one key to run live enrichment.")
    if st.sidebar.button("Test ContactOut key (free, no credits spent)"):
        if not contactout_key:
            st.sidebar.error("Paste your ContactOut token first.")
        else:
            try:
                from enrich import ContactOutClient
                stats = ContactOutClient(contactout_key).stats()
                st.sidebar.success(f"Key works. Credit info: {stats}")
            except EnrichmentAuthError as e:
                st.sidebar.error(str(e))
            except Exception as e:
                st.sidebar.error(f"Key check failed: {e}")
else:
    st.info("👆 You are in **Demo mode** — fictional candidates, no keys, no credits, "
            "no network calls to data vendors.")

st.header("Step 1 — Load candidates")

candidates = []
if mode.startswith("Demo"):
    if st.button("Load 5 demo candidates"):
        st.session_state["candidates"] = DEMO_CANDIDATES
        st.success("Demo candidates loaded.")
    candidates = st.session_state.get("candidates", [])
else:
    uploaded = st.file_uploader("Upload candidate CSV or Excel", type=["csv", "xlsx", "xls"])
    if uploaded is not None:
        try:
            df = pd.read_csv(uploaded) if uploaded.name.lower().endswith(".csv") \
                else pd.read_excel(uploaded)
        except Exception as e:
            st.error(f"Could not read file: {e}")
            df = None
        if df is not None and not df.empty:
            st.subheader("Column mapping")
            st.caption("Check the app matched your columns correctly; fix any mistakes.")
            mapping = auto_map(list(df.columns))
            cols = st.columns(3)
            fields = list(ALIASES.keys())
            for i, field in enumerate(fields):
                with cols[i % 3]:
                    options = [None] + list(df.columns)
                    labels = ["— not present —"] + [str(c) for c in df.columns]
                    current = mapping[field]
                    idx = (list(df.columns).index(current) + 1) if current in list(df.columns) else 0
                    sel = st.selectbox(field.replace("_", " ").title(), labels,
                                       index=idx, key=f"map_{field}")
                    mapping[field] = None if sel == "— not present —" else sel
            st.session_state["mapping"] = mapping
            st.subheader("Preview")
            st.dataframe(df.head(10), use_container_width=True)
            candidates = to_candidates(df, mapping)
            st.caption(f"{len(candidates)} candidate rows ready.")
        elif df is not None:
            st.warning("The file has no data rows.")

st.header("Step 2 — Enrich")

can_run = bool(candidates) and (mode.startswith("Demo") or salesql_key or contactout_key)

if st.button("🚀 Enrich candidates", disabled=not can_run):
    if mode.startswith("Demo"):
        st.warning("Demo mode: results are fictional and clearly labeled DEMO DATA.")
        iterator = demo_enrich_list()
        total = len(DEMO_CANDIDATES)
    else:
        if not salesql_key and not contactout_key:
            st.error("Paste at least one API key in the sidebar first.")
            st.stop()
        iterator = enrich_list(candidates, salesql_key, contactout_key)
        total = len(candidates)

    progress = st.progress(0, text="Starting…")
    enriched_rows = []
    try:
        for i, cand, res in iterator:
            enriched_rows.append((cand, res))
            progress.progress((i + 1) / total,
                              text=f"Enriched {i + 1}/{total}: {cand.get('first_name','')} "
                                   f"{cand.get('last_name','')}")
    except EnrichmentAuthError as e:
        st.error(f"⛔ {e} Fix the key and run again. No workbook was built.")
        st.stop()
    except EnrichmentCreditError as e:
        st.error(f"⛔ {e}")
        st.stop()
    except Exception as e:
        st.error(f"Unexpected error: {e}")
        st.stop()

    st.session_state["enriched_rows"] = enriched_rows
    progress.progress(1.0, text="Done.")
    st.success(f"Enriched {len(enriched_rows)} candidates.")

st.header("Step 3 — Download workbook")

enriched_rows = st.session_state.get("enriched_rows", [])
if enriched_rows:
    summary = pd.DataFrame([
        {
            "Name": f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
            "Company": c.get("company", ""),
            "Personal email": r.get("personal_email", ""),
            "Work email": r.get("work_email", ""),
            "Work email source": r.get("work_email_source", ""),
        }
        for c, r in enriched_rows
    ])
    st.dataframe(summary, use_container_width=True)
    xlsx_bytes = build_workbook(enriched_rows)
    st.download_button(
        "⬇️ Download 3-sheet Excel workbook",
        data=xlsx_bytes,
        file_name="enriched_candidates.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    with st.expander("What do the 3 sheets mean?"):
        st.markdown(
            "- **1 - Personal Preferred:** personal email/phone first, work details as backup.\n"
            "- **2 - Professional Preferred:** work email/phone first (pattern-guessed or "
            "website-found work emails are marked UNVERIFIED and highlighted), personal as backup.\n"
            "- **3 - Maximum:** every personal + professional detail in one place."
        )
else:
    st.caption("Your workbook download will appear here after enrichment.")

with st.expander("⚠️ Honest limits — read before selling or relying on results"):
    st.markdown(
        "- **SalesQL API needs a Professional or Premium plan** (Free/Basic have no API "
        "access). Each reveal spends 1 credit from *your* key.\n"
        "- **ContactOut API needs a paid plan** (Basic $49/mo and up includes API access). "
        "Reveals spend credits from *your* key across email/phone/search pools.\n"
        "- **Pattern-guessed work emails are NOT verified.** They are common-format guesses "
        "(first.last@company.com etc.), highlighted amber in the workbook. Verify before "
        "sending — never present them as confirmed.\n"
        "- **Website-found emails are also unverified** — matched by name on the company site.\n"
        "- **Coverage is never 100%.** Data vendors miss people; blank cells mean 'not found', "
        "not 'does not exist'.\n"
        "- **Your keys never leave your browser session** in this app and are only sent to "
        "the vendors' own APIs. If you deploy this publicly, put keys in Streamlit Secrets, "
        "never in code."
    )
