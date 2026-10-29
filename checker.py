"""Police appointment rescheduler.

Flow: open change-booking page -> enter booking number + email -> read the
emailed verification code -> enter it -> click "Endre time" -> look for a free
slot between DATE_FROM and DATE_TO -> book it (or just report it in dry-run).
If nothing is free, close the browser and exit; the schedule retries later.

Modes:  (none) normal | --dry-run | --probe
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import mail

BOOKING_URL = os.environ["BOOKING_URL"]
BOOKING_NUMBER = os.environ["BOOKING_NUMBER"].strip()
BOOKING_EMAIL = os.environ["BOOKING_EMAIL"].strip()
AUTO_BOOK = os.environ.get("AUTO_BOOK", "true").lower() == "true"
DATE_FROM = date.fromisoformat(os.environ.get("DATE_FROM") or "2026-12-15")
DATE_TO = date.fromisoformat(os.environ.get("DATE_TO") or "2026-12-30")

STATE_DIR = Path("state")
SHOT_DIR = Path("shots")
STATE_DIR.mkdir(exist_ok=True)
SHOT_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"

MONTHS = {
    "januar": 1, "january": 1, "jan": 1, "februar": 2, "february": 2, "feb": 2,
    "mars": 3, "march": 3, "mar": 3, "april": 4, "apr": 4, "mai": 5, "may": 5,
    "juni": 6, "june": 6, "jun": 6, "juli": 7, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11, "desember": 12, "december": 12, "des": 12, "dec": 12,
}


# ---------- state ----------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, indent=2))


def notify(subject, body, files=(), key=None, every_hours=6):
    """Send an email; if `key` is given, send at most once per `every_hours`."""
    st = load_state()
    now = datetime.now(timezone.utc).timestamp()
    if key:
        last = st.get("notices", {}).get(key, 0)
        if now - last < every_hours * 3600:
            print(f"[notify] skipped (throttled): {subject}")
            return
        st.setdefault("notices", {})[key] = now
        save_state(st)
    try:
        mail.send_mail(subject, body, files)
    except Exception as e:
        print(f"[notify] could not send email: {e}")


def snap(page, name):
    png, html = SHOT_DIR / f"{name}.png", SHOT_DIR / f"{name}.html"
    try:
        page.screenshot(path=str(png), full_page=True)
        html.write_text(page.content())
    except Exception as e:
        print(f"[snap] failed: {e}")
    return [p for p in (png, html) if p.exists()]


# ---------- small page helpers ----------
def fill_field(page, labels, value, fallback_index):
    for label in labels:
        loc = page.get_by_label(re.compile(label, re.I))
        if loc.count():
            loc.first.fill(value)
            return
    inputs = page.locator("input[type=text], input[type=email], input:not([type])")
    inputs.nth(fallback_index).fill(value)


def click_button(page, names):
    for name in names:
        rx = re.compile(name, re.I)
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=rx)
            if loc.count():
                loc.first.click()
                return True
        loc = page.locator(f"input[type=submit][value*='{name}' i], input[type=button][value*='{name}' i]")
        if loc.count():
            loc.first.click()
            return True
    return False


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass


def page_has_error(page):
    text = page.inner_text("body").lower()
    return "ikke funnet" in text or "booking was not found" in text or "not found" in text


# ---------- steps ----------
def log_in(page):
    page.goto(BOOKING_URL, wait_until="domcontentloaded")
    settle(page)
    fill_field(page, ["Bookingnummer", "Booking number"], BOOKING_NUMBER, 0)
    fill_field(page, ["E-post", "E-mail", "Email"], BOOKING_EMAIL, 1)
    requested_at = datetime.now(timezone.utc)
    if not click_button(page, ["Endre time", "Change"]):
        raise RuntimeError("Could not find the 'Endre time' button on the login form")
    settle(page)

    if page_has_error(page):
        raise RuntimeError("Site says booking not found - check BOOKING_NUMBER / BOOKING_EMAIL")

    box = page.locator(
        "input[name*=code i], input[name*=kode i], input[id*=code i], "
        "input[id*=kode i], input[autocomplete=one-time-code]"
    )
    try:
        box.first.wait_for(timeout=15000)
    except PWTimeout:
        print("[login] no code box appeared; continuing")
        return
    print("[login] waiting for verification code email...")
    code = mail.fetch_code(requested_at)
    if not code:
        raise RuntimeError("Verification code email did not arrive in time "
                           "(check GMAIL_ADDRESS receives mail sent to the booking email)")
    box.first.fill(code)
    if not click_button(page, ["Bekreft", "Verifiser", "Fortsett", "Neste",
                               "Verify", "Confirm", "Continue", "Logg inn", "^OK$"]):
        box.first.press("Enter")
    settle(page)
    if page_has_error(page):
        raise RuntimeError("Site rejected the verification code")


def open_change_time(page):
    # After login the booking is shown; click "Endre time" to open the calendar.
    if click_button(page, ["Endre time", "Change time", "Endre"]):
        settle(page)


def month_of(page):
    title = page.locator(".ui-datepicker-title").first.inner_text().strip()
    m = re.search(r"([A-Za-zæøåÆØÅ]+)\.?\s+(\d{4})", title)
    if not m or m.group(1).lower() not in MONTHS:
        raise RuntimeError(f"Could not read calendar month from: {title!r}")
    return int(m.group(2)), MONTHS[m.group(1).lower()]


def go_next_month(page):
    nxt = page.locator(".ui-datepicker-next:not(.ui-state-disabled)")
    if not nxt.count():
        return False
    nxt.first.click()
    page.wait_for_timeout(600)
    return True


def free_days_this_month(page):
    y, m = month_of(page)
    cells = page.locator(
        ".ui-datepicker-calendar td:not(.ui-datepicker-unselectable):not(.ui-state-disabled) a"
    )
    out = []
    for i in range(cells.count()):
        txt = cells.nth(i).inner_text().strip()
        if txt.isdigit():
            out.append((date(y, m, int(txt)), cells.nth(i)))
    return out


def find_and_pick(page, select):
    """Walk the calendar from DATE_FROM's month to DATE_TO's month.
    select=False: just list free dates in range. select=True: click the earliest."""
    if not page.locator(".ui-datepicker-title").count():
        raise RuntimeError("Calendar not found on the page - selectors need adjusting (see probe email)")
    for _ in range(12):  # move forward to the first month of the range
        if month_of(page) >= (DATE_FROM.year, DATE_FROM.month):
            break
        if not go_next_month(page):
            break
    found = []
    for _ in range(4):
        ym = month_of(page)
        if ym > (DATE_TO.year, DATE_TO.month):
            break
        days = [(d, el) for d, el in free_days_this_month(page) if DATE_FROM <= d <= DATE_TO]
        found += [d for d, _ in days]
        if select and days:
            d, el = min(days, key=lambda x: x[0])
            el.click()
            page.wait_for_timeout(1000)
            times = page.get_by_role("button", name=re.compile(r"^\s*\d{1,2}[:.]\d{2}"))
            if not times.count():
                times = page.locator("text=/^\\s*\\d{1,2}[:.]\\d{2}\\s*$/")
            if not times.count():
                raise RuntimeError(f"Date {d} clicked but no time slots found")
            label = times.first.inner_text().strip()
            times.first.click()
            page.wait_for_timeout(800)
            return d, label, found
        if not go_next_month(page):
            break
    return None, None, found


def disable_workflow():
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        subprocess.run(["gh", "api", "-X", "PUT",
                        f"repos/{repo}/actions/workflows/checker.yml/disable"], check=False)


# ---------- main ----------
def run(probe, dry_run):
    st = load_state()
    if st.get("booked") and not (probe or dry_run):
        print("Already booked - nothing to do.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="nb-NO", timezone_id="Europe/Oslo",
                                  viewport={"width": 1280, "height": 1000})
        ctx.set_default_timeout(30000)
        page = ctx.new_page()
        try:
            log_in(page)
            open_change_time(page)

            if probe:
                files = snap(page, "probe-calendar")
                found = []
                try:
                    _, _, found = find_and_pick(page, select=False)
                except Exception as e:
                    found = [f"(could not read calendar: {e})"]
                notify("Appointment checker: probe OK",
                       f"Logged in and reached the next page.\nFree dates in "
                       f"{DATE_FROM}..{DATE_TO}: {found or 'none'}\n"
                       "Screenshot and HTML attached.", files)
                return

            d, label, found = find_and_pick(page, select=True)
            if not d:
                print(f"No free slot between {DATE_FROM} and {DATE_TO}. Closing; will retry next run.")
                st["last_check"] = datetime.now(timezone.utc).isoformat()
                st["fails"] = 0
                save_state(st)
                return

            files = snap(page, "slot-selected")
            if dry_run or not AUTO_BOOK:
                notify(f"Appointment checker: slot available {d} {label}",
                       f"Free slot found: {d} at {label}.\n"
                       "Selected but NOT booked (dry-run / AUTO_BOOK off).", files)
                return

            if not click_button(page, ["Bekreft", "Book", "Lagre", "Fullfør",
                                       "Endre time", "Confirm", "Save"]):
                raise RuntimeError(f"Selected {d} {label} but found no confirm button")
            settle(page)
            files = snap(page, "after-confirm")
            body = page.inner_text("body").lower()
            ok = re.search(r"bekreft|endret|kvittering|confirmed|changed|booked", body)
            st["booked"] = True
            st["booked_slot"] = f"{d} {label}"
            save_state(st)
            notify(f"Appointment checker: {'BOOKED' if ok else 'CHECK NEEDED'} {d} {label}",
                   ("The new time was booked." if ok else
                    "Confirm was clicked but I could not see a clear confirmation. "
                    "Please verify on the police site / your email.") +
                   "\nThe checker has been switched off.", files)
            disable_workflow()
        except Exception as e:
            files = snap(page, "error")
            st = load_state()
            st["fails"] = st.get("fails", 0) + 1
            save_state(st)
            notify("Appointment checker: problem", f"{e}\n\nScreenshot and HTML attached.",
                   files, key=f"err-{str(e)[:40]}")
            print(f"ERROR: {e}")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.probe, a.dry_run)
