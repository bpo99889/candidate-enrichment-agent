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
from urllib.parse import unquote, urlparse

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

    def domain_search(self, domain: str) -> Optional[Dict]:
        """GET /v2/domain-search — the domain's email directory: known
        emails + the company's email pattern. 1 search credit.
        Returns None if the domain has no usable data."""
        data = self._get(
            "domain-search", {"domain": domain, "limit": 10},
            "domain search",
        ).get("data", {})
        pattern = (data.get("pattern") or "").strip()
        emails = data.get("emails") or []
        if not pattern and not emails:
            return None
        return {"pattern": pattern,
                "emails": [e.get("value") for e in emails if e.get("value")]}


def _ddg_domains(query: str, limit: int = 3) -> List[str]:
    """One DuckDuckGo HTML search, returning distinct domains."""
    r = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=15)
    if r.status_code != 200 or not r.text:
        return []
    urls = re.findall(r'href="//duckduckgo\.com/l/\?uddg=([^"&]+)', r.text)
    return _clean_domains(urls, limit)


def _bing_domains(query: str, limit: int = 3) -> List[str]:
    """One Bing HTML search (backup when DuckDuckGo flakes), same shape."""
    r = requests.get(
        "https://www.bing.com/search",
        params={"q": query, "format": "rss"},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=15)
    if r.status_code != 200 or not r.text:
        return []
    urls = re.findall(r"<link>(https?://[^<]+)</link>", r.text)
    return _clean_domains(urls, limit, unquote_url=False)


def _clean_domains(urls: List[str], limit: int,
                   unquote_url: bool = True) -> List[str]:
    skip = ("linkedin.com", "facebook.com", "instagram.com", "twitter.com",
            "x.com", "youtube.com", "wikipedia.org", "crunchbase.com",
            "bloomberg.com", "zoominfo.com", "glassdoor.com", "indeed.com")
    seen: List[str] = []
    for u in urls:
        try:
            raw = unquote(u) if unquote_url else u
            netloc = urlparse(raw).netloc.lower().split(":")[0]
        except Exception:
            continue
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if not netloc or "." not in netloc or netloc in seen:
            continue
        if any(b in netloc for b in skip):
            continue
        seen.append(netloc)
        if len(seen) >= limit:
            break
    return seen


def _search_company_domains(company: str, limit: int = 3) -> List[str]:
    """Free web search (no key) for a company's official website.

    Retries DuckDuckGo (it flakes), then tries Bing as backup.
    Returns up to `limit` distinct domains, best first. [] on total failure.
    """
    company = (company or "").strip()
    if not company:
        return []
    query = f"{company} official website"
    for attempt in range(3):
        try:
            found = _ddg_domains(query, limit)
            if found:
                return found
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
    try:
        found = _bing_domains(query, limit)
        if found:
            return found
    except Exception:
        pass
    return []


def discover_domain(company: str, hunter_key: Optional[str] = None,
                    cache: Optional[Dict[str, str]] = None) -> str:
    """Find a company's REAL email domain when the sheet has none.

    1) Free web search for the official website (no credits).
    2) Ask Hunter which candidate domain actually has email data
       (1 search credit per domain checked, stops at the first hit).
    3) Fall back to the top search result, then to the name-guess.
    Results are cached per company for the run. Returns "" if nothing found.
    """
    if cache is None:
        cache = {}
    key = (company or "").strip().lower()
    if key in cache:
        return cache[key]
    domain = ""
    candidates = _search_company_domains(company)
    if hunter_key and candidates:
        try:
            hc = HunterClient(hunter_key)
            for d in candidates[:2]:  # check top 2 only, save credits
                try:
                    ds = hc.domain_search(d)
                    time.sleep(0.4)
                    if ds and (ds.get("emails") or ds.get("pattern")):
                        domain = d
                        break
                except (EnrichmentAuthError, EnrichmentCreditError):
                    raise
                except Exception:
                    continue
        except (EnrichmentAuthError, EnrichmentCreditError):
            raise
        except Exception:
            pass
    if not domain and candidates:
        domain = candidates[0]
    if not domain:
        domain = derive_domain(company)
    cache[key] = domain
    return domain


def domain_has_mx(domain: str) -> bool:
    """Free DNS check: does this domain have mail servers? Used to avoid
    building emails on a wrong/guessed domain. Fail-open: if the DNS check
    itself errors, returns True so a network hiccup doesn't block results."""
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return False
    try:
        resp = requests.get(
            "https://dns.google/resolve",
            params={"name": domain, "type": "MX"},
            timeout=8, headers={"Accept": "application/json"})
        return bool(resp.json().get("Answer"))
    except Exception:
        return True


def _email_matches_name(email: str, first_name: str, last_name: str) -> bool:
    """Does this email's local part look like it belongs to this person?
    Used to spot the candidate's own address in a directory listing."""
    local = (email or "").split("@")[0].lower()
    first = re.sub(r"[^a-z]", "", (first_name or "").lower())
    last = re.sub(r"[^a-z]", "", (last_name or "").lower())
    if not (local and first and last):
        return False
    return (first in local and last in local) or \
           (first[:1] + last in local) or (first + last[:1] in local)


def build_from_pattern(first_name: str, last_name: str,
                       pattern: str, domain: str) -> str:
    """Build an email from Hunter's directory pattern, e.g. '{first}.{last}'
    -> 'jane.doe@acme.com'. Returns '' if the pattern is unusable."""
    first = re.sub(r"[^a-z]", "", (first_name or "").lower())
    last = re.sub(r"[^a-z]", "", (last_name or "").lower())
    domain = (domain or "").strip().lower()
    if not (first and last and pattern and domain and "." in domain):
        return ""
    local = pattern.lower()
    local = local.replace("{first}", first).replace("{last}", last)
    local = local.replace("{f}", first[:1]).replace("{l}", last[:1])
    local = local.replace("{first_initial}", first[:1])
    local = local.replace("{last_initial}", last[:1])
    if "{" in local or "}" in local:
        return ""  # unreplaced placeholder -> pattern unusable
    local = re.sub(r"[^a-z0-9._-]", "", local)
    if not local:
        return ""
    return f"{local}@{domain}"


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


FULLENRICH_BASE = "https://app.fullenrich.com/api/v1"


class FullEnrichClient:
    """Thin client for FullEnrich's Contact Enrichment API v1.

    Docs: https://docs.fullenrich.com (official v1 reference)
    Auth:  Authorization: Bearer <api_key> (from app.fullenrich.com/app/settings/api).
    Free plan: 50 credits, no card. Credit costs: work email 1, personal
    email 3, mobile phone 10. No result found = 0 credits (not charged).
    The bulk endpoint is async: POST returns an enrichment_id, then poll
    GET /contact/enrich/bulk/{id} until status is FINISHED.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _check(self, resp: requests.Response, action: str):
        if resp.status_code == 401:
            raise EnrichmentAuthError(
                "FullEnrich rejected the API key (401). Check the key.")
        if resp.status_code == 402:
            raise EnrichmentCreditError(
                "FullEnrich: out of credits (402).")
        if resp.status_code == 429:
            raise RuntimeError(
                "FullEnrich rate limit hit (429). Slow down and retry.")
        resp.raise_for_status()

    def verify(self) -> Dict:
        """GET /account/keys/verify — confirms the key is valid. Free."""
        resp = self.session.get(
            f"{FULLENRICH_BASE}/account/keys/verify", timeout=REQUEST_TIMEOUT)
        self._check(resp, "FullEnrich verify")
        return resp.json()

    def credits(self) -> Dict:
        """GET /account/credits — credit balance. Free, no credits spent."""
        resp = self.session.get(
            f"{FULLENRICH_BASE}/account/credits", timeout=REQUEST_TIMEOUT)
        self._check(resp, "FullEnrich credits")
        return resp.json()

    def enrich_one(self, first_name: str, last_name: str,
                   domain: str = "", company_name: str = "",
                   linkedin_url: str = "",
                   want_work_email: bool = True,
                   want_personal_email: bool = False,
                   want_phone: bool = False,
                   poll_timeout: int = 120) -> Dict:
        """Enrich a single contact via bulk endpoint + polling.

        Returns normalized dict with keys: work_email, work_email_status,
        personal_email, phones (list). Empty dict if no result.
        """
        enrich_fields = []
        if want_work_email:
            enrich_fields.append("contact.work_emails")
        if want_personal_email:
            enrich_fields.append("contact.personal_emails")
        if want_phone:
            enrich_fields.append("contact.phones")
        if not enrich_fields:
            return {}

        contact: Dict[str, Any] = {
            "firstname": first_name,
            "lastname": last_name,
            "enrich_fields": enrich_fields,
        }
        if domain:
            contact["domain"] = domain
        if company_name:
            contact["company_name"] = company_name
        if linkedin_url:
            contact["linkedin_url"] = linkedin_url

        payload = {
            "name": f"{first_name} {last_name}".strip() or "enrichment",
            "datas": [contact],
        }
        resp = self.session.post(
            f"{FULLENRICH_BASE}/contact/enrich/bulk?silentFail=true",
            json=payload, timeout=REQUEST_TIMEOUT)
        self._check(resp, "FullEnrich bulk enrich")
        enrichment_id = resp.json().get("enrichment_id")
        if not enrichment_id:
            return {}

        # Poll for results
        import time as _time
        deadline = _time.time() + poll_timeout
        while _time.time() < deadline:
            _time.sleep(5)
            poll = self.session.get(
                f"{FULLENRICH_BASE}/contact/enrich/bulk/{enrichment_id}",
                timeout=REQUEST_TIMEOUT)
            # 400 while still in progress — keep waiting
            if poll.status_code == 400:
                continue
            self._check(poll, "FullEnrich poll results")
            data = poll.json()
            status = data.get("status", "")
            if status == "FINISHED":
                return self._parse_result(data)
            if status in ("CANCELED", "CREDITS_INSUFFICIENT", "RATE_LIMIT",
                          "UNKNOWN"):
                if status == "CREDITS_INSUFFICIENT":
                    raise EnrichmentCreditError(
                        "FullEnrich: out of credits during enrichment.")
                return {"_status": status}
            # CREATED / IN_PROGRESS — keep polling
        return {"_status": "TIMEOUT"}

    def _parse_result(self, data: Dict) -> Dict:
        """Extract work/personal emails and phones from FINISHED response."""
        out: Dict[str, Any] = {"work_email": "", "work_email_status": "",
                               "personal_email": "", "phones": []}
        datas = data.get("datas") or data.get("data") or []
        if not datas:
            return out
        record = datas[0]
        contact_info = record.get("contact_info") or record.get("contact") or {}

        # Work emails — prefer most probable, fall back to list
        mp = contact_info.get("most_probable_work_email") or {}
        if isinstance(mp, dict) and mp.get("email"):
            out["work_email"] = mp["email"]
            out["work_email_status"] = mp.get("status", "")
        else:
            work_emails = contact_info.get("work_emails") or []
            if work_emails:
                first = work_emails[0]
                if isinstance(first, dict):
                    out["work_email"] = first.get("email", "")
                    out["work_email_status"] = first.get("status", "")
                else:
                    out["work_email"] = str(first)

        # Personal emails
        mp_pers = contact_info.get("most_probable_personal_email") or {}
        if isinstance(mp_pers, dict) and mp_pers.get("email"):
            out["personal_email"] = mp_pers["email"]
        else:
            pers_emails = contact_info.get("personal_emails") or []
            if pers_emails:
                first = pers_emails[0]
                out["personal_email"] = (first.get("email", "")
                                         if isinstance(first, dict)
                                         else str(first))

        # Phones
        mp_phone = contact_info.get("most_probable_phone") or {}
        if isinstance(mp_phone, dict) and mp_phone.get("number"):
            out["phones"] = [mp_phone["number"]]
        else:
            phones = contact_info.get("phones") or []
            out["phones"] = [
                p.get("number", "") if isinstance(p, dict) else str(p)
                for p in phones if p
            ]
        return out


REOON_BASE = "https://emailverifier.reoon.com/api/v1"


class ReoonClient:
    """Thin client for Reoon Email Verifier API v1.

    Docs: https://www.reoon.com/articles/api-documentation-of-reoon-email-verifier/
    Auth:  API key as `key` query param (from emailverifier.reoon.com dashboard).
    Free tier: ~20 credits/day, up to 600/month, no card.
    Modes: `quick` (~0.5s, syntax/MX/disposable) or `power` (deep SMTP,
    inbox existence, catch-all detection — slower but most accurate).
    Statuses: safe, invalid, disabled, disposable, inbox_full, catch_all,
    role_account, spamtrap, unknown.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def _check(self, resp: requests.Response, action: str):
        if resp.status_code == 401:
            raise EnrichmentAuthError(
                "Reoon rejected the API key (401). Check the key.")
        if resp.status_code == 402:
            raise EnrichmentCreditError(
                "Reoon: out of credits (402). Free tier resets daily/monthly.")
        if resp.status_code == 429:
            raise RuntimeError(
                "Reoon rate limit hit (429). Slow down and retry.")
        resp.raise_for_status()

    def account_info(self) -> Dict:
        """GET /get-account-info — credit balance. Free, no credits spent."""
        resp = self.session.get(
            f"{REOON_BASE}/get-account-info",
            params={"key": self.api_key}, timeout=REQUEST_TIMEOUT)
        self._check(resp, "Reoon account info")
        return resp.json()

    def verify(self, email: str, mode: str = "power") -> Dict:
        """GET /verify — verify a single email.

        mode: "quick" or "power" (default power for deepest check).
        Returns the raw JSON: status, overall_score, is_safe_to_send, etc.
        """
        resp = self.session.get(
            f"{REOON_BASE}/verify",
            params={"email": email, "key": self.api_key, "mode": mode},
            timeout=60)  # power mode can take a few seconds
        self._check(resp, "Reoon verify")
        return resp.json()


def find_email_on_website(domain: str, first_name: str, last_name: str) -> List[str]:
    """Best-effort: fetch the company homepage AND common team/contact pages
    (where companies often list employee emails) and look for an email
    address containing the candidate's first or last name.
    Returns [] on any failure."""
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return []
    first = (first_name or "").lower()
    last = (last_name or "").lower()
    if not (first or last):
        return []
    base = domain if domain.startswith("http") else f"https://{domain}"
    # Homepage + pages where employee emails are commonly listed
    pages = ["", "/contact", "/contact-us", "/about", "/about-us",
             "/team", "/our-team", "/staff", "/people", "/leadership",
             "/management", "/company", "/our-people"]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CandidateEnrichmentAgent/1.0)"}
    found = []
    for page in pages:
        try:
            resp = requests.get(base + page, timeout=REQUEST_TIMEOUT,
                                headers=headers)
            if resp.status_code != 200 or not resp.text:
                continue
            for m in set(EMAIL_RE.findall(resp.text)):
                ml = m.lower()
                if (first and first in ml) or (last and last in ml):
                    # skip obvious generic addresses
                    local = ml.split("@")[0]
                    if local not in {"info", "contact", "support", "sales",
                                     "hello", "admin", "careers", "jobs",
                                     "press", "media", "hr"}:
                        if m not in found:
                            found.append(m)
            if found:
                break  # stop once we find name-matching emails
        except Exception:
            continue
    return sorted(set(found))


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
    fullenrich_key: Optional[str] = None,
    polite_delay: float = 0.4,
    _domain_cache: Optional[Dict[str, str]] = None,
    sheet_type: str = "maximum",
) -> Dict:
    """Enrich one candidate. Returns a result dict with explicit sources.

    candidate keys: first_name, last_name, company, title, linkedin_url,
                    company_domain (optional)
    sheet_type: "professional" (work email required — Hunter first, all
                fallbacks), "personal" (personal contacts first — skips
                Hunter work-email finder to save credits, work email is
                bonus), "maximum" (everything, default).
    """
    first = (candidate.get("first_name") or "").strip()
    last = (candidate.get("last_name") or "").strip()
    company = (candidate.get("company") or "").strip()
    linkedin_url = (candidate.get("linkedin_url") or "").strip()
    domain = clean_domain(candidate.get("company_domain") or "")
    domain_from_sheet = bool(domain)
    if not domain and company:
        # No website in the sheet: find the company's real domain
        # automatically (web search + Hunter validation) instead of guessing
        # it from the company name.
        try:
            domain = discover_domain(company, hunter_key, _domain_cache)
        except (EnrichmentAuthError, EnrichmentCreditError):
            raise
        except Exception:
            domain = derive_domain(company)
        if domain:
            result_notes = [f"Company website auto-found: {domain}"]
        else:
            result_notes = []
    else:
        result_notes = []

    result = {
        "personal_email": "", "personal_email_source": "",
        "personal_phone": "", "personal_phone_source": "",
        "work_email": "", "work_email_source": "",
        "work_phone": "", "work_phone_source": "",
        "notes": result_notes,
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
            # Phone routing per source (user's rules):
            # - SalesQL: ONLY "direct" numbers are taken, all go to the
            #   personal number. No work phone ever comes from SalesQL.
            # - ContactOut: ALL given numbers go to the personal (mobile)
            #   number, regardless of label.
            # - Others: mobile/cell/unlabeled -> personal, work/direct/
            #   office -> work. Multiple numbers are joined.
            mobiles, works = [], []
            slow = src.lower()
            salesql = "salesql" in slow
            contactout = "contactout" in slow
            for p in hit["phones"]:
                ptype = (p.get("type") or "").lower()
                num = p["phone"]
                if salesql:
                    if "direct" in ptype and num not in mobiles:
                        mobiles.append(num)
                elif contactout:
                    if num not in mobiles:
                        mobiles.append(num)
                elif any(t in ptype for t in
                         ("work", "direct", "office", "business")):
                    if num not in works:
                        works.append(num)
                elif num not in mobiles:
                    mobiles.append(num)
            if mobiles:
                result["personal_phone"] = "; ".join(mobiles)
                result["personal_phone_source"] = f"{src} (mobile)"
            if works:
                result["work_phone"] = "; ".join(works)
                result["work_phone_source"] = f"{src} (work/direct)"

    if not api_hits and (salesql_key or contactout_key):
        result["notes"].append("No API data returned for this candidate.")

    # --- 3b) Hunter.io Email Finder + Verifier (work emails only).
    # Runs only if the tools above found no work email yet, to save credits.
    # Free plan: 25 searches + 50 verifications/month.
    # Skipped for "personal" sheets (work email is bonus there, not required)
    # to save Hunter credits for when they matter.
    if hunter_key and not result["work_email"] and first and last \
            and sheet_type != "personal":
        hdomain = domain
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

    # --- 3b2) FullEnrich waterfall (work emails Hunter missed + personal emails + phones).
    # Runs after Hunter: picks up remaining work emails, personal emails,
    # and phone numbers. Free plan: 50 credits, no card. Costs: work email 1,
    # personal email 3, phone 10. No result = 0 credits.
    # Only asks for what's still missing.
    # PROFESSIONAL sheets: spend credits on work email + work phone only.
    # Personal details are added only if they come back free with the lookup
    # (never spend credits just for personal data on Professional).
    need_fe_work = not result["work_email"]
    need_fe_pers = not result["personal_email"]
    need_fe_phone = not result["personal_phone"] and not result["work_phone"]
    # On Professional, don't PAY for personal emails (3 credits each).
    # We still parse them from the response if they come back free.
    fe_want_pers = need_fe_pers and sheet_type != "professional"
    if fullenrich_key and (need_fe_work or need_fe_pers or need_fe_phone) \
            and first and last:
        try:
            fe = FullEnrichClient(fullenrich_key)
            fe_domain = domain or ""
            fe_result = fe.enrich_one(
                first, last,
                domain=fe_domain,
                company_name=company,
                linkedin_url=linkedin_url,
                want_work_email=need_fe_work,
                want_personal_email=fe_want_pers,
                want_phone=need_fe_phone,
            )
            if fe_result.get("_status"):
                status = fe_result["_status"]
                if status not in ("TIMEOUT",):
                    result["notes"].append(
                        f"FullEnrich: no result ({status}).")
            else:
                fe_work = fe_result.get("work_email", "")
                fe_status = fe_result.get("work_email_status", "")
                if need_fe_work and fe_work:
                    result["work_email"] = fe_work
                    src = "FullEnrich"
                    if fe_status:
                        src += f" [{fe_status}]"
                    result["work_email_source"] = src
                    result["notes"].append(
                        f"FullEnrich found work email: {fe_work} "
                        f"(status: {fe_status or 'unknown'}).")
                fe_pers = fe_result.get("personal_email", "")
                # Add personal email if present — even on Professional where
                # we didn't pay for it, keep it if it came back free.
                if fe_pers and not result["personal_email"]:
                    result["personal_email"] = fe_pers
                    result["notes"].append(
                        f"FullEnrich found personal email: {fe_pers}.")
                fe_phones = fe_result.get("phones") or []
                if need_fe_phone and fe_phones:
                    # Route to personal/mobile (same as ContactOut rule)
                    result["personal_phone"] = "; ".join(fe_phones)
                    result["notes"].append(
                        f"FullEnrich found phone(s): {'; '.join(fe_phones)}.")
                if not fe_work and not fe_pers and not fe_phones:
                    result["notes"].append(
                        "FullEnrich: no work email, personal email, or phone found.")
        except (EnrichmentAuthError, EnrichmentCreditError):
            raise
        except Exception as exc:  # per-candidate failure never kills the run
            result["notes"].append(f"FullEnrich lookup failed: {exc}")

    # --- 3c) Lusha Search & Enrich v3 (work emails + phone numbers).
    # Free plan is ~40 credits/month; v3 charges one for the search plus one
    # per revealed field. We reveal ONLY what is still missing, so a
    # candidate who already has an email doesn't pay for another email.
    need_email = not result["work_email"]
    need_phone = not result["work_phone"] and not result["personal_phone"]
    if lusha_key and first and last and (need_email or need_phone):
        hdomain = domain
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
    # Order: company website -> email directory (Hunter domain search learns
    # the company's real pattern) -> blind pattern guess from the domain.
    # Everything here is UNVERIFIED and labeled as such.
    if not result["work_email"]:
        fbdomain = domain
        if not fbdomain:
            result["notes"].append(
                "Work email not found via tools; company domain unknown so "
                "pattern/website fallback was skipped."
            )
        else:
            if not domain_from_sheet:
                result["notes"].append(
                    f"Company domain '{fbdomain}' was auto-discovered — "
                    f"confirm it is correct.")
            website_hits = find_email_on_website(fbdomain, first, last)
            if website_hits:
                result["work_email"] = website_hits[0]
                result["work_email_source"] = "company website (UNVERIFIED — confirm before use)"
                if len(website_hits) > 1:
                    result["notes"].append(
                        f"Other name-matching emails on site: {', '.join(website_hits[1:3])}"
                    )
            else:
                # Pattern-building needs a real mail domain. If the domain
                # (often derived from the company name) has no mail servers,
                # anything built on it would be invalid — skip and say so.
                mx_ok = domain_has_mx(fbdomain)
                if not mx_ok:
                    result["notes"].append(
                        f"Skipped email construction: '{fbdomain}' has no "
                        f"mail servers (domain may be wrong).")
                directory_email = ""
                directory_found = False
                if hunter_key and mx_ok and sheet_type != "personal":
                    # Directory step: Hunter's domain directory lists real
                    # emails at the company. If the candidate's own email is
                    # in there, that's a FOUND email (green). Otherwise learn
                    # the company's pattern and build it (pink).
                    try:
                        hclient = HunterClient(hunter_key)
                        ds = hclient.domain_search(fbdomain)
                        time.sleep(polite_delay)
                        if ds:
                            for e in ds.get("emails") or []:
                                if _email_matches_name(e, first, last):
                                    directory_email = e
                                    directory_found = True
                                    break
                            if not directory_email and ds.get("pattern"):
                                directory_email = build_from_pattern(
                                    first, last, ds["pattern"], fbdomain)
                            if ds.get("pattern"):
                                result["notes"].append(
                                    f"Hunter directory shows this company uses "
                                    f"pattern '{ds['pattern']}'.")
                    except (EnrichmentAuthError, EnrichmentCreditError):
                        raise
                    except Exception as exc:
                        result["notes"].append(
                            f"Hunter directory lookup failed: {exc}")
                if directory_email:
                    result["work_email"] = directory_email
                    result["work_email_source"] = (
                        "Hunter directory (UNVERIFIED — confirm before use)"
                        if directory_found else
                        "built from company email pattern (UNVERIFIED — "
                        "verify before use)")
                elif mx_ok:
                    guesses = guess_work_emails(first, last, fbdomain)
                    if guesses:
                        result["work_email"] = guesses[0]
                        result["work_email_source"] = (
                            "pattern-guessed from company domain (UNVERIFIED — "
                            "do not use without verification)")
                        if len(guesses) > 1:
                            result["notes"].append(
                                f"Other common patterns to try: {', '.join(guesses[1:4])}"
                            )
                    else:
                        result["notes"].append("Work email not found; could not build a pattern.")

    # --- 4b) Verify any UNVERIFIED work email with Hunter before it ships.
    # If Hunter's verifier says "invalid", drop it — a wrong email is worse
    # than a blank cell. Costs 1 verification credit per email.
    # Skipped for "personal" sheets to save verification credits (work email
    # is bonus there, not required).
    if result["work_email"] and hunter_key and sheet_type != "personal" and \
            "UNVERIFIED" in (result.get("work_email_source") or ""):
        try:
            v = HunterClient(hunter_key).email_verifier(result["work_email"])
            time.sleep(polite_delay)
            vstatus = str(v.get("status", "")).lower()
            if vstatus == "invalid":
                result["notes"].append(
                    f"Dropped {result['work_email']}: Hunter verifier reports "
                    f"it as invalid.")
                result["work_email"] = ""
                result["work_email_source"] = ""
            elif vstatus == "valid":
                result["work_email_source"] += " [Hunter verifier: valid]"
        except (EnrichmentAuthError, EnrichmentCreditError):
            raise
        except Exception as exc:
            result["notes"].append(f"Email verification failed: {exc}")

    result["notes"] = "; ".join(result["notes"])
    return result


def enrich_list(candidates: List[Dict], salesql_key=None, contactout_key=None,
                hunter_key=None, lusha_key=None, fullenrich_key=None,
                sheet_type: str = "maximum"):
    """Generator yielding (index, candidate, result) for progress display.
    sheet_type: "professional" (work emails required), "personal" (personal
    contacts first, work email bonus), or "maximum" (everything)."""
    _domain_cache: Dict[str, str] = {}
    for i, cand in enumerate(candidates):
        yield i, cand, enrich_candidate(cand, salesql_key, contactout_key,
                                        hunter_key, lusha_key,
                                        fullenrich_key=fullenrich_key,
                                        _domain_cache=_domain_cache,
                                        sheet_type=sheet_type)


def verify_emails_with_reoon(enriched_rows: List[Dict], reoon_key: str,
                             mode: str = "power"):
    """Generator yielding (index, email, reoon_status) for bounce checking.

    Verifies each result's work email via Reoon. Yields (index, email, status)
    where status is Reoon's verdict: safe, invalid, catch_all, unknown, etc.
    Updates each row's dict in place with `reoon_status`.
    Only verifies rows that have a work email.
    """
    client = ReoonClient(reoon_key)
    for i, row in enumerate(enriched_rows):
        email = (row.get("work_email") or "").strip()
        if not email:
            continue
        try:
            resp = client.verify(email, mode=mode)
            status = resp.get("status", "unknown")
        except (EnrichmentAuthError, EnrichmentCreditError):
            raise
        except Exception as exc:
            status = f"error: {exc}"
        row["reoon_status"] = status
        yield i, email, status
