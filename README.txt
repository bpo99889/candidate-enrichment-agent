================================================================
CANDIDATE ENRICHMENT AGENT — Test Version
README (plain English)
================================================================

WHAT THIS DOES
-------------
You upload a list of candidates (CSV or Excel with First Name, Last Name,
Company, Title, LinkedIn URL, and optionally Company Domain). The app looks
up each person's contact details using YOUR OWN SalesQL and/or ContactOut
accounts, then gives you ONE Excel workbook with 3 sheets:

  Sheet 1 — "Personal Preferred"
      Personal email + personal phone first. Work email/phone added as
      backup columns when available.

  Sheet 2 — "Professional Preferred"
      Work email + work phone first. If the data tool has no work email,
      the app tries two fallbacks, in this order:
        a) looks for a name-matching email on the company website
        b) builds the email from common patterns (first.last@company.com…)
      Fallback emails are marked UNVERIFIED and highlighted amber — they
      are guesses, not confirmed addresses. Personal details appear as
      backup columns.

  Sheet 3 — "Maximum (All Details)"
      Every personal + professional detail in one place.

YOUR KEYS, YOUR CREDITS (BYOK MODEL)
------------------------------------
This app has no data of its own. Every buyer connects THEIR OWN accounts:

  - SalesQL: dashboard → Settings → API Access → create an API key.
    Paste it in the app's sidebar. IMPORTANT: the SalesQL API only works
    on Professional or Premium plans. Free and Basic plans have NO API
    access, so the app cannot work with those.
  - ContactOut: dashboard → API section → copy your API token. Paste it
    in the app's sidebar. Needs a paid plan (Basic $49/month and up
    includes API access).
  - Hunter.io: hunter.io → Dashboard → API → copy your API key. FREE plan
    needs no credit card: 25 searches + 50 verifications per month — good
    for testing. Finds WORK emails only (no personal emails, no phones).
    Needs each candidate's company domain: add a Website/Domain column to
    your sheet, or the app derives it from the company name (marked as
    derived — confirm it).
  - Lusha: dashboard.lusha.com → API & Integrations → copy your API key.
    FREE plan (~40 credits/month, no card; API works with strict rate
    limits). Finds work emails AND phone numbers (direct dials/mobiles) —
    the phone data the other free APIs lack. 1 credit per email reveal,
    ~5-10 per phone reveal, so the app only calls Lusha for candidates
    that still need an email or phone after the other tools.

Keys are typed into password boxes and live ONLY in your browser session
while the app is open. They are never saved in the code, never written to
a file, and never logged. Each lookup spends credits from YOUR account —
1 SalesQL credit per candidate found; ContactOut spends from its
email/phone credit pools.

DEMO MODE (try it with no keys, no cost)
---------------------------------------
Open the app, stay on the "Demo" tab, and either upload your own candidate
list (CSV or Excel, any number of rows) or click "try 5 sample candidates".
Then "Enrich candidates", then download the workbook. All names and
results are fictional and labeled DEMO DATA. This exercises the entire
workflow end-to-end for free.

LOGIN MODE — username + password (EXPERIMENTAL, for testing)
---------------------------------------------------------------
What it is: instead of an API key, you pick a provider (SalesQL or
ContactOut), type your username/email + password, and the app opens a
headless Chromium browser (Playwright), signs in to the vendor's website,
and attempts to find each candidate's contact details through the web UI.
The 3-sheet workbook output is identical to the other modes.

How to run it:
  1. Install the browser piece once:
       pip install playwright && playwright install chromium
     (Streamlit Cloud installs playwright from requirements.txt, but the
     Chromium browser download needs extra setup — see Streamlit's docs
     on Playwright; on a first deploy Login mode may show "not installed"
     until the browser binary is present.)
  2. In the app sidebar choose "Login mode (username + password —
     experimental)", pick the provider, enter your credentials
     (password box; session-only, never saved or logged).
  3. Upload candidates and click Enrich. It is SLOW — roughly 10–30
     seconds per candidate — because it drives real web pages.

HONEST LIMITS — read before using or selling this:
  1. FRAGILE. Bot detection, CAPTCHAs and "verify you are human" checks
     can stop it mid-run. When that happens the app stops with a clear
     error instead of guessing.
  2. OAuth-only accounts are NOT supported. If your SalesQL/ContactOut
     account signs in via Google or LinkedIn buttons (no password box),
     automation cannot complete it — use an email+password account.
  3. TERMS-OF-SERVICE / BAN RISK. Automating logins may violate the
     vendor's Terms of Service and can get the account restricted or
     banned. Never run a buyer's credentials through this without their
     explicit consent, and prefer a test account first.
  4. UNTESTED LIVE. No real login was performed while building this.
     Login page layouts and dashboard search flows were reconstructed
     from public research (Oct 2026) and WILL differ in places. Every
     failure is caught per-candidate and recorded in the Notes column —
     the run never crashes, but results may be thin.
  5. SalesQL caveat: SalesQL's product mainly reveals contacts through
     its LinkedIn browser extension. The web dashboard has no verified
     person-search page we could automate, so SalesQL login-mode lookups
     are best-effort and may return little. ContactOut's dashboard
     search is more automatable.
  6. Page-scraped contacts are NOT "verified by tool". In the workbook
     they are sourced as "<Provider> web login (page-scraped — confirm
     before use)". Treat them as leads, not confirmed data.
  7. SLOWER than API mode and heavier to host (needs a real browser).
     API-key mode remains the stable, supported path for real use and
     for anything you sell.

FILES IN THIS FOLDER
--------------------
  app.py            The Streamlit web app (upload → enrich → download).
  enrich.py         The enrichment engine: API clients + work-email
                    fallback chain (API → website → pattern guess).
  browser_enrich.py EXPERIMENTAL login-mode engine: signs in to SalesQL /
                    ContactOut with your username + password via a headless
                    browser (Playwright) and attempts web lookups. No live
                    login was ever tested — see LOGIN MODE section.
  workbook.py       Builds the 3-sheet Excel file (openpyxl).
  demo_data.py      Fictional demo candidates + canned results.
  requirements.txt  Python packages Streamlit Cloud installs (now includes
                    playwright for Login mode).
  README.txt        This file.

HOW TO DEPLOY FREE ON STREAMLIT CLOUD (step by step)
---------------------------------------------------
You need a free GitHub account. Total cost: $0.

  1. Put these files on GitHub:
     - Go to github.com and create a NEW PUBLIC repository, e.g.
       "enrichment-agent".
     - Click "uploading an existing file" and upload ALL files from this
       folder (app.py, enrich.py, workbook.py, demo_data.py,
       requirements.txt). Do NOT upload README.txt (optional).
     - Click "Commit changes".

  2. Connect Streamlit Cloud:
     - Go to share.streamlit.io and sign in with the SAME GitHub account.
     - Click "Create app" → choose your repository, branch "main",
       and main file path "app.py".
     - Click "Deploy". Wait 2–3 minutes.

  3. Open your new app URL (looks like
     https://your-app-name.streamlit.app). It starts in Demo mode.

  4. Using real accounts:
     - Switch the sidebar to "API key mode" and paste your SalesQL and/or
       ContactOut key into the password boxes. Keys stay in your
       browser session only.
     - "Login mode" is the experimental alternative: pick the provider,
       type your username + password, and the app drives the vendor's
       website with a headless browser. See LOGIN MODE section below —
       read the warnings first.
     - (Optional, more permanent) In the Streamlit Cloud dashboard, open
       your app → Settings → Secrets, and add your keys there. The app
       reads them automatically on every visit. Format:
           SALESQL_API_KEY = "paste-key-here"
           CONTACTOUT_API_TOKEN = "paste-token-here"

  5. Share the URL with buyers. Each buyer pastes THEIR OWN keys —
     your keys and credits are never touched by their lookups.

WHAT WE RESEARCHED ABOUT THE APIs (honest notes)
------------------------------------------------
CONFIRMED from the vendors' own public docs (Oct 2026):

  SalesQL (docs.salesql.com, salesql.com/api):
    - REST API at https://api-public.salesql.com/v1
    - Person enrichment: GET /persons/enrich
    - Auth: "Authorization: Bearer <API_KEY>" header; key created at
      Settings → API Access
    - Accepts linkedin_url, email, first_name, last_name, full_name,
      organization_name, organization_domain
    - Returns verified emails (with Work/Personal type + Valid status)
      and phones in predictable JSON
    - API access ONLY on Professional and Premium plans. Free and Basic
      have no API access.
    - Rate limits (vendor notes these are proposed/pending final
      validation): Professional 1,500 calls/day (monthly) or 5,000/day
      (annual); Premium 4,000/day or 20,000/day.
    - Billing: 1 credit per prospect reveal (emails + phones + company
      data together); you pay only when results are found; phone
      numbers cost no extra credits.
    - Annual prices seen: Professional $711/yr (60,000 credits),
      Premium $1,071/yr (144,000 credits).

  ContactOut (api.contactout.com official API reference):
    - REST API at https://api.contactout.com
    - Auth: "token: <API_TOKEN>" request header
    - Key endpoints: GET /v1/people/linkedin/enrich (full profile),
      GET /v1/people/linkedin (contacts only, supports
      include_phone + email_type filters), GET /v1/stats (FREE —
      checks your credit balance, used by this app's "Test key" button)
    - Free pre-checks that spend NO credits: email/phone availability
      checkers, people count, domain enrichment
    - API access included on paid plans: Basic $49/mo (1,000
      credits/mo), Starter $99/mo (5,000), Business $249/mo (15,000),
      Enterprise custom. Trial = 10 credits.
    - Rate limits: people search 60/min, availability checkers
      150/min, other endpoints 1,000/min.
    - Credits are tracked in separate pools (email / phone / search /
      verifier).

  WHAT WE COULD *NOT* VERIFY (flagged, not assumed):
    - Whether SalesQL's API returns personal emails on all plans, or
      only work emails in some cases (docs example shows a Work email;
      personal-type handling in this app is defensive).
    - ContactOut's exact per-reveal credit costs on official pricing
      (third-party docs describe 4 credit pools; official docs confirm
      the pools exist but not the per-call cost).
    - Whether ContactOut's free trial includes API access.
    - ContactOut's "two keys" setup (separate work-email and
      personal-email keys with separate credit pools) is reported by
      third-party integration docs, not confirmed in official docs.
      This app accepts one token; a buyer with two keys can run the
      app twice (once per key) if needed.
    - We have NOT tested either API with a real paid key — the live
      code paths follow the vendors' documented examples exactly, but
      the buyer should run one small live batch first and compare.

HONEST LIMITS (read before selling or relying on results)
---------------------------------------------------------
  1. Pattern-guessed work emails are NOT verified. They are
     common-format guesses highlighted amber in the workbook. Never
     present them as confirmed — verify (e.g., with an email
     verification tool) before sending.
  2. Website-found emails are also unverified (matched by name on the
     company site).
  3. Coverage is never 100%. Vendors miss people. A blank cell means
     "not found", not "does not exist".
  4. Every live lookup spends the BUYER's credits. Make this clear to
     buyers: they need their own paid SalesQL (Professional+) and/or
     ContactOut (paid) accounts.
  5. Rate limits apply (see above). Large lists take time; the app
     pauses briefly between lookups.
  6. If you deploy publicly, never put API keys in the code or repo —
     use the sidebar password boxes or Streamlit Secrets.

QUESTIONS / NEXT STEPS
----------------------
  - Test Demo mode first (free, 2 minutes).
  - Then run a small LIVE batch (5–10 real candidates) with your own
    key and eyeball the results before selling to anyone.
  - If the live results look right, the app is ready to share.
================================================================
