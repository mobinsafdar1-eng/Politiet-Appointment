"""Police appointment rescheduler for several bookings.

BOOKINGS secret, one person per line:   Name,BookingNumber[,Email[,CurrentDate]]
(blank email = the BOOKING_EMAIL secret). The first line is "you".

Each wake-up:
  1. One booking scouts: logs in, reads its appointment date, clicks
     "Vis første ledige time" once and notes which days have free slots.
     If a slot is earlier than its own date it books it.
  2. Any OTHER booking logs in only if a slot shown is earlier than its date,
     and books it. Several free slots -> several bookings, one by one.
  3. Hard daily caps: DAILY_MAX_TOTAL logins (48) and DAILY_MAX_PER_PERSON (12).
Only strictly earlier dates are ever booked. Emails only when something is found.

Modes:  (none) normal | --dry-run | --probe
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import mail

BOOKING_URL = os.environ["BOOKING_URL"]
AUTO_BOOK = os.environ.get("AUTO_BOOK", "true").lower() == "true"
DATE_FROM = date.fromisoformat(os.environ.get("DATE_FROM") or "2026-12-30")
DATE_CAP = date.fromisoformat(os.environ.get("DATE_TO") or "2027-01-31")
MAX_TOTAL = int(os.environ.get("DAILY_MAX_TOTAL") or 48)         # logins per day, all bookings
MAX_PER_PERSON = int(os.environ.get("DAILY_MAX_PER_PERSON") or 12)  # logins per day, per booking
START_JITTER = int(os.environ.get("START_JITTER_SECONDS") or 120)

STATE_DIR = Path("state")
SHOT_DIR = Path("shots")
STATE_DIR.mkdir(exist_ok=True)
SHOT_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"

LOG = []         # diagnostics for the current person
UNREADABLE = []  # set when the site showed no day buttons at all
SEEN = []        # [(day, free_count)] shown by the last 'Vis første ledige time'
STATE = {}

DAY_RX = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(\d+)\s*(?:ledige|available|free)", re.I)
TIME_RX = re.compile(r"^\s*\d{1,2}[:.]\d{2}\s*$")
MONTHS = {
    "januar": 1, "january": 1, "februar": 2, "february": 2, "mars": 3, "march": 3,
    "april": 4, "mai": 5, "may": 5, "juni": 6, "june": 6, "juli": 7, "july": 7,
    "august": 8, "september": 9, "oktober": 10, "october": 10,
    "november": 11, "desember": 12, "december": 12,
}


# ---------- people & state ----------
def load_people():
    default_email = os.environ.get("BOOKING_EMAIL", "").strip()
    people = []
    for line in (os.environ.get("BOOKINGS") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        email = (parts[2] if len(parts) > 2 else "") or default_email
        if len(parts) < 2 or not parts[1] or not email:
            print("[config] skipping a malformed BOOKINGS line (needs: Name,BookingNumber[,Email[,Date]])")
            continue
        people.append({"name": parts[0], "ref": parts[1], "email": email,
                       "hint": to_date(parts[3]) if len(parts) > 3 else None})
    if not people and os.environ.get("BOOKING_NUMBER") and default_email:
        people.append({"name": "Main", "ref": os.environ["BOOKING_NUMBER"].strip(),
                       "email": default_email, "hint": None})
    return people


def to_date(v):
    try:
        return date.fromisoformat(v) if isinstance(v, str) and v else None
    except ValueError:
        return None


def load_state():
    global STATE
    try:
        STATE = json.loads(STATE_FILE.read_text())
    except Exception:
        STATE = {}


def save_state():
    STATE_FILE.write_text(json.dumps(STATE, indent=2))


def pstate(p):
    ps = STATE.setdefault("people", {}).setdefault(p["ref"], {})
    legacy = os.environ.get("BOOKING_NUMBER", "").strip()
    if p["ref"] == legacy and not ps.get("booked_date"):  # from the older single-booking version
        bd = STATE.get("booked_date") or (STATE.get("booked_slot") or "")[:10]
        if to_date(bd):
            ps["booked_date"] = bd
    return ps


def logins_today():
    day = datetime.now(ZoneInfo("Europe/Oslo")).date().isoformat()
    L = STATE.setdefault("logins", {})
    if L.get("day") != day:
        L.clear()
        L.update({"day": day, "total": 0, "by": {}})
    return L


def can_login(p):
    L = logins_today()
    return L["total"] < MAX_TOTAL and L["by"].get(p["ref"], 0) < MAX_PER_PERSON


def count_login(p):
    L = logins_today()
    L["total"] += 1
    L["by"][p["ref"]] = L["by"].get(p["ref"], 0) + 1
    save_state()


def notify(subject, body, files=(), key=None, every_hours=6):
    if key:
        now = datetime.now(timezone.utc).timestamp()
        notices = STATE.setdefault("notices", {})
        if now - notices.get(key, 0) < every_hours * 3600:
            print(f"[notify] skipped (throttled): {subject}")
            return
        notices[key] = now
        save_state()
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
def log_in(page, p):
    page.goto(BOOKING_URL, wait_until="domcontentloaded")
    settle(page)
    fill_field(page, ["Bookingnummer", "Booking number"], p["ref"], 0)
    fill_field(page, ["E-post", "E-mail", "Email"], p["email"], 1)
    requested_at = datetime.now(timezone.utc)
    if not click_button(page, ["Endre time", "Change"]):
        raise RuntimeError("Could not find the 'Endre time' button on the login form")
    settle(page)
    if page_has_error(page):
        raise RuntimeError("Site says booking not found - check the booking number / email")

    box = page.get_by_label(re.compile(r"verifikasjonskode|verification code", re.I)).or_(page.locator(
        "input[name*=code i], input[name*=kode i], input[id*=code i], "
        "input[id*=kode i], input[autocomplete=one-time-code]"
    ))
    try:
        box.first.wait_for(timeout=15000)
    except PWTimeout:
        print("[login] no code box appeared; continuing")
        return
    code = mail.fetch_code(requested_at, p["ref"])
    if not code:
        raise RuntimeError("Verification code email did not arrive in time "
                           "(the booking email must be readable by GMAIL_ADDRESS)")
    box.first.fill(code)
    if not click_button(page, ["Bekreft", "Verifiser", "Fortsett", "Neste",
                               "Verify", "Confirm", "Continue", "Logg inn", "^OK$"]):
        box.first.press("Enter")
    settle(page)
    if page_has_error(page):
        raise RuntimeError("Site rejected the verification code")


def read_current_date(page):
    """Best-effort: the appointment date shown right after login (before the slot list)."""
    text = page.inner_text("body")
    cut = re.search(r"Ledige timer|Available times", text, re.I)
    if cut:
        text = text[:cut.start()]
    LOG.append(f"Page text after login: {' '.join(text.split())[:500]!r}")
    found = []
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        found.append((int(m[1]), int(m[2]), int(m[3])))
    for m in re.finditer(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", text):
        found.append((int(m[3]), int(m[2]), int(m[1])))
    for m in re.finditer(r"\b(\d{1,2})\.?\s+([A-Za-zæøåÆØÅ]+)\s+(\d{4})\b", text):
        mo = MONTHS.get(m[2].lower())
        if mo:
            found.append((int(m[3]), mo, int(m[1])))
    today, out = date.today(), []
    for y, mo, d in found:
        try:
            dd = date(y, mo, d)
        except ValueError:
            continue
        if today <= dd <= date(2028, 12, 31):
            out.append(dd)
    return min(out) if out else None


def open_change_time(page):
    if on_slot_page(page):
        return
    if click_button(page, ["Endre time", "Change time"]):
        settle(page)


def show_week(page, start, button):
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
    loc = page.get_by_text(re.compile(r"\d{4}-\d{2}-\d{2}\s*\d+\s*(?:ledige|available|free)", re.I))
    out, seen = [], set()
    for i in range(loc.count()):
        el = loc.nth(i)
        m = DAY_RX.search(" ".join(el.inner_text().split()))
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((date.fromisoformat(m.group(1)), int(m.group(2)), el))
    return sorted(out, key=lambda t: t[0])


def find_slot(page, date_to):
    """One click of 'Vis første ledige time'. Returns (day, count, element) if the
    earliest free day is inside DATE_FROM..date_to, else None."""
    if not on_slot_page(page):
        raise RuntimeError("Slot search page ('Finn time') not found")
    show_week(page, DATE_FROM, ("Vis første ledige time", "Show first available"))
    tabs = read_tabs(page)
    SEEN[:] = [(d, n) for d, n, _ in tabs]
    LOG.append("After 'Vis første ledige time': " +
               (", ".join(f"{d} ({n})" for d, n, _ in tabs) or "no day buttons found"))
    cands = [(d, n, el) for d, n, el in tabs if d >= DATE_FROM and n > 0]
    if cands:
        d, n, el = cands[0]
        if d > date_to:
            LOG.append(f"Earliest free day {d} is later than the limit {date_to}.")
            return None
        return d, n, el
    if not tabs:
        UNREADABLE.append(True)
    return None


def choose_time(page, d, el):
    box = el.bounding_box()
    try:
        el.click()
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


def end_session(page):
    try:
        if click_button(page, ["Avslutt økten", "End session"]):
            settle(page)
        else:
            page.goto("https://politietbooking.nemo-q.se/Booking/Booking/Restart/Avtale",
                      wait_until="domcontentloaded")
    except Exception as e:
        print(f"[end_session] could not close session: {e}")


# ---------- one person ----------
def process_person(browser, p, probe, dry_run):
    name, ps = p["name"], pstate(p)
    ps["last_checked"] = datetime.now(timezone.utc).isoformat()
    LOG.clear()
    UNREADABLE.clear()
    SEEN.clear()

    pre = [d for d in (p["hint"], to_date(ps.get("booked_date"))) if d]
    if pre and min(min(pre) - timedelta(days=1), DATE_CAP) < DATE_FROM and not (probe or dry_run):
        print(f"[{name}] already at the start of the range; nothing earlier possible.")
        return "none"

    ctx = browser.new_context(locale="nb-NO", timezone_id="Europe/Oslo",
                              viewport={"width": 1280, "height": 1000})
    ctx.set_default_timeout(30000)
    page = ctx.new_page()
    page.on("dialog", lambda d: d.accept())
    try:
        count_login(p)
        log_in(page, p)
        on_page = read_current_date(page)
        known = [d for d in (on_page, p["hint"], to_date(ps.get("booked_date"))) if d]
        have = min(known) if known else None
        limit = min(DATE_CAP, have - timedelta(days=1)) if have else DATE_CAP
        LOG.append(f"Current appointment: {have or 'UNKNOWN'}; searching {DATE_FROM}..{limit}")
        if have:
            ps["booked_date"] = have.isoformat()
        open_change_time(page)

        if probe:
            files = snap(page, f"probe-{p['ref'][-4:]}")
            hit = find_slot(page, limit) if limit >= DATE_FROM else None
            files += snap(page, f"probe-after-{p['ref'][-4:]}")
            notify(f"[{name}] probe OK",
                   f"Current appointment: {have or 'UNKNOWN (add it as the 4th column in BOOKINGS)'}\n"
                   f"Earlier slot in range: {hit[0] if hit else 'none'}\n\n"
                   "What the run saw:\n" + "\n".join(LOG) + "\n\nScreenshots attached.", files)
            return "none"

        hit = find_slot(page, limit) if limit >= DATE_FROM else None
        if not hit:
            ps["fails"] = 0
            ps["unreadable"] = (ps.get("unreadable", 0) + 1) if UNREADABLE else 0
            print(f"[{name}] no earlier slot. {LOG[-1] if LOG else ''}")
            if ps["unreadable"] >= 4:
                notify(f"[{name}] cannot read results",
                       "The site showed no readable results 4 checks in a row; the page may have changed.",
                       snap(page, "unreadable"), key=f"unreadable-{p['ref']}", every_hours=24)
            return "none"

        d, n, el = hit
        label = choose_time(page, d, el)
        files = snap(page, f"slot-{p['ref'][-4:]}")
        reason = None
        if dry_run or not AUTO_BOOK:
            reason = "dry-run / AUTO_BOOK is off"
        elif have is None:
            reason = "the current appointment date could not be read, so auto-booking is blocked for safety"
        if reason:
            notify(f"[{name}] earlier slot available {d} {label}",
                   f"Earlier slot found: {d} at {label} (current: {have or 'unknown'}).\n"
                   f"Selected but NOT booked: {reason}.", files,
                   key=f"avail-{p['ref']}-{d}", every_hours=3)
            return "alert"

        if not click_button(page, ["Bekreft ny tid", "Confirm new time"]):
            raise RuntimeError(f"Selected {d} {label} but found no 'Bekreft ny tid' button")
        settle(page)
        page.wait_for_timeout(1500)
        files = snap(page, f"confirm-{p['ref'][-4:]}")
        body = page.inner_text("body").lower()
        still_form = page.get_by_role("button", name=re.compile("Bekreft ny tid", re.I)).count() > 0
        ok = (not still_form) and re.search(
            r"er endret|endret|kvittering|bekreftelse|confirmation|has been changed|confirmed", body)
        if ok:
            ps["booked_date"] = d.isoformat()
        ps["booked_slot"] = f"{d} {label}"
        ps["fails"] = 0
        notify(f"[{name}] {'BOOKED' if ok else 'CHECK NEEDED'} {d} {label}",
               (f"The new time {d} {label} appears to be booked (was {have})." if ok else
                "Confirm was clicked but I could not see a clear confirmation. "
                "Please verify on the police site / your email.") +
               "\nFurther changes for this booking must be earlier than " + str(d) + ".", files)
        return "booked" if ok else "alert"
    except Exception as e:
        files = snap(page, f"error-{p['ref'][-4:]}")
        ps["fails"] = ps.get("fails", 0) + 1
        print(f"[{name}] ERROR: {e}")
        if ps["fails"] >= 3:  # no email for one-off glitches
            notify(f"[{name}] problem (3 failed checks in a row)",
                   f"{e}\n\nScreenshot and HTML attached.", files,
                   key=f"repeated-failure-{p['ref']}", every_hours=24)
        return "error"
    finally:
        end_session(page)
        ctx.close()


# ---------- main ----------
def known_date(p):
    ps = pstate(p)
    ds = [d for d in (p["hint"], to_date(ps.get("booked_date"))) if d]
    return min(ds) if ds else None


def improvable(p):
    kd = known_date(p)
    return kd is None or min(kd - timedelta(days=1), DATE_CAP) >= DATE_FROM


def eligible(p, remaining):
    """Is any slot shown earlier than this person's current appointment?"""
    kd = known_date(p)
    return any(n > 0 and DATE_FROM <= d <= DATE_CAP and (kd is None or d < kd)
               for d, n in remaining.items())


def take(remaining, p):
    bd = to_date((pstate(p).get("booked_slot") or "")[:10])
    if bd in remaining:
        remaining[bd] = max(0, remaining[bd] - 1)


def pick_scout(people):
    """The booking with the fewest logins today (ties: first in BOOKINGS, i.e. yours)."""
    L = logins_today()
    cands = [p for p in people if improvable(p) and can_login(p)]
    if not cands:
        return None
    return min(cands, key=lambda p: (L["by"].get(p["ref"], 0), people.index(p)))


def main(probe, dry_run):
    load_state()
    people = load_people()
    if not people:
        print("No bookings configured (set the BOOKINGS secret).")
        sys.exit(1)
    manual = probe or dry_run
    scout = None
    if not manual:
        scout = pick_scout(people)
        if not scout:
            L = logins_today()
            print(f"Nothing to do: daily login limit reached or all bookings at the earliest "
                  f"date (today {L['total']}/{MAX_TOTAL} logins).")
            save_state()
            return
        time.sleep(random.uniform(0, START_JITTER))  # vary the exact start time

    errors = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            def go(p, first):
                nonlocal errors
                if not first:
                    time.sleep(random.uniform(25, 90))  # pause between logins
                status = process_person(browser, p, probe, dry_run)
                save_state()
                if status == "error":
                    errors += 1
                return status

            if manual:
                for i, p in enumerate(people):
                    go(p, i == 0)
            else:
                # 1) one login scouts the slot list (and books for itself if a slot suits it)
                status = go(scout, True)
                remaining = dict(SEEN)
                if status == "booked":
                    take(remaining, scout)
                # 2) log in for another booking ONLY if a shown slot is earlier than its date
                if status != "error":
                    for p in people:
                        if p is scout or not improvable(p) or not can_login(p):
                            continue
                        if not eligible(p, remaining):
                            continue
                        status = go(p, False)
                        if SEEN:
                            remaining = dict(SEEN)
                        if status == "booked":
                            take(remaining, p)
        finally:
            browser.close()
    save_state()
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    main(a.probe, a.dry_run)
