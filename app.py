"""
Candidate Enrichment Agent — Streamlit web app.

Three tabs:
  * Demo (free test) — fictional data, no login or keys needed. Upload a
    sheet of ANY size; every row gets a fictional demo result.
  * API keys — the buyer pastes their OWN SalesQL API key and/or ContactOut
    API token. The stable path for real enrichment.
  * Login (EXPERIMENTAL) — the buyer logs in with their OWN SalesQL or
    ContactOut username + password; a headless browser drives the vendor's
    web UI. Fragile: bot checks, CAPTCHAs and OAuth-only accounts can break
    it, and automating logins may violate the vendor's terms / risk the
    buyer's account.

Credentials live only in this browser session's memory (Streamlit
session_state) — they are never written to disk, never logged, and never
sent anywhere except the vendors' own sites/APIs.
"""

import pandas as pd
import streamlit as st

from demo_data import DEMO_CANDIDATES, demo_enrich_list
from enrich import EnrichmentAuthError, EnrichmentCreditError, enrich_list
from workbook import build_workbook

# Login mode is optional: the app must import cleanly even when Playwright
# (or browser_enrich's other needs) are unavailable.
try:
    import browser_enrich as _be
    _LOGIN_IMPORT_OK = True
except Exception:
    _be = None
    _LOGIN_IMPORT_OK = False

_LOGIN_MODE_SUPPORTED = _LOGIN_IMPORT_OK and bool(
    getattr(_be, "PLAYWRIGHT_AVAILABLE", False))

_LOGIN_ERRORS = tuple(
    c for c in [
        getattr(_be, "LoginAuthError", None),
        getattr(_be, "LoginBlockedError", None),
        getattr(_be, "LoginUnavailableError", None),
    ] if isinstance(c, type) and issubclass(c, Exception)
)

st.set_page_config(page_title="BPO WIZARD", layout="wide")

# ---------------------------------------------------------------------------
# Column mapping helpers
# ---------------------------------------------------------------------------

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


def read_upload(uploaded):
    """Read an uploaded CSV/Excel into a DataFrame, or None on failure."""
    try:
        if uploaded.name.lower().endswith(".csv"):
            return pd.read_csv(uploaded)
        return pd.read_excel(uploaded)
    except Exception as e:
        st.error(f"Could not read that file: {e}")
        return None


def upload_block(key: str):
    """Upload + auto column mapping. Returns the candidate list (any size)."""
    uploaded = st.file_uploader(
        "Upload your candidate list (CSV or Excel — any number of rows)",
        type=["csv", "xlsx", "xls"], key=f"{key}_up")
    if uploaded is None:
        return []
    st.session_state[f"{key}_upload_bytes"] = uploaded.getvalue()
    st.session_state[f"{key}_upload_name"] = uploaded.name
    df = read_upload(uploaded)
    if df is None or df.empty:
        if df is not None:
            st.warning("The file has no data rows.")
        return []
    mapping = auto_map(list(df.columns))
    matched = [f for f, h in mapping.items() if h]
    st.caption(f"Detected columns: {', '.join(matched) or 'none'} — "
               f"{len(df)} rows ready.")
    with st.expander("Fix column mapping (only if something looks wrong)"):
        cols = st.columns(3)
        for i, field in enumerate(ALIASES.keys()):
            with cols[i % 3]:
                labels = ["— not present —"] + [str(c) for c in df.columns]
                current = mapping[field]
                idx = (list(df.columns).index(current) + 1) \
                    if current in list(df.columns) else 0
                sel = st.selectbox(field.replace("_", " ").title(), labels,
                                   index=idx, key=f"{key}_map_{field}")
                mapping[field] = None if sel == "— not present —" else sel
    return to_candidates(df, mapping)


def run_enrichment(key: str, candidates, make_iterator, note=None, can_run=None):
    """Enrich button + progress. Stores rows in session_state on success."""
    ready = can_run if can_run is not None else bool(candidates)
    if st.button("🚀 Enrich candidates", disabled=not ready,
                 key=f"{key}_go", type="primary"):
        if note:
            st.warning(note)
        total = len(candidates)
        progress = st.progress(0, text="Starting…")
        enriched_rows = []
        try:
            for i, cand, res in make_iterator():
                enriched_rows.append((cand, res))
                progress.progress(
                    (i + 1) / total,
                    text=f"Enriched {i + 1}/{total}: "
                         f"{cand.get('first_name', '')} {cand.get('last_name', '')}")
        except _LOGIN_ERRORS as e:
            st.error(f"⛔ Login mode failed: {e}")
            st.stop()
        except EnrichmentAuthError as e:
            st.error(f"⛔ {e} Fix the key and run again. No workbook was built.")
            st.stop()
        except EnrichmentCreditError as e:
            st.error(f"⛔ {e}")
            st.stop()
        except Exception as e:  # noqa: BLE001 — surface unexpected errors plainly
            st.error(f"Unexpected error: {e}")
            st.stop()
        st.session_state[f"{key}_enriched"] = enriched_rows
        progress.progress(1.0, text="Done.")
        st.success(f"Enriched {len(enriched_rows)} candidates.")


def fill_original_sheet(file_bytes, filename, enriched_rows):
    """Write enriched contact details back into the uploaded sheet's own
    columns (in place). Only fills blank cells — existing data is never
    overwritten. Adds a missing contact column at the end if needed.

    Work-email routing + highlighting (user's rules):
    - Work emails from any tool (Hunter, SalesQL, ContactOut, Lusha) go to
      the "Work Email" column, unhighlighted.
    - UNVERIFIED work emails (website, directory, constructed) go to a
      separate "Possible Work Email" column (created if missing) with a
      PINK highlight.
    Returns xlsx bytes."""
    import io
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill

    PINK_FILL = PatternFill("solid", fgColor="FFC7CE")

    def _is_tool_email(source: str) -> bool:
        s = (source or "").lower()
        return any(t in s for t in ("hunter.io", "salesql", "contactout",
                                    "lusha"))

    bio = io.BytesIO(file_bytes)
    if filename.lower().endswith(".csv"):
        df = pd.read_csv(bio)
        tmp = io.BytesIO()
        df.to_excel(tmp, index=False)
        tmp.seek(0)
        bio = tmp
    wb = load_workbook(bio)
    ws = wb.active

    headers = [(c.value if c.value is not None else "") for c in ws[1]]
    norm = {str(h).strip().lower(): i for i, h in enumerate(headers)
            if str(h).strip()}
    targets = {
        "work_email": (["work email"], "Work Email"),
        "personal_email": (["personal email"], "Personal Email"),
        "personal_phone": (["mobile phone", "personal phone", "mobile"],
                           "Mobile Phone"),
        "work_phone": (["work phone", "work direct phone", "direct phone",
                        "office phone"], "Work Phone"),
    }
    # Separate column for unverified work emails — always added so the
    # user can see where unverified emails land (stays empty if none).
    targets["possible_work_email"] = ([], "Possible Work Email")
    # Notes column: explains what happened for each candidate (domain found,
    # fallbacks tried, why emails were dropped). Always added.
    targets["notes"] = ([], "Enrichment Notes")
    colmap = {}
    for field, (aliases, new_name) in targets.items():
        idx = next((norm[a] for a in aliases if a in norm), None)
        if idx is None:
            idx = len(headers)
            ws.cell(row=1, column=idx + 1, value=new_name)
            headers.append(new_name)
            norm[new_name.strip().lower()] = idx
        colmap[field] = idx

    def _blank(cell):
        v = cell.value
        return v is None or str(v).strip() in ("", "nan", "None")

    for i, (_cand, res) in enumerate(enriched_rows):
        r = i + 2  # 1-indexed + header row
        if r > ws.max_row:
            break
        we = res.get("work_email") or ""
        we_tool = _is_tool_email(res.get("work_email_source"))
        vals = {
            # Verified tool email -> Work Email; unverified -> Possible column
            "work_email": we if we_tool else "",
            "possible_work_email": we if (we and not we_tool) else "",
            "personal_email": res.get("personal_email") or "",
            "personal_phone": res.get("personal_phone") or "",
            "work_phone": res.get("work_phone") or "",
            "notes": res.get("notes") or "",
        }
        for field, cidx in colmap.items():
            if not vals[field]:
                continue
            cell = ws.cell(row=r, column=cidx + 1)
            if _blank(cell):
                cell.value = vals[field]
                if field == "possible_work_email":
                    cell.fill = PINK_FILL
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def download_block(key: str):
    """Results preview + downloads for a tab: filled-in original sheet
    first, then the 3-sheet workbook."""
    enriched_rows = st.session_state.get(f"{key}_enriched", [])
    if not enriched_rows:
        st.caption("Your download will appear here after enrichment.")
        return
    summary = pd.DataFrame([
        {
            "Name": f"{c.get('first_name', '')} {c.get('last_name', '')}".strip(),
            "Company": c.get("company", ""),
            "Personal email": r.get("personal_email", ""),
            "Work email": r.get("work_email", ""),
            "Work email source": r.get("work_email_source", ""),
            "What happened": r.get("notes", ""),
        }
        for c, r in enriched_rows
    ])
    st.dataframe(summary, use_container_width=True)
    up_bytes = st.session_state.get(f"{key}_upload_bytes")
    up_name = st.session_state.get(f"{key}_upload_name", "candidates.xlsx")
    if up_bytes:
        filled = fill_original_sheet(up_bytes, up_name, enriched_rows)
        base = up_name.rsplit(".", 1)[0]
        st.download_button(
            "⬇️ Download your sheet with details filled in",
            data=filled,
            file_name=f"{base}_enriched.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key}_dl_filled",
        )
    xlsx_bytes = build_workbook(enriched_rows)
    st.download_button(
        "⬇️ Download 3-sheet Excel workbook",
        data=xlsx_bytes,
        file_name="enriched_candidates.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{key}_dl",
    )
    with st.expander("What do the 3 sheets mean?"):
        st.markdown(
            "- **1 - Personal Preferred:** personal email/phone first, work details as backup.\n"
            "- **2 - Professional Preferred:** work email/phone first (pattern-guessed or "
            "website-found work emails are marked UNVERIFIED and highlighted), personal as backup.\n"
            "- **3 - Maximum:** every personal + professional detail in one place."
        )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.title("🧙 BPO WIZARD")
st.caption("Enrich candidate lists with contact details, then download a "
           "3-sheet Excel workbook.")

tab_demo, tab_api, tab_login = st.tabs(
    ["🧪 Demo (free test)", "🔑 API keys", "🔐 Login (experimental)"])

# ------------------------------------------------------------------ Demo tab
with tab_demo:
    st.info("**Demo mode** — everything is fictional: no login, no keys, no "
            "credits, no real lookups. Upload a sheet with **as many rows as "
            "you like** and every row gets a fictional demo result.")
    col_up, col_sample = st.columns([3, 2])
    with col_up:
        demo_candidates = upload_block("demo")
        demo_canned = False
    with col_sample:
        st.write("")
        st.write("")
        if st.button("Or try 5 sample candidates", key="demo_sample"):
            st.session_state["demo_candidates"] = DEMO_CANDIDATES
            st.session_state["demo_canned"] = True
    if not demo_candidates and st.session_state.get("demo_candidates"):
        demo_candidates = st.session_state["demo_candidates"]
        demo_canned = st.session_state.get("demo_canned", False)
        st.caption(f"{len(demo_candidates)} sample candidates loaded.")

    run_enrichment(
        "demo", demo_candidates,
        lambda: demo_enrich_list(demo_candidates, canned=demo_canned),
        note="Demo mode: results are fictional and clearly labeled DEMO DATA.")
    download_block("demo")

# ------------------------------------------------------------------- API tab
with tab_api:
    st.info("**API key mode (recommended for real use)** — paste YOUR OWN keys. "
            "Keys stay in this browser session only; lookups spend credits "
            "from your accounts.")
    c1, c2 = st.columns(2)
    with c1:
        salesql_key = st.text_input(
            "SalesQL API key", type="password", key="api_sql",
            help="SalesQL dashboard → Settings → API Access. Needs Professional or Premium plan.",
        ) or None
    with c2:
        contactout_key = st.text_input(
            "ContactOut API token", type="password", key="api_co",
            help="ContactOut dashboard → API. Needs a paid plan.",
        ) or None
        if st.button("Test ContactOut key (free, no credits spent)", key="api_test"):
            if not contactout_key:
                st.error("Paste your ContactOut token first.")
            else:
                try:
                    from enrich import ContactOutClient
                    stats = ContactOutClient(contactout_key).stats()
                    st.success(f"Key works. Credit info: {stats}")
                except EnrichmentAuthError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Key check failed: {e}")
    c3, c4 = st.columns(2)
    with c3:
        hunter_key = st.text_input(
            "Hunter.io API key", type="password", key="api_hunter",
            help="hunter.io → Dashboard → API. Free plan: 25 searches + 50 verifications/month, no card. Finds WORK emails only.",
        ) or None
        if st.button("Test Hunter.io key (free)", key="api_test_hunter"):
            if not hunter_key:
                st.error("Paste your Hunter.io API key first.")
            else:
                try:
                    from enrich import HunterClient
                    acct = HunterClient(hunter_key).account()
                    st.success(
                        f"Key works. Plan: {acct['plan']} — searches: "
                        f"{acct['searches_used']}/{acct['searches_available']} used, "
                        f"verifications: {acct['verifications_used']}/"
                        f"{acct['verifications_available']} used."
                    )
                except EnrichmentAuthError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Key check failed: {e}")
    with c4:
        lusha_key = st.text_input(
            "Lusha API key", type="password", key="api_lusha",
            help="dashboard.lusha.com → API & Integrations. Free plan: ~40 credits/month, no card. Finds work emails AND phone numbers (1 credit/email, ~5/phone).",
        ) or None
        if st.button("Test Lusha key (free)", key="api_test_lusha"):
            if not lusha_key:
                st.error("Paste your Lusha API key first.")
            else:
                try:
                    from enrich import LushaClient
                    usage = LushaClient(lusha_key).usage()
                    st.success(f"Key works. Usage info: {usage}")
                except EnrichmentAuthError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Key check failed: {e}")

    api_candidates = upload_block("api")
    can_run = bool(api_candidates) and (salesql_key or contactout_key or hunter_key
                                        or lusha_key)
    if api_candidates and not (salesql_key or contactout_key or hunter_key or lusha_key):
        st.warning("Paste at least one API key above to run.")
    if hunter_key and api_candidates:
        st.caption(f"Hunter.io free plan: 25 searches/month. "
                   f"This run would use up to {len(api_candidates)} searches "
                   f"(+1 verification each for emails found). Test your key "
                   f"above to see remaining credits.")
    if lusha_key and api_candidates:
        st.caption(f"Lusha free plan: ~40 credits/month — 1 credit per email, "
                   f"~5 per phone. This run uses 1 Lusha call per candidate "
                   f"that still needs an email or phone, revealing only "
                   f"what's missing (~1-7 credits each).")
    sheet_type = st.radio(
        "What type of sheet is this?",
        ["Professional (work emails required)",
         "Personal (personal emails/phones first)",
         "Maximum (everything)"],
        key="api_sheet_type",
        help="Professional: goes all-out on work emails (Hunter + all fallbacks). "
             "Personal: prioritizes personal contacts, work email is bonus. "
             "Maximum: finds everything.")
    # Map to short codes used by enrich.py
    sheet_code = {"Professional (work emails required)": "professional",
                  "Personal (personal emails/phones first)": "personal",
                  "Maximum (everything)": "maximum"}[sheet_type]
    run_enrichment("api", api_candidates,
                   lambda: enrich_list(api_candidates, salesql_key, contactout_key,
                                       hunter_key, lusha_key,
                                       sheet_type=sheet_code),
                   can_run=can_run)
    download_block("api")

# ----------------------------------------------------------------- Login tab
with tab_login:
    st.caption("Login helper v2 — automatic and manual sign-in supported.")
    st.error("**EXPERIMENTAL** — a browser signs in to the vendor's "
             "website. ContactOut always shows a 'verify you're human' "
             "checkbox that only a person can click, so **manual sign-in is "
             "recommended**. Automating logins may violate the vendor's Terms "
             "of Service or risk your account being banned. Try a small test "
             "run first.")
    if not _LOGIN_MODE_SUPPORTED:
        st.error("Login mode needs the Playwright browser package, which is "
                 "not installed on this server.")
    c1, c2 = st.columns(2)
    with c1:
        login_provider = st.selectbox("Provider", ["SalesQL", "ContactOut"],
                                      key="login_prov")
    with c2:
        login_how = st.radio(
            "How to sign in",
            ["I'll sign in myself in the browser window (recommended)",
             "Type my email + password, app signs in"],
            key="login_how")
    manual_mode = login_how.startswith("I'll sign in myself")
    if manual_mode:
        st.info("When you click **Enrich candidates**, a browser window opens "
                "on the provider's login page. Sign in yourself — including "
                "the 'verify you're human' checkbox — and the app continues "
                "automatically once you're signed in. Your credentials never "
                "touch this app.")
        login_user = login_pass = None
        show_browser = True
    else:
        c3, c4 = st.columns(2)
        with c3:
            login_user = st.text_input("Email / username",
                                       key="login_user") or None
        with c4:
            login_pass = st.text_input("Password", type="password",
                                       key="login_pass") or None
        st.caption("Credentials stay in this browser session only — never stored, "
                   "never logged, never written to disk.")
        show_browser = st.checkbox(
            "Show the browser window while it works",
            help="If the provider shows a 'verify you're human' check, you can "
                 "solve it yourself in the visible window. Recommended when "
                 "running on your own computer.",
            key="login_show")

    login_candidates = upload_block("login")
    if manual_mode:
        can_login = (bool(login_candidates) and _LOGIN_MODE_SUPPORTED
                     and _be is not None)
        make_iter = lambda: _be.manual_login_enrich_list(
            login_candidates,
            provider=(login_provider or "ContactOut").lower())
        note = ("A browser window will open — sign in yourself (including "
                "the 'verify you're human' checkbox). The run continues "
                "automatically once you're signed in.")
    else:
        can_login = (bool(login_candidates) and bool(login_user)
                     and bool(login_pass) and _LOGIN_MODE_SUPPORTED
                     and _be is not None)
        make_iter = lambda: _be.login_enrich_list(
            login_candidates,
            provider=(login_provider or "SalesQL").lower(),
            username=login_user, password=login_pass,
            headless=not show_browser)
        note = ("Login mode is experimental: a browser will sign in "
                "now. This is slow and may be blocked.")
    if login_candidates and not manual_mode and not (login_user and login_pass):
        st.warning("Enter your provider username and password to run.")
    run_enrichment("login", login_candidates, make_iter, note=note,
                   can_run=can_login)
    download_block("login")

# ---------------------------------------------------------------------------
# Honest limits (short)
# ---------------------------------------------------------------------------
with st.expander("⚠️ Honest limits — read before relying on results"):
    st.markdown(
        "- **Demo results are fictional.** Every demo row is labeled DEMO DATA — "
        "never treat it as real contact info.\n"
        "- **SalesQL API needs a Professional or Premium plan** (Free/Basic have "
        "no API access). Each reveal spends 1 credit from *your* key.\n"
        "- **ContactOut API needs a paid plan.** Reveals spend credits from *your* key.\n"
        "- **Pattern-guessed and website-found work emails are NOT verified** — "
        "highlighted amber in the workbook. Verify before sending.\n"
        "- **Coverage is never 100%.** Blank cells mean 'not found', not 'does not exist'."
    )
