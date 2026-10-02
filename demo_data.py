"""
Demo mode: fictional candidates + canned enrichment results.

No network calls, no API keys, no credits spent. Lets anyone test the full
workflow end-to-end (upload -> enrich -> 3-sheet workbook download).
All people/companies here are fictional.
"""

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
        "company_domain": "",
    },
]

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


def demo_enrich_list():
    """Yields (index, candidate, result) exactly like enrich.enrich_list."""
    for i, (cand, res) in enumerate(zip(DEMO_CANDIDATES, DEMO_RESULTS)):
        yield i, cand, dict(res)
