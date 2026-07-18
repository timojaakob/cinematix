#!/usr/bin/env python3
"""
Cinema City (cinemacity.cz) showtime/availability scraper.

Polls the site's internal data-api-service JSON feed for a given cinema,
looks for showtimes matching one or more movie title queries, and pushes an
ntfy notification when a matching, non-sold-out showing appears (IMAX-only
by default). State (which events we've already notified about) is kept
in a small JSON file so we don't spam on every poll.

The monitored movie list and IMAX requirement live in /data/config.json,
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
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, request

import storage

# --- Config (env vars) -------------------------------------------------

CINEMA_ID = os.environ.get("CINEMA_ID", "1052")  # Praha Flora
SITE_ID = os.environ.get("SITE_ID", "10101")  # CZ site code used in the API path
# Confirmed live on 2026-07-17: the API accepts lang=en_US and returns film
# names in English (e.g. "The Odyssey" instead of "Odyssea") for the same
# events/auditoriums/booking links as cs_CZ — this lets movie match text
# default straight to the TMDb title instead of needing a Czech translation.
LANG = os.environ.get("LANG_CODE", "en_US")
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
# Fallback only: the live endpoint is normally storage's settings.ntfy_endpoint,
# editable at runtime via webui.py's settings dialog (see notify()'s callers).
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "media-server-timo-8k3p")
NTFY_URL = os.environ.get("NTFY_URL", f"https://ntfy.sh/{NTFY_TOPIC}")
WEBUI_URL = os.environ.get("WEBUI_URL", "https://cine.vfk.ovh")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
# Internal-only control port (not published to the host) that webui.py hits
# to trigger an immediate poll cycle instead of waiting for the interval.
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "5001"))

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

def notify(
    title: str, message: str, url: str = None, priority: str = "urgent", ntfy_endpoint: str | None = None
) -> bool:
    endpoint = ntfy_endpoint or NTFY_URL
    headers = {"Title": title, "Priority": priority, "Tags": "movie_camera,tickets"}
    if url:
        headers["Click"] = url
    try:
        resp = requests.post(endpoint, data=message.encode("utf-8"), headers=headers, timeout=10)
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
        attrs_set = {str(a).lower() for a in attrs}
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

        audio_language, audio_language_code, subtitle_language, subtitle_language_code = (
            audio_and_subtitle_languages(ev.get("languages") or {})
        )
        format_ = detect_format(attrs_set, is_imax)

        normalized.append(
            {
                "event_id": ev.get("id"),
                "movie_name": movie_name,
                "sold_out": sold_out,
                "availability": availability,
                "is_imax": is_imax,
                "format": format_,
                "auditorium": ev.get("auditorium"),
                "auditorium_tiny": ev.get("auditoriumTinyName"),
                "start_time": ev.get("eventDateTime"),
                "booking_link": clean_booking_link(ev.get("bookingLink")),
                "audio_language": audio_language,
                "audio_language_code": audio_language_code,
                "subtitle_language": subtitle_language,
                "subtitle_language_code": subtitle_language_code,
                "raw": ev if DEBUG else None,
            }
        )
    return normalized


def detect_format(attrs_set: set[str], is_imax: bool) -> str:
    """One of storage.FORMAT_OPTIONS. Confirmed live on 2026-07-17 against
    three weeks of real Flora listings: "3d" and "2d" are used as exact
    attributeIds tags (never combined with IMAX in the data seen), so IMAX
    takes priority since it's detected via the more specific auditorium/
    70-mm signals in is_imax."""
    if is_imax:
        return "IMAX"
    if "3d" in attrs_set:
        return "3D"
    return "2D"


# Confirmed live on 2026-07-17: each event carries a "languages" object
# ({"original": [...], "dubbed": [...], "voiceover": [...], "subtitles": [...]},
# each a list of ISO 639-1 codes) independent of the film-level attributeIds
# (which just list every language version offered across all showtimes, not
# this specific one).
LANGUAGE_NAMES = {
    "en": "English", "cs": "Czech", "sk": "Slovak", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pl": "Polish", "ru": "Russian", "hu": "Hungarian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "pt": "Portuguese", "nl": "Dutch",
    "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish", "uk": "Ukrainian",
}


def language_name(code: str | None) -> str | None:
    if not code:
        return None
    return LANGUAGE_NAMES.get(code.lower(), code.upper())


def audio_and_subtitle_languages(languages: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """What the audience actually hears is the dub/voiceover if one was
    recorded for this showtime, otherwise the film's original audio.
    Returns (audio_name, audio_code, subtitle_name, subtitle_code)."""
    dubbed = languages.get("dubbed") or []
    voiceover = languages.get("voiceover") or []
    original = languages.get("original") or []
    subtitles = languages.get("subtitles") or []

    audio_codes = dubbed or voiceover or original
    audio_code = audio_codes[0].lower() if audio_codes else None
    subtitle_code = subtitles[0].lower() if subtitles else None
    audio = language_name(audio_code)
    subtitle = language_name(subtitle_code)
    return audio, audio_code, subtitle, subtitle_code


def matching_movie(movie_name: str, movies: list[dict]) -> dict | None:
    name_lower = movie_name.lower()
    for m in movies:
        if m["query"] and m["query"] in name_lower:
            return m
    return None


def is_movie_due(movie: dict, now: datetime) -> bool:
    """check_interval_seconds of None means checking is turned off for this
    movie. A missing key (config predating this field) defaults to on."""
    interval = movie.get("check_interval_seconds", storage.DEFAULT_CHECK_INTERVAL_SECONDS)
    if interval is None:
        return False
    last_checked_at = movie.get("last_checked_at")
    if not last_checked_at:
        return True
    try:
        last_checked = datetime.fromisoformat(last_checked_at)
    except ValueError:
        return True
    return (now - last_checked).total_seconds() >= interval


def check_date(
    day: str,
    state: dict,
    seen_auditoriums: set,
    movies: list[dict],
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
        # No formats selected means nothing matches (not "any format") -
        # a movie only starts producing results once the user explicitly
        # picks at least one format to check for.
        if ev["format"] not in (movie.get("required_formats") or []):
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
            "format": ev["format"],
            "booking_link": link,
            "audio_language": ev["audio_language"],
            "audio_language_code": ev["audio_language_code"],
            "subtitle_language": ev["subtitle_language"],
            "subtitle_language_code": ev["subtitle_language_code"],
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


CHECKING = threading.Event()
# Movie ids queued by webui.py's "Check now" button to force a check this
# cycle even if their own check_interval_seconds says they're not due yet.
FORCE_CHECK_LOCK = threading.Lock()
FORCE_CHECK_IDS: set[str] = set()


def run_once(state: dict) -> None:
    CHECKING.set()
    try:
        config = storage.load_config()
        now = datetime.now()
        with FORCE_CHECK_LOCK:
            forced_ids = set(FORCE_CHECK_IDS)
            FORCE_CHECK_IDS.clear()
        due_movies = [m for m in config["movies"] if m["id"] in forced_ids or is_movie_due(m, now)]
        if not due_movies:
            log.debug("No movies due for a check this cycle (per-movie check_interval_seconds)")
            return

        today = date.today()
        seen_auditoriums = set()
        results: list[dict] = []
        new_matches: list[dict] = []
        for offset in range(DATE_RANGE_DAYS):
            day = (today + timedelta(days=offset)).isoformat()
            check_date(day, state, seen_auditoriums, due_movies, results, new_matches)
        if seen_auditoriums:
            log.debug("Auditoriums seen this cycle: %s", sorted(seen_auditoriums))

        # Movies not due this cycle keep their previously fetched showtimes
        # instead of being wiped from results.json; movies removed from the
        # monitor list entirely (not in configured_ids) get dropped here.
        configured_ids = {m["id"] for m in config["movies"]}
        due_ids = {m["id"] for m in due_movies}
        previous_matches = storage.load_results().get("matches", [])
        preserved = [
            r for r in previous_matches if r["movie_id"] in configured_ids and r["movie_id"] not in due_ids
        ]
        combined = preserved + results
        combined.sort(key=lambda r: r["start_time"] or "")
        storage.save_results(combined)
        storage.mark_movies_checked(due_ids, now.isoformat(timespec="seconds"))

        if new_matches:
            title, message = summarize_new_matches(new_matches)
            settings = config["settings"]
            if settings.get("notify_on_new_showtime", True):
                # Only mark these as notified if the push actually went through, so a
                # failed/rate-limited push gets retried next cycle instead of being
                # silently dropped.
                pushed = notify(
                    title,
                    message,
                    url=WEBUI_URL,
                    priority=settings.get("notify_on_new_showtime_priority", "urgent"),
                    ntfy_endpoint=settings.get("ntfy_endpoint") or None,
                )
                if pushed:
                    state["notified_event_ids"].extend(m["event_id"] for m in new_matches)
            else:
                # This notification case is turned off - still mark these as
                # notified so nothing floods through once it's turned back on.
                state["notified_event_ids"].extend(m["event_id"] for m in new_matches)

        save_state(state)
    finally:
        CHECKING.clear()


def start_control_server(wake_event: threading.Event) -> None:
    """Internal-only HTTP control port so webui.py can wake the poll loop
    early instead of waiting out the full interval."""
    control_app = Flask(__name__)
    control_app.logger.disabled = True
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @control_app.route("/check-now", methods=["POST"])
    def check_now():
        body = request.get_json(silent=True) or {}
        movie_id = body.get("movie_id")
        if movie_id:
            with FORCE_CHECK_LOCK:
                FORCE_CHECK_IDS.add(movie_id)
        wake_event.set()
        return {"ok": True}

    @control_app.route("/status", methods=["GET"])
    def control_status():
        return {"checking": CHECKING.is_set()}

    control_app.run(host="0.0.0.0", port=CONTROL_PORT)


def main() -> None:
    config = storage.load_config()
    titles = [m["title"] for m in config["movies"]]
    log.info(
        "Starting cinemacity-scraper: cinema=%s movies=%r interval=%ss",
        CINEMA_ID, titles, POLL_INTERVAL_SECONDS,
    )
    state = load_state()
    settings = config["settings"]
    if settings.get("notify_on_start", True):
        notify(
            "cinemacity-scraper started",
            f"Monitoring for {', '.join(titles) or 'nothing yet'} at Praha Flora",
            url=WEBUI_URL,
            priority=settings.get("notify_on_start_priority", "low"),
            ntfy_endpoint=settings.get("ntfy_endpoint") or None,
        )

    wake_event = threading.Event()
    threading.Thread(target=start_control_server, args=(wake_event,), daemon=True).start()

    while True:
        try:
            run_once(state)
        except Exception:
            log.exception("Unhandled error during poll cycle")
        wake_event.wait(POLL_INTERVAL_SECONDS)
        wake_event.clear()


if __name__ == "__main__":
    sys.exit(main())
