"""Clicks Actions -> Run on the Mr.cotton reports in Amazon Ads reporting.

Amazon emails each report to its configured recipients once it finishes, so nothing is downloaded here.

Flow: open the reporting page -> sign in if needed (email -> password -> OTP only if Amazon asks) ->
find the report's row -> Actions (the row's menu) -> Run.

Which report: the data is in UTC, so from 12:30 IST until midnight "Mr.cotton today" is run, and from
midnight until 12:30 IST "Mr.cotton yesterday" (TODAY_FROM_IST in .env; override with --report).

OTP is avoided by keeping a persistent browser profile (browser_profile/) with "Keep me signed in" and
"Don't require OTP on this browser" ticked, so Amazon trusts the device after the first sign-in.
If Amazon still asks for a code:
  - AMAZON_ADS_TOTP_SECRET set (authenticator-app 2SV) -> the code is generated, fully unattended;
  - otherwise, when run from a terminal, the code is asked for there;
  - otherwise (cron) the run fails with a screenshot - sign in once with `uv run run_reports.py --headed`.

Usage:
  uv run run_reports.py               # run today's or yesterday's report by the IST time (headless)
  uv run run_reports.py --report both # run both
  uv run run_reports.py --headed      # visible browser, e.g. for the first sign-in
  uv run run_reports.py --dry-run     # open each report's menu and check "Run" is there, without clicking it
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import pyotp
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError, Locator, Page, sync_playwright

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

EMAIL = os.getenv("AMAZON_ADS_EMAIL", "")
PASSWORD = os.getenv("AMAZON_ADS_PASSWORD", "")
TOTP_SECRET = os.getenv("AMAZON_ADS_TOTP_SECRET", "").replace(" ", "")
REPORTING_URL = os.getenv(
    "REPORTING_URL", "https://advertising.amazon.com/reporting?entityId=ENTITY1YST5GXMAIR36"
)
# Report data is in UTC. From TODAY_FROM_IST until midnight IST the "today" report is run, before it "yesterday".
REPORTS = {
    "today": ("Mr.cotton today", os.getenv("TODAY_REPORT_ID", "cbaf9b24-4e76-4ab5-805d-3a48d930fa5d")),
    "yesterday": ("Mr.cotton yesterday", os.getenv("YESTERDAY_REPORT_ID", "d66eeaf9-671b-4c4b-aa1a-b76931c5c9a2")),
}
TODAY_FROM_IST = dt_time.fromisoformat(os.getenv("TODAY_FROM_IST", "12:30"))
IST = timezone(timedelta(hours=5, minutes=30), "IST")     # no DST, so no tz database needed (Windows lacks one)
PROFILE_DIR = BASE_DIR / "browser_profile"
LOG_DIR = BASE_DIR / "logs"

SIGN_IN_TIMEOUT_SECONDS = 300
NAV_TIMEOUT_MS = 60_000

_EMAIL = ("#ap_email_login", "#ap_email", "input[name='email']")
_CONTINUE = ("input#continue", "#continue input", "#continue")
_PASSWORD = ("#ap_password", "input[name='password']")
_SIGN_IN = ("#signInSubmit",)
_REMEMBER_ME = "input[name='rememberMe']"
_OTP = ("#auth-mfa-otpcode", "#cvf-input-code", "#input-box-otp", "input[name='otpCode']", "input[name='code']")
_OTP_REMEMBER_DEVICE = "#auth-mfa-remember-device, input[name='rememberDevice']"
_OTP_SUBMIT = ("#auth-signin-button", "#cvf-submit-otp-button input", "#cvf-submit-otp-button",
               "input[type='submit']", "button[type='submit']")
_NOT_NOW = ("#ap-account-fixup-phone-skip-link", "a:has-text('Not now')", "button:has-text('Not now')",
            "input[value='Not now']")

_RUN_ITEM = re.compile(r"^\s*run(\s+now|\s+report)?\s*$", re.I)
_ACTIONS_LABEL = re.compile(r"action|more|option|menu", re.I)

logger = logging.getLogger("mrcotton")


class RunError(Exception):
    pass


def _first_visible(page: Page, selectors) -> Locator | None:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.is_visible():
                return loc
        except PlaywrightError:
            pass
    return None


def _click_first(page: Page, selectors) -> None:
    loc = _first_visible(page, selectors)
    if loc is None:
        page.keyboard.press("Enter")
    else:
        loc.click()


def _on_reporting_page(page: Page) -> bool:
    if "advertising.amazon.com" not in page.url or "/reporting" not in page.url:
        return False
    try:
        return (page.get_by_placeholder("Search by report name").first.is_visible()
                or page.get_by_role("button", name="Create report").first.is_visible())
    except PlaywrightError:
        return False


def _otp_code() -> str:
    if TOTP_SECRET:
        logger.info("OTP requested - generating it from AMAZON_ADS_TOTP_SECRET")
        return pyotp.TOTP(TOTP_SECRET).now()
    if sys.stdin and sys.stdin.isatty():
        return input("Amazon sent a one-time code (OTP) - type it here and press Enter: ").strip()
    raise RunError("Amazon asked for an OTP and there is no AMAZON_ADS_TOTP_SECRET or terminal to type it in. "
                   "Sign in once with `uv run run_reports.py --headed` so this browser is remembered.")


def ensure_signed_in(page: Page) -> None:
    """Opens the reporting page, filling Amazon's sign-in screens if they come up."""
    page.goto(REPORTING_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    done: set[str] = set()      # a step's screen coming back (e.g. wrong password) means it failed
    deadline = time.monotonic() + SIGN_IN_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        page.wait_for_timeout(2000)
        try:
            if _on_reporting_page(page):
                if "signed_in" in done:
                    logger.info("Signed in as %s", EMAIL)
                else:
                    logger.info("Already signed in (saved browser session)")
                return

            otp_box = _first_visible(page, _OTP)
            if otp_box:
                if "otp" in done:
                    raise RunError("Amazon rejected the OTP")
                otp_box.fill(_otp_code())
                remember = page.locator(_OTP_REMEMBER_DEVICE).first
                if remember.count() and not remember.is_checked():
                    remember.check()        # "Don't require OTP on this browser"
                _click_first(page, _OTP_SUBMIT)
                done.update({"otp", "signed_in"})
                continue

            not_now = _first_visible(page, _NOT_NOW)
            if not_now:
                not_now.click()
                continue

            password_box = _first_visible(page, _PASSWORD)
            if password_box:
                if "password" in done:
                    raise RunError("Amazon showed the password screen again - check AMAZON_ADS_PASSWORD, "
                                   "or a CAPTCHA may need solving in a --headed run")
                if not EMAIL or not PASSWORD:
                    raise RunError("Sign-in needed but AMAZON_ADS_EMAIL / AMAZON_ADS_PASSWORD are not set in .env")
                email_box = _first_visible(page, _EMAIL)
                if email_box and not email_box.input_value():
                    email_box.fill(EMAIL)
                password_box.fill(PASSWORD)
                remember = page.locator(_REMEMBER_ME).first
                if remember.count() and not remember.is_checked():
                    remember.check()        # "Keep me signed in"
                _click_first(page, _SIGN_IN)
                done.update({"password", "signed_in"})
                continue

            email_box = _first_visible(page, _EMAIL)
            if email_box and "email" not in done:
                if not EMAIL:
                    raise RunError("Sign-in needed but AMAZON_ADS_EMAIL is not set in .env")
                email_box.fill(EMAIL)
                _click_first(page, _CONTINUE)
                done.add("email")
                continue

            # Signed-out advertising.amazon.com landing page -> its "Sign in" link.
            if "/ap/" not in page.url and "open_sign_in" not in done:
                sign_in = page.get_by_role("link", name=re.compile(r"^\s*sign in\s*$", re.I)).first
                if sign_in.count() and sign_in.is_visible():
                    sign_in.click()
                    done.add("open_sign_in")
                    continue

            # Signed in but landed elsewhere (home page, account picker...) -> back to the reporting page.
            if "signed_in" in done and "/ap/" not in page.url and "advertising.amazon.com" in page.url \
                    and "/reporting" not in page.url and "reopened" not in done:
                page.goto(REPORTING_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                done.add("reopened")
                continue
        except PlaywrightError as exc:
            logger.debug("Screen changed mid-step (%s); looking again", exc)

    raise RunError(f"Did not reach the reporting page within {SIGN_IN_TIMEOUT_SECONDS}s (last URL: {page.url})")


def _report_row(page: Page, name: str, report_id: str) -> Locator:
    search = page.get_by_placeholder("Search by report name").first
    if search.count() and search.is_visible():
        search.fill(name)
        search.press("Enter")
        page.wait_for_timeout(2500)

    name_el = page.locator(f"a[href*='{report_id}']").first
    if not name_el.count():
        name_el = page.get_by_role("link", name=name, exact=True).first
    if not name_el.count():
        name_el = page.get_by_text(name, exact=True).first
    try:
        name_el.wait_for(state="visible", timeout=20_000)
    except PlaywrightError:
        raise RunError(f"Report '{name}' not found on the reporting page") from None

    row = name_el.locator("xpath=ancestor::*[@role='row' or self::tr][1]")
    if row.count():
        return row.first
    # Rows rendered as plain divs: the nearest ancestor that also holds a button is the row.
    return name_el.locator("xpath=ancestor::*[.//button or .//*[@role='button']][1]").first


def _actions_button(row: Locator) -> Locator:
    buttons = row.locator("button, [role='button']")
    count = buttons.count()
    candidates = [buttons.nth(i) for i in range(count)]
    for btn in candidates:
        label = (btn.get_attribute("aria-label") or "") + " " + (btn.get_attribute("title") or "")
        if _ACTIONS_LABEL.search(label) or btn.get_attribute("aria-haspopup"):
            return btn
    for btn in candidates:      # the ⋮ menu is icon-only, unlike the "Completed" status pill
        if btn.is_visible() and not btn.inner_text().strip():
            return btn
    raise RunError("Could not find the Actions (⋮) button in the report's row")


def _run_menu_item(page: Page) -> Locator:
    for role in ("menuitem", "option", "button", "link"):
        item = page.get_by_role(role, name=_RUN_ITEM)
        for i in range(item.count()):
            if item.nth(i).is_visible():
                return item.nth(i)
    item = page.get_by_text(_RUN_ITEM)
    for i in range(item.count()):
        if item.nth(i).is_visible():
            return item.nth(i)
    raise RunError("The Actions menu has no 'Run' option")


def pick_report(now_ist: datetime) -> str:
    """'today' from TODAY_FROM_IST until midnight IST, 'yesterday' from midnight until TODAY_FROM_IST."""
    return "today" if now_ist.time() >= TODAY_FROM_IST else "yesterday"


def run_report(page: Page, name: str, report_id: str, dry_run: bool) -> None:
    row = _report_row(page, name, report_id)
    _actions_button(row).click()
    page.wait_for_timeout(1000)
    run_item = _run_menu_item(page)

    if dry_run:
        logger.info("[dry run] '%s': Actions menu has '%s' - not clicking it", name, run_item.inner_text().strip())
        page.keyboard.press("Escape")
        return

    run_item.click()
    page.wait_for_timeout(2000)
    # Some report actions ask to confirm in a dialog.
    dialog = page.get_by_role("dialog")
    if dialog.count() and dialog.first.is_visible():
        confirm = dialog.first.get_by_role("button", name=re.compile(r"^\s*(run|confirm|yes|ok)\b", re.I))
        if confirm.count():
            confirm.first.click()
            page.wait_for_timeout(2000)

    status = ""
    try:
        status = " ".join(row.inner_text(timeout=5000).split())
    except PlaywrightError:
        pass
    logger.info("Ran '%s' - row now reads: %s", name, status or "(row re-rendered)")


def _screenshot(page: Page, label: str) -> None:
    path = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{label}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        logger.info("Screenshot saved to %s", path)
    except PlaywrightError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--dry-run", action="store_true", help="check each report's Run option without clicking it")
    parser.add_argument("--report", choices=["auto", "today", "yesterday", "both"], default="auto",
                        help="which report to run (default: auto, picked by the time in IST)")
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(LOG_DIR / "run.log", encoding="utf-8")])
    headless = not args.headed and os.getenv("HEADLESS", "true").lower() != "false"

    now_ist = datetime.now(IST)
    if args.report == "auto":
        keys = [pick_report(now_ist)]
        logger.info("It is %s IST -> running the '%s' report", f"{now_ist:%Y-%m-%d %H:%M}", keys[0])
    else:
        keys = ["today", "yesterday"] if args.report == "both" else [args.report]
    report_names = [REPORTS[k][0] for k in keys]

    failed: list[str] = []
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=headless, viewport={"width": 1600, "height": 1000}, locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(30_000)
        try:
            ensure_signed_in(page)
            for key in keys:
                name, report_id = REPORTS[key]
                try:
                    run_report(page, name, report_id, args.dry_run)
                except (RunError, PlaywrightError) as exc:
                    logger.error("'%s' failed: %s", name, exc)
                    _screenshot(page, re.sub(r"\W+", "_", name))
                    failed.append(name)
                    page.goto(REPORTING_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
        except (RunError, PlaywrightError) as exc:
            logger.error("Sign-in failed: %s", exc)
            _screenshot(page, "sign_in")
            failed = report_names
        finally:
            context.close()

    if failed:
        logger.error("Failed: %s", ", ".join(failed))
        return 1
    logger.info("Done - %s run; Amazon will email it to the configured recipients", ", ".join(report_names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
