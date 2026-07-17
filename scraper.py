#!/usr/bin/env python3
"""
Cinema City (cinemacity.cz) showtime/availability scraper.

Polls the site's internal data-api-service JSON feed for a given cinema,
looks for showtimes matching one or more movie title queries, and pushes an
ntfy notification when a matching, non-sold-out showing appears (IMAX-only
by default). State (which events we've already notified about) is kept
in a small JSON file so we don't spam on every poll.

The watched movie list and IMAX requirement live in /data/config.json,
editable at runtime via the companion webui.py without restarting this
process (config is reloaded once per poll cycle). Every poll cycle also
writes the full set of currently matching showtimes to /data/results.json
so the web UI can display them.

Schema and IMAX detection fully confirmed against live data on 2026-07-17:
body.events[]/body.films[], soldOut, availabilityRatio, attributeIds,
auditorium/auditoriumTinyName, bookingLink. IMAX screenings at Flora carry
auditoriumTinyName="IMAX" (auditorium="IMAX VOLVO") and the "70-mm"
attributeId — both used together for redundancy. /collections and
/cinemas endpoints on this API do not exist (confirmed empty/404);
/attributes does and was used to cross-check the full tag taxonomy.
"""

import json
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

import storage

# --- Config (env vars) -------------------------------------------------

CINEMA_ID = os.environ.get("CINEMA_ID", "1052")  # Praha Flora
SITE_ID = os.environ.get("SITE_ID", "10101")  # CZ site code used in the API path
LANG = os.environ.get("LANG_CODE", "cs_CZ")
# Confirmed live on 2026-07-17 against a real IMAX screening (Odyssea,
# 2026-07-17T20:30 at Flora): the IMAX auditorium reports
# auditoriumTinyName="IMAX" (auditorium="IMAX VOLVO") AND carries the
# "70-mm" attributeId. Both signals are used (OR'd) for redundancy in case
# Cinema City ever changes the auditorium's display name.
IMAX_ATTRIBUTE_TAG = os.environ.get("IMAX_ATTRIBUTE_TAG", "70-mm").strip().lower()
IMAX_AUDITORIUM_TINY_NAME = os.environ.get("IMAX_AUDITORIUM_TINY_NAME", "imax").strip().lower()
# Optional stricter override: exact full auditorium name match instead of
# the tiny name / attribute signals above.
IMAX_AUDITORIUM = os.environ.get("IMAX_AUDITORIUM", "").strip().lower()
DATE_RANGE_DAYS = int(os.environ.get("DATE_RANGE_DAYS", "21"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "media-server-timo-8k3p")
NTFY_URL = os.environ.get("NTFY_URL", f"https://ntfy.sh/{NTFY_TOPIC}")
WEBUI_URL = os.environ.get("WEBUI_URL", "http://10.0.0.32:8090")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

BASE_URL = (
    f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{SITE_ID}"
    f"/film-events/in-cinema/{CINEMA_ID}/at-date/{{date}}"
)
CINEMA_PAGE_URL = f"https://www.cinemacity.cz/cinemas/flora/{CINEMA_ID}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) cinemacity-scraper/1.0",
    "Accept": "application/json",
}

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cinemacity-scraper")


# --- State ---------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("State file corrupt, starting fresh")
    return {"notified_event_ids": []}


def save_state(state: dict) -> None:
    storage._atomic_write(STATE_FILE, json.dumps(state))


# --- ntfy ------------------------------------------------------------

def notify(title: str, message: str, url: str = None, priority: str = "urgent") -> bool:
    headers = {"Title": title, "Priority": priority, "Tags": "movie_camera,tickets"}
    if url:
        headers["Click"] = url
    try:
        resp = requests.post(NTFY_URL, data=message.encode("utf-8"), headers=headers, timeout=10)
        resp.raise_for_status()
        log.info("Notified: %s", title)
        return True
    except requests.RequestException as e:
        log.error("ntfy push failed: %s", e)
        return False


def clean_booking_link(link: str | None) -> str | None:
    """Raw bookingLink from the API is tickets.cinemacity.cz/api/order/{id};
    the working link on the actual site drops the /api/ segment."""
    if not link:
        return link
    return link.replace("tickets.cinemacity.cz/api/order/", "tickets.cinemacity.cz/order/")


# --- Fetching / parsing --------------------------------------------------

def fetch_day(day: str) -> dict | None:
    url = BASE_URL.format(date=day) + "?attr=&lang=" + LANG
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error("Fetch failed for %s: %s", day, e)
        return None
    except ValueError:
        log.error("Non-JSON response for %s (site may have changed)", day)
        return None


def extract_events(payload: dict) -> list[dict]:
    """
    Try a few plausible response shapes. Vista/quickbook-style backends
    usually wrap the real payload in a "body" key; some deployments return
    it flat. Adjust here once you've seen a real response (run with
    DEBUG=true and check the logs / mounted state dir).
    """
    if not payload:
        return []
    body = payload.get("body", payload)
    events = body.get("events") or body.get("filmEvents") or []
    films = {f.get("id"): f for f in (body.get("films") or [])}

    normalized = []
    for ev in events:
        film = films.get(ev.get("filmId"), {})
        movie_name = (film.get("name") or "").strip()
        attrs = ev.get("attributeIds") or []
        attrs_str = " ".join(str(a) for a in attrs).lower()
        auditorium = (ev.get("auditorium") or "").lower()
        auditorium_tiny = (ev.get("auditoriumTinyName") or "").lower()
        availability = ev.get("availabilityRatio")

        sold_out = bool(ev.get("soldOut", False))
        if availability is not None and availability <= 0:
            sold_out = True

        if IMAX_AUDITORIUM:
            is_imax = auditorium == IMAX_AUDITORIUM
        else:
            is_imax = (
                auditorium_tiny == IMAX_AUDITORIUM_TINY_NAME
                or IMAX_ATTRIBUTE_TAG in attrs_str
            )

        normalized.append(
            {
                "event_id": ev.get("id"),
                "movie_name": movie_name,
                "sold_out": sold_out,
                "availability": availability,
                "is_imax": is_imax,
                "auditorium": ev.get("auditorium"),
                "auditorium_tiny": ev.get("auditoriumTinyName"),
                "start_time": ev.get("eventDateTime"),
                "booking_link": clean_booking_link(ev.get("bookingLink")),
                "raw": ev if DEBUG else None,
            }
        )
    return normalized


def matching_movie(movie_name: str, movies: list[dict]) -> dict | None:
    name_lower = movie_name.lower()
    for m in movies:
        if m["query"] and m["query"] in name_lower:
            return m
    return None


def check_date(
    day: str,
    state: dict,
    seen_auditoriums: set,
    movies: list[dict],
    require_imax: bool,
    results: list[dict],
    new_matches: list[dict],
) -> None:
    payload = fetch_day(day)
    if DEBUG and payload:
        log.debug("Raw payload for %s:\n%s", day, json.dumps(payload, indent=2)[:3000])
    events = extract_events(payload)
    if not events:
        log.debug("%s: no film-events (not yet published — confirmed normal for future weeks)", day)
        return

    for ev in events:
        if ev["auditorium"]:
            seen_auditoriums.add(ev["auditorium"])

    for ev in events:
        movie = matching_movie(ev["movie_name"], movies)
        if not movie:
            continue
        if require_imax and not ev["is_imax"]:
            continue

        eid = str(ev["event_id"])
        link = ev["booking_link"] or (
            f"{CINEMA_PAGE_URL}#/buy-tickets-by-cinema?in-cinema={CINEMA_ID}&at={day}&view-mode=list"
        )
        entry = {
            "event_id": eid,
            "movie_name": ev["movie_name"],
            "movie_id": movie["id"],
            "query": movie["query"],
            "date": day,
            "start_time": ev["start_time"],
            "auditorium": ev["auditorium"],
            "availability": ev["availability"],
            "sold_out": ev["sold_out"],
            "is_imax": ev["is_imax"],
            "booking_link": link,
        }
        results.append(entry)

        if ev["sold_out"]:
            log.info("%s: '%s' found but SOLD OUT (event %s)", day, ev["movie_name"], eid)
            continue
        if eid in state["notified_event_ids"]:
            continue  # already notified

        new_matches.append(entry)


def summarize_new_matches(new_matches: list[dict]) -> tuple[str, str]:
    counts: dict[str, int] = {}
    for m in new_matches:
        counts[m["movie_name"]] = counts.get(m["movie_name"], 0) + 1
    title = f"{len(new_matches)} new showtime{'s' if len(new_matches) != 1 else ''} available"
    message = ", ".join(f"{name}: {count}" for name, count in counts.items())
    return title, message


def run_once(state: dict) -> None:
    config = storage.load_config()
    today = date.today()
    seen_auditoriums = set()
    results: list[dict] = []
    new_matches: list[dict] = []
    for offset in range(DATE_RANGE_DAYS):
        day = (today + timedelta(days=offset)).isoformat()
        check_date(
            day, state, seen_auditoriums, config["movies"], config["require_imax"], results, new_matches
        )
    if seen_auditoriums:
        log.debug("Auditoriums seen this cycle: %s", sorted(seen_auditoriums))
    results.sort(key=lambda r: r["start_time"] or "")
    storage.save_results(results)

    if new_matches:
        title, message = summarize_new_matches(new_matches)
        # Only mark these as notified if the push actually went through, so a
        # failed/rate-limited push gets retried next cycle instead of being
        # silently dropped.
        if notify(title, message, url=WEBUI_URL):
            state["notified_event_ids"].extend(m["event_id"] for m in new_matches)

    save_state(state)


def main() -> None:
    config = storage.load_config()
    titles = [m["title"] for m in config["movies"]]
    log.info(
        "Starting cinemacity-scraper: cinema=%s movies=%r imax_only=%s interval=%ss",
        CINEMA_ID, titles, config["require_imax"], POLL_INTERVAL_SECONDS,
    )
    state = load_state()
    notify(
        "cinemacity-scraper started",
        f"Watching for {', '.join(titles) or 'nothing yet'} at Praha Flora",
        url=WEBUI_URL,
        priority="low",
    )

    while True:
        try:
            run_once(state)
        except Exception:
            log.exception("Unhandled error during poll cycle")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
