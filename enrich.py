"""
Enrichment engine for the Candidate Enrichment Agent.

Queries the buyer's OWN SalesQL and/or ContactOut API keys (BYOK model),
merges the results, and applies the work-email fallback chain:

    1. Work email from SalesQL / ContactOut API        -> verified by the tool
    2. Work email found on the company website         -> UNVERIFIED
    3. Work email built from common patterns          -> UNVERIFIED (pattern-guessed)
    4. Nothing found                                  -> left blank, never invented silently

API details were researched from the vendors' public docs (see README.txt).
Anything we could not confirm is marked in README.txt, not silently assumed.
"""

import re
import time
from typing import Dict, List, Optional, Tuple

import requests

SALESQL_BASE = "https://api-public.salesql.com/v1"
CONTACTOUT_BASE = "https://api.contactout.com"

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "live.com", "msn.com", "ymail.com", "protonmail.com",
    "proton.me", "mail.com", "gmx.com", "zoho.com",
}

EMAIL_PATTERNS = [
    "{first}.{last}",
    "{first}{last}",
    "{f}.{last}",
    "{first}.{l}",
    "{f}{last}",
    "{first}{l}",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
REQUEST_TIMEOUT = 10


class EnrichmentAuthError(Exception):
    """Raised when an API key is rejected (HTTP 401). Stop the run."""


class EnrichmentCreditError(Exception):
    """Raised when the account is out of credits / plan blocked (HTTP 402/403)."""


class SalesQLClient:
    """Thin client for the SalesQL public REST API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _check(self, resp: requests.Response, action: str):
        if resp.status_code == 401:
            raise EnrichmentAuthError("SalesQL rejected the API key (401). Check the key.")
        if resp.status_code in (402, 403):
            raise EnrichmentCreditError(
                f"SalesQL blocked the request ({resp.status_code}) — plan/credits issue."
            )
        if resp.status_code == 429:
            raise RuntimeError("SalesQL rate limit hit (429). Slow down and retry.")
        resp.raise_for_status()

    def enrich_person(
        self,
        linkedin_url: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        company: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Dict:
        params = {}
        if linkedin_url:
            params["linkedin_url"] = linkedin_url
        if first_name:
            params["first_name"] = first_name
        if last_name:
            params["last_name"] = last_name
        if company:
            params["organization_name"] = company
        if domain:
            params["organization_domain"] = domain
        resp = self.session.get(
            f"{SALESQL_BASE}/persons/enrich", params=params, timeout=REQUEST_TIMEOUT
        )
        self._check(resp, "SalesQL person enrich")
        return self._normalize(resp.json())

    @staticmethod
    def _normalize(data: Dict) -> Dict:
        emails = data.get("emails") or []
        phones = data.get("phones") or []
        work_emails, personal_emails = [], []
        for e in emails:
            addr = e.get("email", "").strip()
            if not addr:
                continue
            etype = (e.get("type") or "").lower()
            status = e.get("status") or ""
            entry = {"email": addr, "status": status}
            if "work" in etype:
                work_emails.append(entry)
            elif "personal" in etype or "private" in etype:
                personal_emails.append(entry)
            else:
                work_emails.append(entry)
        return {
            "source": "SalesQL API",
            "work_emails": work_emails,
            "personal_emails": personal_emails,
            "phones": [
                {"phone": p.get("phone", "").strip(), "type": p.get("type") or ""}
                for p in phones
                if p.get("phone")
            ],
            "title": data.get("title") or "",
            "company": (data.get("company") or {}).get("name", "") if isinstance(data.get("company"), dict) else "",
        }


class ContactOutClient:
    """Thin client for the ContactOut v1 REST API."""

    def __init__(self, api_token: str):
        self.session = requests.Session()
        self.session.headers.update({"token": api_token})

    def _check(self, resp: requests.Response, action: str):
        if resp.status_code == 401:
            raise EnrichmentAuthError("ContactOut rejected the API token (401). Check the token.")
        if resp.status_code in (402, 403):
            raise EnrichmentCreditError(
                f"ContactOut blocked the request ({resp.status_code}) — credits/plan issue."
            )
        if resp.status_code == 429:
            raise RuntimeError("ContactOut rate limit hit (429). Slow down and retry.")
        resp.raise_for_status()

    def stats(self) -> Dict:
        resp = self.session.get(f"{CONTACTOUT_BASE}/v1/stats", timeout=REQUEST_TIMEOUT)
        self._check(resp, "ContactOut stats")
        return resp.json()

    def enrich_linkedin(self, profile_url: str) -> Dict:
        resp = self.session.get(
            f"{CONTACTOUT_BASE}/v1/people/linkedin/enrich",
            params={"profile": profile_url},
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp, "ContactOut linkedin enrich")
        return self._normalize(resp.json())

    def contacts(self, profile_url: str, include_phone: bool = True) -> Dict:
        resp = self.session.get(
            f"{CONTACTOUT_BASE}/v1/people/linkedin",
            params={
                "profile": profile_url,
                "include_phone": "true" if include_phone else "false",
                "email_type": "personal,work",
            },
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp, "ContactOut contacts")
        return self._normalize(resp.json())

    @staticmethod
    def _normalize(data: Dict) -> Dict:
        emails = data.get("emails") or []
        phones = data.get("phones") or []
        company = data.get("current_company") or {}
        company_domain = ""
        if isinstance(company, dict):
            company_domain = (company.get("domain") or "").replace("https://", "").replace("http://", "").strip("/")
        work_emails, personal_emails = [], []
        for addr in emails:
            addr = addr.strip()
            if not addr or "@" not in addr:
                continue
            dom = addr.split("@")[-1].lower()
            if dom in FREE_EMAIL_DOMAINS:
                personal_emails.append({"email": addr, "status": ""})
            elif company_domain and dom == company_domain.lower():
                work_emails.append({"email": addr, "status": ""})
            else:
                work_emails.append({"email": addr, "status": ""})
        return {
            "source": "ContactOut API",
            "work_emails": work_emails,
            "personal_emails": personal_emails,
            "phones": [{"phone": p.strip(), "type": ""} for p in phones if p and p.strip()],
            "title": data.get("title") or "",
            "company": company.get("name", "") if isinstance(company, dict) else "",
            "company_domain": company_domain,
        }


def guess_work_emails(first_name: str, last_name: str, domain: str) -> List[str]:
    """Build common work-email patterns. NEVER presented as verified."""
    first = re.sub(r"[^a-z]", "", (first_name or "").lower())
    last = re.sub(r"[^a-z]", "", (last_name or "").lower())
    domain = (domain or "").strip().lower()
    if not (first and last and domain and "." in domain):
        return []
    seen, out = set(), []
    for pat in EMAIL_PATTERNS:
        local = pat.format(first=first, last=last, f=first[0], l=last[0])
        addr = f"{local}@{domain}"
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def find_email_on_website(domain: str, first_name: str, last_name: str) -> List[str]:
    """Best-effort: fetch the company homepage and look for a name-matching email."""
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return []
    first = (first_name or "").lower()
    last = (last_name or "").lower()
    if not (first or last):
        return []
    url = domain if domain.startswith("http") else f"https://{domain}"
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CandidateEnrichmentAgent/1.0)"},
        )
        if resp.status_code != 200 or not resp.text:
            return []
        found = []
        for m in set(EMAIL_RE.findall(resp.text)):
            ml = m.lower()
            if (first and first in ml) or (last and last in ml):
                local = ml.split("@")[0]
                if local not in {"info", "contact", "support", "sales", "hello", "admin"}:
                    found.append(m)
        return sorted(set(found))
    except Exception:
        return []


def clean_domain(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://", "", v).split("/")[0]
    return v


def enrich_candidate(
    candidate: Dict,
    salesql_key: Optional[str] = None,
    contactout_key: Optional[str] = None,
    polite_delay: float = 0.4,
) -> Dict:
    first = (candidate.get("first_name") or "").strip()
    last = (candidate.get("last_name") or "").strip()
    company = (candidate.get("company") or "").strip()
    linkedin_url = (candidate.get("linkedin_url") or "").strip()
    domain = clean_domain(candidate.get("company_domain") or "")

    result = {
        "personal_email": "", "personal_email_source": "",
        "personal_phone": "", "personal_phone_source": "",
        "work_email": "", "work_email_source": "",
        "work_phone": "", "work_phone_source": "",
        "notes": [],
    }

    api_hits: List[Dict] = []

    if salesql_key and (linkedin_url or (first and last)):
        try:
            client = SalesQLClient(salesql_key)
            api_hits.append(
                client.enrich_person(
                    linkedin_url=linkedin_url or None,
                    first_name=first or None, last_name=last or None,
                    company=company or None, domain=domain or None,
                )
            )
        except (EnrichmentAuthError, EnrichmentCreditError):
            raise
        except Exception as exc:
            result["notes"].append(f"SalesQL lookup failed: {exc}")
        time.sleep(polite_delay)

    if contactout_key and linkedin_url:
        try:
            client = ContactOutClient(contactout_key)
            api_hits.append(client.contacts(linkedin_url))
        except (EnrichmentAuthError, EnrichmentCreditError):
            raise
        except Exception as exc:
            result["notes"].append(f"ContactOut lookup failed: {exc}")
        time.sleep(polite_delay)

    for hit in api_hits:
        src = hit.get("source", "")
        if not result["work_email"] and hit.get("work_emails"):
            e = hit["work_emails"][0]
            result["work_email"] = e["email"]
            result["work_email_source"] = f"{src} (verified by tool)"
        if not result["personal_email"] and hit.get("personal_emails"):
            e = hit["personal_emails"][0]
            edom = e["email"].split("@")[-1].lower()
            if domain and edom == domain:
                if not result["work_email"]:
                    result["work_email"] = e["email"]
                    result["work_email_source"] = f"{src} (verified by tool)"
            else:
                result["personal_email"] = e["email"]
                result["personal_email_source"] = f"{src} (verified by tool)"
        if not result["work_phone"] and not result["personal_phone"] and hit.get("phones"):
            p = hit["phones"][0]
            result["personal_phone"] = p["phone"]
            result["personal_phone_source"] = f"{src} (type not labeled by tool)"

    if not api_hits and (salesql_key or contactout_key):
        result["notes"].append("No API data returned for this candidate.")

    if not result["work_email"]:
        if not domain:
            result["notes"].append(
                "Work email not found via tools; company domain unknown so "
                "pattern/website fallback was skipped."
            )
        else:
            website_hits = find_email_on_website(domain, first, last)
            if website_hits:
                result["work_email"] = website_hits[0]
                result["work_email_source"] = "company website (UNVERIFIED — confirm before use)"
                if len(website_hits) > 1:
                    result["notes"].append(
                        f"Other name-matching emails on site: {', '.join(website_hits[1:3])}"
                    )
            else:
                guesses = guess_work_emails(first, last, domain)
                if guesses:
                    result["work_email"] = guesses[0]
                    result["work_email_source"] = (
                        "pattern-guessed (UNVERIFIED — do not use without verification)"
                    )
                    if len(guesses) > 1:
                        result["notes"].append(
                            f"Other common patterns to try: {', '.join(guesses[1:4])}"
                        )
                else:
                    result["notes"].append("Work email not found; could not build a pattern.")

    result["notes"] = "; ".join(result["notes"])
    return result


def enrich_list(candidates: List[Dict], salesql_key=None, contactout_key=None):
    """Generator yielding (index, candidate, result) for progress display."""
    for i, cand in enumerate(candidates):
        yield i, cand, enrich_candidate(cand, salesql_key, contactout_key)
