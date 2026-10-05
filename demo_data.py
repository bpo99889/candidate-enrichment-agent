"""
Demo mode: fictional candidates + generated enrichment results.

No network calls, no API keys, no credits spent. Lets anyone test the full
workflow end-to-end (upload -> enrich -> 3-sheet workbook download) with a
sheet of ANY size. All people/companies here are fictional.

Results are generated deterministically from each candidate's name, so the
same sheet always produces the same demo output.
"""

import hashlib

# ---------------------------------------------------------------------------
# Built-in 5-person sample (kept exactly as before for the "try sample" button)
# ---------------------------------------------------------------------------

DEMO_CANDIDATES = [
    {
        "first_name": "Ava", "last_name": "Martinez",
        "company": "Northwind Traders", "title": "Regional Sales Manager",
        "linkedin_url": "https://www.linkedin.com/in/ava-martinez-demo",
        "company_domain": "northwindtraders.com",
    },
    {
        "first_name": "Liam", "last_name": "Okafor",
        "company": "Bluepeak Roofing", "title": "Territory Sales Rep",
        "linkedin_url": "https://www.linkedin.com/in/liam-okafor-demo",
        "company_domain": "bluepeakroofing.com",
    },
    {
        "first_name": "Sofia", "last_name": "Reyes",
        "company": "Copperline Supply", "title": "Outside Sales Representative",
        "linkedin_url": "https://www.linkedin.com/in/sofia-reyes-demo",
        "company_domain": "copperlinesupply.com",
    },
    {
        "first_name": "Noah", "last_name": "Kim",
        "company": "Harbor Materials", "title": "Account Manager",
        "linkedin_url": "https://www.linkedin.com/in/noah-kim-demo",
        "company_domain": "harbormaterials.com",
    },
    {
        "first_name": "Mia", "last_name": "Novak",
        "company": "Summit Glass Co", "title": "Architectural Representative",
        "linkedin_url": "https://www.linkedin.com/in/mia-novak-demo",
        "company_domain": "",  # no domain -> fallback chain is skipped on purpose
    },
]

# Canned results exercising every branch: API hit, website fallback,
# pattern-guess fallback, and total miss.
DEMO_RESULTS = [
    {
        "personal_email": "ava.martinez87@gmail.com",
        "personal_email_source": "SalesQL API (verified by tool)",
        "personal_phone": "+1 512 555 0148",
        "personal_phone_source": "SalesQL API (type not labeled by tool)",
        "work_email": "ava.martinez@northwindtraders.com",
        "work_email_source": "SalesQL API (verified by tool)",
        "work_phone": "+1 512 555 0190",
        "work_phone_source": "ContactOut API (type not labeled by tool)",
        "notes": "DEMO DATA — not real",
    },
    {
        "personal_email": "liam.okafor@yahoo.com",
        "personal_email_source": "ContactOut API (verified by tool)",
        "personal_phone": "",
        "personal_phone_source": "",
        "work_email": "liam.okafor@bluepeakroofing.com",
        "work_email_source": "company website (UNVERIFIED — confirm before use)",
        "work_phone": "",
        "work_phone_source": "",
        "notes": "DEMO DATA — work email found on company contact page; not verified",
    },
    {
        "personal_email": "",
        "personal_email_source": "",
        "personal_phone": "+1 713 555 0117",
        "personal_phone_source": "SalesQL API (type not labeled by tool)",
        "work_email": "sofia.reyes@copperlinesupply.com",
        "work_email_source": "pattern-guessed (UNVERIFIED — do not use without verification)",
        "work_phone": "",
        "work_phone_source": "",
        "notes": "DEMO DATA — pattern-guessed from first.last@domain; verify before use. "
                 "Other common patterns to try: sofiareyes@, s.reyes@, sofia.r@",
    },
    {
        "personal_email": "noahkim77@hotmail.com",
        "personal_email_source": "SalesQL API (verified by tool)",
        "personal_phone": "",
        "personal_phone_source": "",
        "work_email": "",
        "work_email_source": "",
        "work_phone": "+1 214 555 0163",
        "work_phone_source": "ContactOut API (type not labeled by tool)",
        "notes": "DEMO DATA — work email not found; could not build a pattern.",
    },
    {
        "personal_email": "",
        "personal_email_source": "",
        "personal_phone": "",
        "personal_phone_source": "",
        "work_email": "",
        "work_email_source": "",
        "work_phone": "",
        "work_phone_source": "",
        "notes": "DEMO DATA — no data found; company domain unknown so pattern/website "
                 "fallback was skipped.",
    },
]


# ---------------------------------------------------------------------------
# Fictional result generator for sheets of ANY size
# ---------------------------------------------------------------------------

_PERSONAL_DOMAINS = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com"]


def _stable_int(*parts: str) -> int:
    h = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _clean(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def make_demo_result(cand: dict, index: int = 0) -> dict:
    """Build a fictional enrichment result for ANY candidate dict.

    Deterministic: the same candidate always gets the same demo result.
    Cycles through the same five scenarios as the canned sample
    (full hit, website fallback, pattern-guess fallback, partial, total miss)
    so big sheets still show every workbook branch, including the amber
    UNVERIFIED highlighting.
    """
    first = _clean(cand.get("first_name")) or "alex"
    last = _clean(cand.get("last_name")) or "sample"
    company = (cand.get("company") or "").strip()
    domain = (cand.get("company_domain") or "").strip().lower()
    if not domain:
        domain = (_clean(company) or "examplecorp") + ".com"

    seed = _stable_int(first, last, company, domain, str(index))
    scenario = seed % 5

    num = 10 + (seed % 89)                       # 10..98
    area = 201 + ((seed >> 4) % 680)             # 201..880
    line = 1000 + ((seed >> 12) % 9000)          # 1000..9999
    # 555-01XX numbers are reserved fictional-safe.
    phone = f"+1 {area} 555 {line:04d}"
    personal_domain = _PERSONAL_DOMAINS[seed % len(_PERSONAL_DOMAINS)]

    res = {
        "personal_email": "", "personal_email_source": "",
        "personal_phone": "", "personal_phone_source": "",
        "work_email": "", "work_email_source": "",
        "work_phone": "", "work_phone_source": "",
        "notes": "DEMO DATA — fictional sample, not real people",
    }

    if scenario == 0:
        # Full hit: personal + work, everything "verified".
        res.update({
            "personal_email": f"{first}.{last}{num}@{personal_domain}",
            "personal_email_source": "SalesQL API (verified by tool)",
            "personal_phone": phone,
            "personal_phone_source": "SalesQL API (type not labeled by tool)",
            "work_email": f"{first}.{last}@{domain}",
            "work_email_source": "SalesQL API (verified by tool)",
            "work_phone": phone,
            "work_phone_source": "ContactOut API (type not labeled by tool)",
            "notes": "DEMO DATA — not real",
        })
    elif scenario == 1:
        # Personal hit + work email found on company website (UNVERIFIED).
        res.update({
            "personal_email": f"{first}.{last}@{personal_domain}",
            "personal_email_source": "ContactOut API (verified by tool)",
            "work_email": f"{first}.{last}@{domain}",
            "work_email_source": "company website (UNVERIFIED — confirm before use)",
            "notes": "DEMO DATA — work email found on company contact page; not verified",
        })
    elif scenario == 2:
        # Work email pattern-guessed (UNVERIFIED) + personal phone only.
        res.update({
            "personal_phone": phone,
            "personal_phone_source": "SalesQL API (type not labeled by tool)",
            "work_email": f"{first}.{last}@{domain}",
            "work_email_source": "pattern-guessed (UNVERIFIED — do not use without verification)",
            "notes": "DEMO DATA — pattern-guessed from first.last@domain; verify before use.",
        })
    elif scenario == 3:
        # Partial: personal email verified + work phone, no work email.
        res.update({
            "personal_email": f"{first}{last}{num}@hotmail.com",
            "personal_email_source": "SalesQL API (verified by tool)",
            "work_phone": phone,
            "work_phone_source": "ContactOut API (type not labeled by tool)",
            "notes": "DEMO DATA — work email not found; could not build a pattern.",
        })
    else:
        # Total miss: everything blank, honest note.
        res.update({
            "notes": "DEMO DATA — no data found for this candidate in the demo.",
        })
    return res


def demo_enrich_list(candidates, canned: bool = False):
    """Yields (index, candidate, result) exactly like enrich.enrich_list.

    canned=True replays the hand-crafted 5-person sample results; otherwise
    fictional results are generated for every candidate, however many rows
    the sheet has.
    """
    for i, cand in enumerate(candidates):
        if canned and i < len(DEMO_RESULTS):
            res = dict(DEMO_RESULTS[i])
        else:
            res = make_demo_result(cand, i)
        yield i, cand, res
