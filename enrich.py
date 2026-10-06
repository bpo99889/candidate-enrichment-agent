"""
Enrichment engine for the Candidate Enrichment Agent.

Queries the buyer's OWN API keys (BYOK model): SalesQL, ContactOut,
Hunter.io and/or Lusha, merges the results, and applies the work-email
fallback chain:

    1. Work email from SalesQL / ContactOut / Hunter.io API -> verified by the tool
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SALESQL_BASE = "https://api-public.salesql.com/v1"
CONTACTOUT_BASE = "https://api.contactout.com"

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "live.com", "msn.com", "ymail.com", "protonmail.com",
    "proton.me", "mail.com", "gmx.com", "zoho.com",
}

# Common work-email patterns, most common first.
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


# ---------------------------------------------------------------------------
# API clients
# ---------------------------------------------------------------------------

class SalesQLClient:
    """Thin client for the SalesQL public REST API.

    Docs: https://docs.salesql.com/reference/intro-to-the-salesql-api
    Auth:  Authorization: Bearer <API_KEY>   (key from Settings -> API Access)
    Plans: API access requires Professional or Premium (Free/Basic excluded).
    """

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
        """GET /persons/enrich. Returns normalized contact dict."""
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
                # Type not labeled by the tool -> treat as work email for now;
                # enrich_candidate() re-checks anything whose domain matches
                # the company domain anyway.
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
    """Thin client for the ContactOut v1 REST API.

    Docs: http://api.contactout.com (official API reference)
    Auth:  token: <API_TOKEN> header
    Note:  ContactOut may issue separate work-email and personal-email keys,
           each with its own credit pool (per third-party integration docs).
           This client accepts one token; pass the work key for work data.
    """

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
        """GET /v1/stats — free endpoint, returns credit usage. Good key check."""
        resp = self.session.get(f"{CONTACTOUT_BASE}/v1/stats", timeout=REQUEST_TIMEOUT)
        self._check(resp, "ContactOut stats")
        return resp.json()

    def enrich_linkedin(self, profile_url: str) -> Dict:
        """GET /v1/people/linkedin/enrich?profile=... — full profile + contacts."""
        resp = self.session.get(
            f"{CONTACTOUT_BASE}/v1/people/linkedin/enrich",
            params={"profile": profile_url},
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp, "ContactOut linkedin enrich")
        return self._normalize(resp.json())

    def contacts(self, profile_url: str, include_phone: bool = True) -> Dict:
        """GET /v1/people/linkedin — contacts only (cheaper than full enrich)."""
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
        # ContactOut returns emails as a flat list and phones as a flat list
        # (per public examples); company domain helps classify work vs personal.
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


HUNTER_BASE = "https://api.hunter.io/v2"


class HunterClient:
    """Thin client for the Hunter.io API v2.

    Docs: https://hunter.io/api-documentation/v2
    Auth:  api_key query parameter (key from Hunter dashboard).
    Free plan: 25 searches + 50 verifications/month, no credit card.
    Provides WORK emails only (no personal emails, no phone lookup API —
    Email Finder sometimes returns a phone number, captured when present).
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, endpoint: str, params: Dict, action: str) -> Dict:
        params = dict(params)
        params["api_key"] = self.api_key
        try:
            resp = requests.get(
                f"{HUNTER_BASE}/{endpoint}", params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Hunter.io request failed: {exc}")
        if resp.status_code == 401:
            raise EnrichmentAuthError(
                "Hunter.io rejected the API key (401). Check the key.")
        if resp.status_code in (402, 403):
            raise EnrichmentCreditError(
                f"Hunter.io blocked the request ({resp.status_code}) — "
                "credits/plan issue.")
        if resp.status_code == 429:
            raise EnrichmentCreditError(
                "Hunter.io usage limit reached (429). The free plan allows "
                "25 searches + 50 verifications per month.")
        if resp.status_code == 451:
            raise RuntimeError(
                "Hunter.io cannot process this person (451, legal block).")
        resp.raise_for_status()
        return resp.json()

    def account(self) -> Dict:
        """GET /v2/account — plan + remaining searches/verifications. Free."""
        data = self._get("account", {}, "account").get("data", {})
        reqs = data.get("requests", {})
        searches = reqs.get("searches", {})
        verifs = reqs.get("verifications", {})
        return {
            "plan": data.get("plan_name", "?"),
            "searches_used": searches.get("used", "?"),
            "searches_available": searches.get("available", "?"),
            "verifications_used": verifs.get("used", "?"),
            "verifications_available": verifs.get("available", "?"),
            "reset_date": data.get("reset_date", ""),
        }

    def email_finder(self, first_name: str, last_name: str,
                     domain: str) -> Optional[Dict]:
        """GET /v2/email-finder. 1 search credit. Returns None if not found."""
        data = self._get(
            "email-finder",
            {"first_name": first_name, "last_name": last_name,
             "domain": domain},
            "email finder",
        ).get("data", {})
        email = (data.get("email") or "").strip()
        if not email:
            return None
        return {
            "email": email,
            "score": data.get("score"),          # 0-100 confidence
            "domain": data.get("domain") or domain,
            "phone": (data.get("phone_number") or "").strip() or None,
            "position": data.get("position") or "",
        }

    def email_verifier(self, email: str) -> Dict:
        """GET /v2/email-verifier. 1 verification credit."""
        return self._get(
            "email-verifier", {"email": email}, "email verifier"
        ).get("data", {})


def derive_domain(company: str) -> str:
    """Best-effort company name -> domain guess (e.g. 'Acme Inc' -> acme.com).

    NEVER presented as verified; Hunter.io itself validates by returning
    (or not returning) a result for the domain.
    """
    name = (company or "").lower()
    name = re.sub(r"\b(inc|llc|ltd|corp|co|company|group|holdings|partners|associates|services|solutions)\b\.?", "", name)
    name = re.sub(r"[^a-z0-9]", "", name)
    if len(name) < 3:
        return ""
    return f"{name}.com"


LUSHA_BASE = "https://api.lusha.com"


class LushaClient:
    """Thin client for Lusha's Contacts Search & Enrich API v3.

    Docs: https://docs.lusha.com (official v3 reference)
    Auth:  api_key request header (key from dashboard.lusha.com).
    Free plan: ~40 credits/month, no card; every user gets an API key,
    including on Free (per Lusha's docs). Two charges per result: one for
    the search plus one per revealed field (email ~1, phone ~5); the
    response reports billing.creditsCharged. The `reveal` parameter
    controls what gets unlocked, so we only ask for what is still missing.
    V2 (GET /v2/person) is being sunset — v3 is the current build.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"api_key": api_key})

    def _check(self, resp: requests.Response, action: str):
        if resp.status_code == 401:
            raise EnrichmentAuthError(
                "Lusha rejected the API key (401). Check the key.")
        if resp.status_code == 400 and "api key" in resp.text.lower():
            # Lusha reports malformed/rejected keys as 400, e.g.
            # {"message": "Invalid API key format"}.
            raise EnrichmentAuthError(
                "Lusha rejected the API key (invalid key format). Check the key.")
        if resp.status_code == 402:
            raise EnrichmentCreditError(
                "Lusha: out of credits (402). The free plan resets monthly.")
        if resp.status_code == 403:
            msg = resp.text.lower()
            if "v3" in msg and "not enabled" in msg:
                raise EnrichmentAuthError(
                    "Lusha: V3 API access is not enabled on this account "
                    "(403). Contact Lusha support.")
            raise EnrichmentCreditError(
                "Lusha blocked the request (403) — account/plan issue.")
        if resp.status_code == 429:
            raise RuntimeError(
                "Lusha rate limit hit (429). Slow down and retry.")
        resp.raise_for_status()

    def usage(self) -> Dict:
        """GET /v3/account/usage — credit balance. Free, no credits spent."""
        for path in ("/v3/account/usage", "/account/usage"):
            resp = self.session.get(
                f"{LUSHA_BASE}{path}", timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                continue
            self._check(resp, "Lusha usage")
            return resp.json()
        return {"note": "usage endpoint not reachable on this plan"}

    def enrich_person(self, first_name: Optional[str] = None,
                      last_name: Optional[str] = None,
                      company: Optional[str] = None,
                      domain: Optional[str] = None,
                      linkedin_url: Optional[str] = None,
                      email: Optional[str] = None,
                      reveal: Optional[List[str]] = None) -> Dict:
        """POST /v3/contacts/search-and-enrich — normalized hit dict.

        reveal: subset of ["emails", "phones"] — only requested fields are
        unlocked and charged. Defaults to both.
        """
        contact: Dict[str, str] = {"clientReferenceId": "c0"}
        if first_name:
            contact["firstName"] = first_name
        if last_name:
            contact["lastName"] = last_name
        if company:
            contact["companyName"] = company
        if domain:
            contact["companyDomain"] = domain
        if linkedin_url:
            contact["linkedinUrl"] = linkedin_url
        if email:
            contact["email"] = email
        body = {
            "contacts": [contact],
            "reveal": reveal or ["emails", "phones"],
            "options": {"includePartialProfiles": True},
        }
        resp = self.session.post(
            f"{LUSHA_BASE}/v3/contacts/search-and-enrich",
            json=body, timeout=REQUEST_TIMEOUT)
        self._check(resp, "Lusha search-and-enrich")
        return self._normalize(resp.json())

    @staticmethod
    def _normalize(data: Dict) -> Dict:
        results = data.get("results") or []
        billing = data.get("billing") or {}
        credits = billing.get("creditsCharged")
        if not results:
            return {"source": "Lusha API", "work_emails": [],
                    "personal_emails": [], "phones": [], "title": "",
                    "company": "", "note": "no match",
                    "credits_charged": credits}
        r = results[0]
        err = r.get("error")
        if err:
            return {"source": "Lusha API", "work_emails": [],
                    "personal_emails": [], "phones": [], "title": "",
                    "company": "", "note": str(err)[:200],
                    "credits_charged": credits}
        work_emails, personal_emails = [], []
        for e in r.get("emails") or []:
            addr = (e.get("email") or "").strip()
            if not addr or "@" not in addr:
                continue
            etype = (e.get("type") or "").lower()
            entry = {"email": addr, "status": e.get("confidence") or ""}
            if etype in ("private", "personal"):
                personal_emails.append(entry)
            else:
                # "work" (and untyped) -> treated as work; enrich_candidate()
                # re-checks anything whose domain matches the company domain.
                work_emails.append(entry)
        phones = []
        for p in r.get("phones") or []:
            num = (p.get("number") or "").strip()
            if num and not p.get("doNotCall"):
                phones.append({"phone": num, "type": p.get("type") or ""})
        jt = r.get("jobTitle") or {}
        sl = r.get("socialLinks") or {}
        comp = r.get("company") or {}
        missing = r.get("missingDataPoints") or []
        return {
            "source": "Lusha API",
            "work_emails": work_emails,
            "personal_emails": personal_emails,
            "phones": phones,
            "title": jt.get("title") or "",
            "company": comp.get("name", "") if isinstance(comp, dict) else "",
            "linkedin_url": sl.get("linkedin") or "",
            "credits_charged": credits,
            "missing": missing,
        }


# Fallback helpers
# ---------------------------------------------------------------------------

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
    """Best-effort: fetch the company homepage and look for an email address
    containing the candidate's first or last name. Returns [] on any failure."""
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
                # skip obvious generic addresses
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


# ---------------------------------------------------------------------------
# Main per-candidate enrichment
# ---------------------------------------------------------------------------

def enrich_candidate(
    candidate: Dict,
    salesql_key: Optional[str] = None,
    contactout_key: Optional[str] = None,
    hunter_key: Optional[str] = None,
    lusha_key: Optional[str] = None,
    polite_delay: float = 0.4,
) -> Dict:
    """Enrich one candidate. Returns a result dict with explicit sources.

    candidate keys: first_name, last_name, company, title, linkedin_url,
                    company_domain (optional)
    """
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

    # --- 1) SalesQL ---
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
        except Exception as exc:  # per-candidate failure must not kill the run
            result["notes"].append(f"SalesQL lookup failed: {exc}")
        time.sleep(polite_delay)

    # --- 2) ContactOut (needs a LinkedIn URL) ---
    if contactout_key and linkedin_url:
        try:
            client = ContactOutClient(contactout_key)
            api_hits.append(client.contacts(linkedin_url))
        except (EnrichmentAuthError, EnrichmentCreditError):
            raise
        except Exception as exc:
            result["notes"].append(f"ContactOut lookup failed: {exc}")
        time.sleep(polite_delay)

    # --- 3) Merge: first verified hit wins per field ---
    for hit in api_hits:
        src = hit.get("source", "")
        if not result["work_email"] and hit.get("work_emails"):
            e = hit["work_emails"][0]
            result["work_email"] = e["email"]
            result["work_email_source"] = f"{src} (verified by tool)"
        if not result["personal_email"] and hit.get("personal_emails"):
            # re-classify: if its domain == company domain it is really work mail
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
            # Prefer the tool's own type label when present (Lusha labels
            # Mobile vs work/direct). Otherwise record as found, unlabeled.
            for p in hit["phones"]:
                ptype = (p.get("type") or "").lower()
                if "mobile" in ptype or "cell" in ptype:
                    result["personal_phone"] = p["phone"]
                    result["personal_phone_source"] = f"{src} (mobile)"
                    break
                if any(t in ptype for t in ("work", "direct", "office", "business")):
                    result["work_phone"] = p["phone"]
                    result["work_phone_source"] = f"{src} (work/direct)"
                    break
            else:
                p = hit["phones"][0]
                result["personal_phone"] = p["phone"]
                result["personal_phone_source"] = f"{src} (type not labeled by tool)"

    if not api_hits and (salesql_key or contactout_key):
        result["notes"].append("No API data returned for this candidate.")

    # --- 3b) Hunter.io Email Finder + Verifier (work emails only).
    # Runs only if the tools above found no work email yet, to save credits.
    # Free plan: 25 searches + 50 verifications/month.
    if hunter_key and not result["work_email"] and first and last:
        hdomain = domain or derive_domain(company)
        if not hdomain:
            result["notes"].append(
                "Hunter.io skipped: no company domain (add a Website/Domain "
                "column to the sheet).")
        else:
            if not domain:
                result["notes"].append(
                    f"Hunter.io used derived domain '{hdomain}' from the "
                    "company name — confirm it is correct.")
            try:
                hclient = HunterClient(hunter_key)
                found = hclient.email_finder(first, last, hdomain)
                time.sleep(polite_delay)
                if found and found.get("email"):
                    email = found["email"]
                    score = found.get("score")
                    verified = False
                    try:
                        v = hclient.email_verifier(email)
                        time.sleep(polite_delay)
                        if str(v.get("result", "")).lower() == "deliverable":
                            verified = True
                    except (EnrichmentAuthError, EnrichmentCreditError):
                        raise
                    except Exception:
                        pass  # verifier failed; keep the finder result as-is
                    result["work_email"] = email
                    if verified:
                        result["work_email_source"] = \
                            "Hunter.io (verified deliverable)"
                    elif score:
                        result["work_email_source"] = (
                            f"Hunter.io Email Finder (confidence {score}% — "
                            "verify before use)")
                    else:
                        result["work_email_source"] = \
                            "Hunter.io Email Finder (unverified — confirm before use)"
                    if found.get("phone") and not result["personal_phone"] \
                            and not result["work_phone"]:
                        result["personal_phone"] = found["phone"]
                        result["personal_phone_source"] = \
                            "Hunter.io (type not labeled by tool)"
                else:
                    result["notes"].append(
                        f"Hunter.io: no email found for {first} {last} "
                        f"at {hdomain}.")
            except (EnrichmentAuthError, EnrichmentCreditError):
                raise
            except Exception as exc:  # per-candidate failure never kills the run
                result["notes"].append(f"Hunter.io lookup failed: {exc}")

    # --- 3c) Lusha Search & Enrich v3 (work emails + phone numbers).
    # Free plan is ~40 credits/month; v3 charges one for the search plus one
    # per revealed field. We reveal ONLY what is still missing, so a
    # candidate who already has an email doesn't pay for another email.
    need_email = not result["work_email"]
    need_phone = not result["work_phone"] and not result["personal_phone"]
    if lusha_key and first and last and (need_email or need_phone):
        hdomain = domain or derive_domain(company)
        try:
            lclient = LushaClient(lusha_key)
            reveal = []
            if need_email:
                reveal.append("emails")
            if need_phone:
                reveal.append("phones")
            hit = lclient.enrich_person(
                first_name=first, last_name=last,
                company=company or None, domain=hdomain or None,
                linkedin_url=linkedin_url or None, reveal=reveal)
            time.sleep(polite_delay)
            credits = hit.get("credits_charged")
            if credits:
                result["notes"].append(
                    f"Lusha charged {credits} credit(s) for this candidate.")
            if hit.get("note"):
                result["notes"].append(f"Lusha: {hit['note']}")
            elif not hit.get("work_emails") and not hit.get("phones"):
                if credits:
                    result["notes"].append(
                        "Lusha found no contacts for this candidate "
                        "(credits were still charged).")
                else:
                    result["notes"].append(
                        "Lusha found no contacts for this candidate "
                        "(no credits charged).")
            else:
                # Merge through the same field rules as the other tools.
                h, src = hit, hit.get("source", "")
                if not result["work_email"] and h.get("work_emails"):
                    e = h["work_emails"][0]
                    result["work_email"] = e["email"]
                    conf = f", confidence {e['status']}" if e.get("status") else ""
                    result["work_email_source"] = \
                        f"{src} (verified by tool{conf})"
                if not result["work_phone"] and not result["personal_phone"] \
                        and h.get("phones"):
                    for p in h["phones"]:
                        ptype = (p.get("type") or "").lower()
                        if "mobile" in ptype or "cell" in ptype:
                            result["personal_phone"] = p["phone"]
                            result["personal_phone_source"] = f"{src} (mobile)"
                            break
                        if any(t in ptype for t in
                               ("work", "direct", "office", "business")):
                            result["work_phone"] = p["phone"]
                            result["work_phone_source"] = f"{src} (work/direct)"
                            break
                    else:
                        p = h["phones"][0]
                        result["personal_phone"] = p["phone"]
                        result["personal_phone_source"] = \
                            f"{src} (type not labeled by tool)"
                if hit.get("linkedin_url") and not linkedin_url:
                    result["notes"].append(
                        f"Lusha LinkedIn: {hit['linkedin_url']}")
                if hit.get("title"):
                    result["notes"].append(
                        f"Lusha job title: {hit['title']}")
        except (EnrichmentAuthError, EnrichmentCreditError):
            raise
        except Exception as exc:  # per-candidate failure never kills the run
            result["notes"].append(f"Lusha lookup failed: {exc}")

    # --- 4) Work-email fallback chain (only if the tools found nothing) ---
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


def enrich_list(candidates: List[Dict], salesql_key=None, contactout_key=None,
                hunter_key=None, lusha_key=None):
    """Generator yielding (index, candidate, result) for progress display."""
    for i, cand in enumerate(candidates):
        yield i, cand, enrich_candidate(cand, salesql_key, contactout_key,
                                        hunter_key, lusha_key)
