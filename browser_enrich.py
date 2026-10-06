"""
Login-mode enrichment for the Candidate Enrichment Agent (EXPERIMENTAL).

Automates the provider's WEB login with the buyer's OWN username + password
using headless Chromium (Playwright), then attempts per-candidate contact
lookups through the provider's web UI.

IMPORTANT — read before relying on this:
  * This is fragile by nature. Vendors run bot detection; page structures
    change; logins that go through Google/LinkedIn OAuth CANNOT be automated
    (automation stops with a clear error in that case).
  * Automating logins may violate the vendor's Terms of Service and can put
    the buyer's account at risk. API-key mode remains the stable path.
  * NO live login was ever tested with real credentials while building this.
    Login page structures below come from public research (Oct 2026) and may
    differ. Every step is wrapped in try/except and every failure lands in
    the result's "notes" — the run never crashes.

Result dicts have the EXACT same shape as enrich.enrich_candidate() so
workbook.py works unchanged. "notes" is a single joined string.
"""

import re
import time
from typing import Dict, List, Optional

# Playwright is optional: the app must import cleanly without it.
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in the no-playwright test
    sync_playwright = None
    PlaywrightTimeout = Exception
    PLAYWRIGHT_AVAILABLE = False

# Reuse the tested fallback chain (website + pattern guess) from the API engine.
from enrich import (
    EMAIL_RE,
    clean_domain,
    find_email_on_website,
    guess_work_emails,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LoginUnavailableError(Exception):
    """Playwright is not installed; login mode cannot run."""


class LoginAuthError(Exception):
    """The sign-in was rejected, or the account cannot be automated
    (e.g., Google/LinkedIn OAuth-only account)."""


class LoginBlockedError(Exception):
    """Bot detection / CAPTCHA / human-verification blocked the automation."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOGIN_URLS = {
    "salesql": "https://app.salesql.com",
    "contactout": "https://contactout.com/login",
}

# SalesQL login offers: "Sign in with LinkedIn. Sign in with Google.
# Or use your work email." Only the email+password path is automatable.
OAUTH_MARKERS = [
    "sign in with linkedin",
    "continue with linkedin",
    "sign in with google",
    "continue with google",
]

BLOCK_MARKERS = [
    "captcha",
    "verify you are human",
    "verify you're human",
    "are you a robot",
    "just a moment",
    "attention required",
    "unusual traffic",
    "access denied",
    "datadome",
    "perimeterx",
]

PHONE_RE = re.compile(r"\+?1?[\s().\-]*\(?\d{3}\)?[\s().\-]*\d{3}[\s().\-]*\d{4}")

PAGE_TIMEOUT_MS = 30000
SELECTOR_TIMEOUT_MS = 15000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def _blank_result() -> Dict:
    return {
        "personal_email": "", "personal_email_source": "",
        "personal_phone": "", "personal_phone_source": "",
        "work_email": "", "work_email_source": "",
        "work_phone": "", "work_phone_source": "",
        "notes": [],
    }


def _page_text_lower(page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def _detect_blockers(page) -> Optional[str]:
    """Return a human-readable block reason, or None."""
    text = _page_text_lower(page)
    for marker in BLOCK_MARKERS:
        if marker in text:
            return (
                f"blocked by a human-verification / bot check ('{marker}'). "
                "Automation cannot continue."
            )
    return None


def _has_password_field(page) -> bool:
    try:
        return page.locator('input[type="password"]').count() > 0
    except Exception:
        return False


def _offers_oauth(page) -> bool:
    text = _page_text_lower(page)
    return any(m in text for m in OAUTH_MARKERS)


def _fill_login_form(page, username: str, password: str) -> None:
    """Fill the first email-ish + password fields and submit."""
    email_loc = page.locator(
        'input[type="email"], input[name="email"], '
        'input[placeholder*="mail" i], input[autocomplete="username"]'
    ).first
    email_loc.wait_for(state="visible", timeout=SELECTOR_TIMEOUT_MS)
    email_loc.fill(username)

    pw_loc = page.locator('input[type="password"]').first
    pw_loc.wait_for(state="visible", timeout=SELECTOR_TIMEOUT_MS)
    pw_loc.fill(password)

    # Prefer an explicit Sign In button; fall back to Enter key.
    btn = page.locator(
        'button:has-text("Sign In"), button:has-text("Log In"), '
        'button:has-text("Log in"), button[type="submit"]'
    ).first
    try:
        if btn.count() > 0:
            btn.click(timeout=8000)
        else:
            pw_loc.press("Enter")
    except Exception:
        pw_loc.press("Enter")


def _wait_for_login_result(page, provider: str) -> None:
    """Raise LoginAuthError/LoginBlockedError if the sign-in clearly failed."""
    deadline = time.time() + 25
    while time.time() < deadline:
        blocked = _detect_blockers(page)
        if blocked:
            raise LoginBlockedError(
                f"{provider}: {blocked} If this is a CAPTCHA, solve it is not "
                "possible in headless mode — try again later or use API-key mode."
            )
        url = (page.url or "").lower()
        text = _page_text_lower(page)
        # Away from any login page + some account signal = success.
        if "login" not in url and "signin" not in url and "sign-in" not in url:
            if any(s in text for s in
                   ["logout", "log out", "sign out", "dashboard", "credits",
                    "my account", "settings"]):
                return
            # URL moved on but no clear signal yet — keep waiting a little.
        # Still on login page with an error message = rejected credentials.
        if any(s in text for s in
               ["invalid", "incorrect", "wrong password", "not found",
                "does not exist", "couldn't find", "try again", "failed"]):
            if "login" in url or "signin" in url or _has_password_field(page):
                raise LoginAuthError(
                    f"{provider}: the site rejected the sign-in. Check the "
                    "username/password and try again."
                )
        time.sleep(1.0)
    # Timed out without a clear signal.
    if _has_password_field(page):
        raise LoginAuthError(
            f"{provider}: still on the login page after submitting — the "
            "sign-in was probably rejected, or the page structure changed."
        )


def _wait_for_selector_any(page, selectors, timeout_ms=15000) -> bool:
    """True if any selector matches within the timeout (for slow JS pages)."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for sel in selectors:
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def _save_debug_screenshot(page, provider: str) -> Optional[str]:
    """Save a screenshot of the current page for diagnosing login issues."""
    try:
        path = f"login_debug_{provider}.png"
        page.screenshot(path=path, full_page=True)
        return path
    except Exception:
        return None


def _fill_email_first_step(page, username: str) -> bool:
    """Try an email-first flow: fill email, click Continue, expect password next."""
    try:
        email_loc = page.locator(
            'input[type="email"], input[name="email"], '
            'input[placeholder*="mail" i], input[autocomplete="username"]'
        ).first
        if email_loc.count() == 0:
            return False
        email_loc.wait_for(state="visible", timeout=8000)
        email_loc.fill(username)
        btn = page.locator(
            'button:has-text("Continue"), button:has-text("Next"), '
            'button[type="submit"]'
        ).first
        if btn.count() > 0:
            btn.click(timeout=8000)
        else:
            email_loc.press("Enter")
        page.wait_for_timeout(2500)
        return True
    except Exception:
        return False


def do_login(page, provider: str, username: str, password: str) -> None:
    """Log in to the provider. Raises LoginAuthError / LoginBlockedError."""
    page.goto(LOGIN_URLS[provider], wait_until="domcontentloaded",
              timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(2500)

    try:
        blocked = _detect_blockers(page)
        if blocked:
            raise LoginBlockedError(f"{provider} login page: {blocked}")

        if _offers_oauth(page) and not _has_password_field(page):
            raise LoginAuthError(
                f"{provider}: this account only offers Google/LinkedIn sign-in, "
                "which automation cannot complete. Use an account with an "
                "email+password login, or switch to API-key mode later."
            )

        # The form is often JS-rendered seconds after load — wait for it.
        # Some sites also use an email-first two-step flow.
        password_ready = _wait_for_selector_any(
            page, ['input[type="password"]'], timeout_ms=15000)
        if not password_ready and _fill_email_first_step(page, username):
            password_ready = _wait_for_selector_any(
                page, ['input[type="password"]'], timeout_ms=15000)

        if not password_ready:
            raise LoginAuthError(
                f"{provider}: no email/password form found on the login page — "
                "the page structure may have changed."
            )

        _fill_login_form(page, username, password)
        _wait_for_login_result(page, provider)
    except (LoginAuthError, LoginBlockedError) as e:
        shot = _save_debug_screenshot(page, provider)
        hint = (f" A screenshot was saved as {shot} in the app folder — "
                "send it to support so the selectors can be fixed." if shot
                else "")
        raise type(e)(str(e) + hint) from e


# ---------------------------------------------------------------------------
# Per-candidate web lookups (best effort, heavily guarded)
# ---------------------------------------------------------------------------

def _scrape_contacts_from_page(page) -> Dict[str, List[str]]:
    """Pull email/phone-looking strings from the current page text."""
    try:
        text = page.inner_text("body") or ""
    except Exception:
        return {"emails": [], "phones": []}
    emails = sorted(set(m.lower() for m in EMAIL_RE.findall(text)))
    phones = []
    for m in set(PHONE_RE.findall(text)):
        digits = re.sub(r"\D", "", m)
        if 10 <= len(digits) <= 15 and m not in phones:
            phones.append(m.strip())
    return {"emails": emails[:10], "phones": phones[:5]}


def _lookup_contactout(page, cand: Dict) -> Dict:
    """Best effort: dashboard search -> first profile -> scrape contacts."""
    notes = []
    found = {"emails": [], "phones": []}
    try:
        page.goto("https://contactout.com/dashboard/search",
                  wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(2000)
        blocked = _detect_blockers(page)
        if blocked:
            notes.append(f"ContactOut search blocked: {blocked}")
            return {"found": found, "notes": notes}

        box = page.locator(
            'input[type="search"], input[placeholder*="Search" i], '
            'input[name="q"], input[name="query"]'
        ).first
        if box.count() == 0:
            notes.append("ContactOut: no search box found on the dashboard "
                         "(page structure may have changed).")
            return {"found": found, "notes": notes}
        query = f"{cand.get('first_name','')} {cand.get('last_name','')}".strip()
        box.fill(query)
        box.press("Enter")
        page.wait_for_timeout(4000)

        links = page.locator('a[href*="/people/"], a[href*="/profile/"]').all()
        if not links:
            notes.append(f"ContactOut: no profile results for '{query}'.")
            return {"found": found, "notes": notes}
        href = links[0].get_attribute("href") or ""
        if href.startswith("/"):
            href = "https://contactout.com" + href
        page.goto(href, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(2500)
        found = _scrape_contacts_from_page(page)
        notes.append(f"ContactOut: opened top result for '{query}'.")
    except Exception as exc:
        notes.append(f"ContactOut lookup error: {exc}")
    return {"found": found, "notes": notes}


def _lookup_salesql(page, cand: Dict) -> Dict:
    """Best effort: SalesQL's web app has no verified person-search page.

    SalesQL's product primarily reveals contacts through its LinkedIn
    browser extension, which this automation cannot drive. We try a
    generic dashboard search box if one exists, then report honestly.
    """
    notes = []
    found = {"emails": [], "phones": []}
    try:
        blocked = _detect_blockers(page)
        if blocked:
            notes.append(f"SalesQL page blocked: {blocked}")
            return {"found": found, "notes": notes}
        box = page.locator(
            'input[type="search"], input[placeholder*="Search" i], '
            'input[name="q"], input[name="query"]'
        ).first
        if box.count() == 0:
            notes.append(
                "SalesQL: no person-search box found in the web dashboard. "
                "SalesQL mainly reveals contacts via its LinkedIn browser "
                "extension, which this automation cannot drive — "
                "per-candidate web lookup skipped."
            )
            return {"found": found, "notes": notes}
        query = f"{cand.get('first_name','')} {cand.get('last_name','')}".strip()
        box.fill(query)
        box.press("Enter")
        page.wait_for_timeout(4000)
        found = _scrape_contacts_from_page(page)
        notes.append(f"SalesQL: searched dashboard for '{query}'.")
    except Exception as exc:
        notes.append(f"SalesQL lookup error: {exc}")
    return {"found": found, "notes": notes}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def login_enrich_candidate(page, provider: str, candidate: Dict) -> Dict:
    """Enrich ONE candidate using an already-logged-in page.

    Returns a result dict in the EXACT shape of enrich.enrich_candidate().
    """
    result = _blank_result()
    notes = result["notes"]

    first = (candidate.get("first_name") or "").strip()
    last = (candidate.get("last_name") or "").strip()
    domain = clean_domain(candidate.get("company_domain") or "")

    lookup = _lookup_contactout(page, candidate) if provider == "contactout" \
        else _lookup_salesql(page, candidate)
    notes.extend(lookup["notes"])
    emails = lookup["found"]["emails"]
    phones = lookup["found"]["phones"]

    work, personal = [], []
    for addr in emails:
        dom = addr.split("@")[-1]
        if dom in {
            "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
            "icloud.com", "live.com", "msn.com", "ymail.com", "protonmail.com",
            "proton.me", "mail.com", "gmx.com", "zoho.com",
        }:
            personal.append(addr)
        else:
            work.append(addr)

    if work:
        result["work_email"] = work[0]
        result["work_email_source"] = (
            f"{provider.title()} web login (page-scraped — confirm before use)")
    if personal:
        result["personal_email"] = personal[0]
        result["personal_email_source"] = (
            f"{provider.title()} web login (page-scraped — confirm before use)")
    if phones:
        # All ContactOut numbers are mobile/personal (page shows no type
        # labels) — every one goes to the personal number.
        result["personal_phone"] = "; ".join(dict.fromkeys(phones))
        result["personal_phone_source"] = (
            f"{provider.title()} web login (mobile)")

    # Same fallback chain as the API engine when the web lookup found nothing.
    if not result["work_email"]:
        if not domain:
            notes.append("Work email not found via web lookup; company domain "
                         "unknown so pattern/website fallback was skipped.")
        else:
            website_hits = find_email_on_website(domain, first, last)
            if website_hits:
                result["work_email"] = website_hits[0]
                result["work_email_source"] = \
                    "company website (UNVERIFIED — confirm before use)"
            else:
                guesses = guess_work_emails(first, last, domain)
                if guesses:
                    result["work_email"] = guesses[0]
                    result["work_email_source"] = (
                        "pattern-guessed (UNVERIFIED — do not use without "
                        "verification)")
                else:
                    notes.append("Work email not found; could not build a pattern.")

    if not any([result["work_email"], result["personal_email"],
                result["personal_phone"]]):
        notes.append("No contact details found via login-mode web lookup.")

    result["notes"] = "; ".join(notes)
    return result


def wait_for_manual_login(page, provider: str, timeout_s: int = 300) -> None:
    """Wait for the user to complete the sign-in manually in the visible browser.

    Polls for the same success signals as _wait_for_login_result. The user
    handles any 'verify you're human' checkbox themselves. Raises
    LoginAuthError on timeout.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        url = (page.url or "").lower()
        text = _page_text_lower(page)
        # Away from any login page + some account signal = success.
        if "login" not in url and "signin" not in url and "sign-in" not in url:
            if any(s in text for s in
                   ["logout", "log out", "sign out", "dashboard", "credits",
                    "my account", "settings"]):
                return
        time.sleep(2.0)
    raise LoginAuthError(
        f"{provider}: you did not finish signing in within "
        f"{timeout_s // 60} minutes. Please run again and complete the "
        "sign-in (including any 'verify you're human' checkbox) in the "
        "browser window."
    )


def manual_login_enrich_list(candidates, provider="contactout",
                             timeout_s=300):
    """Generator like login_enrich_list, but the USER signs in manually.

    Opens a VISIBLE browser on the provider's login page and waits until the
    user has signed in (handling any human-verification checkbox themselves),
    then enriches each candidate. No credentials are typed by the automation.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise LoginUnavailableError(
            "Login mode needs the Playwright browser package, which is not "
            "installed. Install it with:  pip install playwright  &&  "
            "playwright install chromium")
    provider = (provider or "contactout").lower()
    if provider not in LOGIN_URLS:
        raise ValueError(f"Unknown provider '{provider}'.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # must be visible
        try:
            page = browser.new_page(
                viewport={"width": 1366, "height": 900},
                user_agent=USER_AGENT,
            )
            page.set_default_timeout(SELECTOR_TIMEOUT_MS)
            page.goto(LOGIN_URLS[provider], wait_until="domcontentloaded",
                      timeout=PAGE_TIMEOUT_MS)
            wait_for_manual_login(page, provider, timeout_s)
            for i, cand in enumerate(candidates):
                try:
                    res = login_enrich_candidate(page, provider, cand)
                except Exception as exc:  # one bad candidate never kills the run
                    res = _blank_result()
                    res["notes"] = f"Login-mode lookup failed: {exc}"
                yield i, cand, res
        finally:
            browser.close()


def login_enrich_list(candidates, provider="salesql", username="",
                      password="", headless=True):
    """Generator yielding (index, candidate, result), like enrich.enrich_list.

    Logs in ONCE, then enriches each candidate with per-candidate isolation:
    one candidate's failure never stops the run.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise LoginUnavailableError(
            "Login mode needs the Playwright browser package, which is not "
            "installed. Install it with:  pip install playwright  &&  "
            "playwright install chromium")
    provider = (provider or "salesql").lower()
    if provider not in LOGIN_URLS:
        raise ValueError(f"Unknown provider '{provider}'.")
    if not username or not password:
        raise LoginAuthError("Username and password are both required.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page(
                viewport={"width": 1366, "height": 900},
                user_agent=USER_AGENT,
            )
            page.set_default_timeout(SELECTOR_TIMEOUT_MS)
            do_login(page, provider, username, password)  # raises on failure
            for i, cand in enumerate(candidates):
                try:
                    res = login_enrich_candidate(page, provider, cand)
                except Exception as exc:  # one bad candidate never kills the run
                    res = _blank_result()
                    res["notes"] = f"Login-mode lookup failed: {exc}"
                yield i, cand, res
        finally:
            browser.close()
