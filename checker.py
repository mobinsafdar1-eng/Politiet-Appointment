"""Police appointment rescheduler.

Flow: open change-booking page -> enter booking number + email -> read the
emailed verification code -> enter it -> "Finn time" page -> set Dato to
DATE_FROM, click "Vis første ledige time" ONCE -> if the earliest free day is
inside DATE_FROM..DATE_TO, pick it and click "Bekreft ny tid" and email you.
Otherwise end the session silently (no email); the schedule retries later.

Modes:  (none) normal | --dry-run | --probe
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import mail

BOOKING_URL = os.environ["BOOKING_URL"]
BOOKING_NUMBER = os.environ["BOOKING_NUMBER"].strip()
BOOKING_EMAIL = os.environ["BOOKING_EMAIL"].strip()
AUTO_BOOK = os.environ.get("AUTO_BOOK", "true").lower() == "true"
DATE_FROM = date.fromisoformat(os.environ.get("DATE_FROM") or "2026-12-30")
DATE_TO = date.fromisoformat(os.environ.get("DATE_TO") or "2027-01-15")

STATE_DIR = Path("state")
SHOT_DIR = Path("shots")
STATE_DIR.mkdir(exist_ok=True)
SHOT_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"

LOG = []  # diagnostics about what the slot search saw (included in probe email)
UNREADABLE = []  # filled when the site showed no day buttons at all

DAY_RX = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(\d+)\s*(?:ledige|available|free)", re.I)
TIME_RX = re.compile(r"^\s*\d{1,2}[:.]\d{2}\s*$")


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


def on_slot_page(page):
    return page.get_by_text(re.compile(r"Finn time|Find time|Ledige timer|Available times", re.I)).count() > 0


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

    box = page.get_by_label(re.compile(r"verifikasjonskode|verification code", re.I)).or_(page.locator(
        "input[name*=code i], input[name*=kode i], input[id*=code i], "
        "input[id*=kode i], input[autocomplete=one-time-code]"
    ))
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
    """After login, make sure we are on the 'Finn time' slot-search page."""
    if on_slot_page(page):
        return
    if click_button(page, ["Endre time", "Change time"]):
        settle(page)


def show_week(page, start, button=("Vis timer", "Show times")):
    """Type `start` into the Dato box and click the given button."""
    box = page.locator("input[type=date]")
    if not box.count():
        box = page.get_by_label(re.compile(r"Dato|Date", re.I))
    if not box.count():
        raise RuntimeError("Date box ('Dato') not found on the slot page")
    box.first.fill(start.isoformat())
    if not click_button(page, list(button)):
        raise RuntimeError(f"Button {button[0]!r} not found on the slot page")
    settle(page)
    page.wait_for_timeout(800)


def read_tabs(page):
    """Return [(date, free_count, element)] for the day buttons currently shown."""
    loc = page.get_by_text(re.compile(r"\d{4}-\d{2}-\d{2}\s*\d+\s*(?:ledige|available|free)", re.I))
    out, seen = [], set()
    for i in range(loc.count()):
        el = loc.nth(i)
        m = DAY_RX.search(" ".join(el.inner_text().split()))
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((date.fromisoformat(m.group(1)), int(m.group(2)), el))
    return sorted(out, key=lambda t: t[0])


def choose_time(page, d, el):
    """Pick the first time listed under day d's own column. Returns the time text."""
    box = el.bounding_box()
    try:
        el.click()  # harmless if the day header is not clickable
        settle(page)
        page.wait_for_timeout(500)
    except Exception:
        pass
    times = page.get_by_role("button", name=TIME_RX)
    if not times.count():
        times = page.get_by_text(TIME_RX)
    n = times.count()
    if not n:
        raise RuntimeError(f"Day {d}: no time buttons found")
    pick = None
    if box:
        best = None
        for i in range(n):
            b = times.nth(i).bounding_box()
            if not b:
                continue
            cx = b["x"] + b["width"] / 2
            if box["x"] - 2 <= cx <= box["x"] + box["width"] + 2 and b["y"] >= box["y"]:
                if best is None or b["y"] < best[0]:
                    best = (b["y"], i)
        if best:
            pick = best[1]
    if pick is None:
        pick = 0
        LOG.append(f"Could not match a time to day {d}'s column; used the first time on the page.")
    label = times.nth(pick).inner_text().strip()
    times.nth(pick).click()
    page.wait_for_timeout(800)
    return label


def find_and_pick(page, select):
    """Your manual steps: set Dato = DATE_FROM, click 'Vis første ledige time'.
    If the first free day is inside DATE_FROM..DATE_TO, pick it; otherwise do nothing."""
    if not on_slot_page(page):
        raise RuntimeError("Slot search page ('Finn time') not found - see probe screenshot")
    show_week(page, DATE_FROM, button=("Vis første ledige time", "Show first available"))
    tabs = read_tabs(page)
    LOG.append(f"After 'Vis første ledige time' (date {DATE_FROM}): " +
               (", ".join(f"{d} ({n})" for d, n, _ in tabs) or "no day buttons found"))
    cands = [(d, n, el) for d, n, el in tabs if d >= DATE_FROM and n > 0]
    if cands:
        d, n, el = cands[0]
        if d > DATE_TO:
            LOG.append(f"Earliest free day {d} is after {DATE_TO} - outside the range.")
            return None, None, []
        if select:
            return d, choose_time(page, d, el), [f"{d} ({n} free)"]
        return None, None, [f"{d} ({n} free)"]
    if not tabs:
        UNREADABLE.append(True)
        LOG.append("No day buttons were shown after 'Vis første ledige time'.")
    else:
        LOG.append(f"No free day on/after {DATE_FROM} in the results shown.")
    return None, None, []


def end_session(page):
    """Click 'Avslutt økten' (end session) so the site session is closed cleanly."""
    try:
        if click_button(page, ["Avslutt økten", "End session"]):
            settle(page)
        else:
            page.goto("https://politietbooking.nemo-q.se/Booking/Booking/Restart/Avtale",
                      wait_until="domcontentloaded")
    except Exception as e:
        print(f"[end_session] could not close session: {e}")


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
        page.on("dialog", lambda d: d.accept())
        try:
            log_in(page)
            open_change_time(page)

            if probe:
                files = snap(page, "probe-start")
                found = []
                try:
                    _, _, found = find_and_pick(page, select=False)
                    files += snap(page, "probe-after-search")
                except Exception as e:
                    LOG.append(f"(could not search: {e})")
                    files += snap(page, "probe-error")
                notify("Appointment checker: probe OK",
                       f"Logged in and reached the slot page.\n"
                       f"Range: {DATE_FROM}..{DATE_TO}\n"
                       f"Free days in range: {found or 'none'}\n\n"
                       "What the search saw:\n" + "\n".join(LOG) +
                       "\n\nScreenshots and HTML attached.", files)
                return

            d, label, found = find_and_pick(page, select=True)
            if not d:
                print(f"No free slot between {DATE_FROM} and {DATE_TO}. "
                      f"Searched: {LOG}. Closing; will retry next run.")
                st["last_check"] = datetime.now(timezone.utc).isoformat()
                st["fails"] = 0
                st["unreadable"] = (st.get("unreadable", 0) + 1) if UNREADABLE else 0
                save_state(st)
                if st["unreadable"] >= 4:  # safety net: page layout may have changed
                    notify("Appointment checker: cannot read results",
                           "The site showed no readable results 4 runs in a row. "
                           "The page may have changed. Screenshot attached.",
                           snap(page, "unreadable"), key="unreadable", every_hours=24)
                return

            files = snap(page, "slot-selected")
            if dry_run or not AUTO_BOOK:
                notify(f"Appointment checker: slot available {d} {label}",
                       f"Free slot found: {d} at {label}.\n"
                       "Selected but NOT booked (dry-run / AUTO_BOOK off).", files)
                return

            if not click_button(page, ["Bekreft ny tid", "Confirm new time"]):
                raise RuntimeError(f"Selected {d} {label} but found no 'Bekreft ny tid' button")
            settle(page)
            page.wait_for_timeout(1500)
            files = snap(page, "after-confirm")
            body = page.inner_text("body").lower()
            still_form = page.get_by_role("button", name=re.compile("Bekreft ny tid", re.I)).count() > 0
            ok = (not still_form) and re.search(
                r"er endret|endret|kvittering|bekreftelse|confirmation|has been changed|confirmed", body)
            st["booked"] = True
            st["booked_slot"] = f"{d} {label}"
            save_state(st)
            notify(f"Appointment checker: {'BOOKED' if ok else 'CHECK NEEDED'} {d} {label}",
                   ("The new time appears to be booked." if ok else
                    "Confirm was clicked but I could not see a clear confirmation. "
                    "Please verify on the police site / your email.") +
                   "\nThe checker has been switched off.", files)
            disable_workflow()
        except Exception as e:
            files = snap(page, "error")
            st = load_state()
            st["fails"] = st.get("fails", 0) + 1
            save_state(st)
            if st["fails"] >= 3:  # no email for one-off glitches
                notify("Appointment checker: problem (3 failed runs in a row)",
                       f"{e}\n\nScreenshot and HTML attached.",
                       files, key="repeated-failure", every_hours=24)
            print(f"ERROR: {e}")
            sys.exit(1)
        finally:
            end_session(page)
            browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.probe, a.dry_run)
