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
import time
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
# Dates normally come from the site's date-list endpoint (see fetch_dates).
# This many days get scanned one by one instead, only as a cross-check
# (FULL_SCAN_INTERVAL_SECONDS) or as a fallback if that endpoint 404s
# (FALLBACK_SCAN_MIN_INTERVAL_SECONDS). 150 sequential day requests every
# 2 minutes ran fine for hours on 2026-10-19, so these rarer scans are safe.
FALLBACK_RANGE_DAYS = int(os.environ.get("FALLBACK_RANGE_DAYS", "150"))
DATES_LOOKAHEAD_DAYS = int(os.environ.get("DATES_LOOKAHEAD_DAYS", "365"))
FULL_SCAN_INTERVAL_SECONDS = int(os.environ.get("FULL_SCAN_INTERVAL_SECONDS", "3600"))
FALLBACK_SCAN_MIN_INTERVAL_SECONDS = int(os.environ.get("FALLBACK_SCAN_MIN_INTERVAL_SECONDS", "300"))
# Stop the cycle after this many single-day HTTP failures in a row (a lone
# bad day gets skipped instead of blocking every later date).
MAX_CONSECUTIVE_DAY_FAILURES = 3
# Notify again for an already-notified event when its availability rises by
# more than this over the lowest value seen since the last notification -
# catches screenings listed before going on sale (Dune: Part Three sat at
# 0.0156 for weeks), sold-out shows that reopen, and seats released from
# expired baskets after a drop. availabilityRatio has 4 decimals (checked
# live on 2026-10-21), fine enough to show a single seat even in the IMAX
# hall, so the default 0 means "any returned seat".
AVAILABILITY_RISE_THRESHOLD = float(os.environ.get("AVAILABILITY_RISE_THRESHOLD", "0"))
OUTAGE_ALERT_AFTER_SECONDS = int(os.environ.get("OUTAGE_ALERT_AFTER_SECONDS", "300"))
BACKOFF_MAX_SECONDS = 900
CHECK_NOW_COOLDOWN_SECONDS = 15
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
DATES_URL = (
    f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{SITE_ID}"
    f"/dates/in-cinema/{CINEMA_ID}/until/{{until}}"
)
CINEMA_PAGE_URL = f"https://www.cinemacity.cz/cinemas/flora/{CINEMA_ID}"
# The booking site's own showtime endpoint, called by its order page before
# any reCAPTCHA step. The film-events feed has no sale start time (its
# compositeBookingLink.blockOnlineSales was false/null for all 378 Flora
# showtimes on 2026-10-21), but this one does: confirmed live on 2026-10-21
# that ticketSaleStart ("2026-10-20 09:00:00", Prague local time) matches
# the Dune: Part Three drop. Times are Prague local time without an offset
# and get compared with datetime.now(), so the container needs
# TZ=Europe/Prague (as in the README's compose file).
PRESENTATION_URL = "https://tickets.cinemacity.cz/api/presentations/{id}"
# A sale start can still move until it passes, so recheck upcoming ones now
# and then. Once a showtime is on sale it's never fetched again.
PRESENTATION_REFRESH_SECONDS = 3600
# Caps the extra requests a newly monitored movie with many showtimes causes;
# the rest get fetched over the next cycles.
PRESENTATION_FETCHES_PER_CYCLE = 40

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) cinematix/1.0",
    "Accept": "application/json",
}

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cinematix")


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

# Network errors and 5xx get retried after these delays. 403/429 never get
# retried - hammering a rate limit or bot challenge is how you get banned.
FETCH_RETRY_DELAYS_SECONDS = (2, 5)


class FetchError(Exception):
    """kind is one of:
    network   - connection error/timeout after retries (we're offline)
    blocked   - 403/429 or a Cloudflare challenge (back off, alert at once)
    not_found - 404 (endpoint gone - API probably changed)
    http      - any other HTTP error after retries
    invalid   - response isn't the JSON shape we expect"""

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


def _retry_after_seconds(resp: requests.Response) -> int | None:
    try:
        return int(resp.headers.get("Retry-After", ""))
    except ValueError:
        return None


def get_json(url: str, what: str) -> dict:
    for attempt in range(len(FETCH_RETRY_DELAYS_SECONDS) + 1):
        last_attempt = attempt == len(FETCH_RETRY_DELAYS_SECONDS)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
        except requests.RequestException as e:
            if not last_attempt:
                time.sleep(FETCH_RETRY_DELAYS_SECONDS[attempt])
                continue
            raise FetchError("network", f"{what}: {e}")
        if resp.status_code in (403, 429) or resp.headers.get("cf-mitigated"):
            raise FetchError(
                "blocked", f"{what}: HTTP {resp.status_code}", resp.status_code, _retry_after_seconds(resp)
            )
        if resp.status_code == 404:
            raise FetchError("not_found", f"{what}: HTTP 404", 404)
        if resp.status_code >= 500 and not last_attempt:
            time.sleep(FETCH_RETRY_DELAYS_SECONDS[attempt])
            continue
        if resp.status_code >= 400:
            raise FetchError("http", f"{what}: HTTP {resp.status_code}", resp.status_code)
        try:
            payload = resp.json()
        except ValueError:
            raise FetchError("invalid", f"{what}: non-JSON response (site may have changed)", resp.status_code)
        if not isinstance(payload, dict):
            raise FetchError("invalid", f"{what}: unexpected JSON shape")
        return payload
    raise AssertionError("unreachable")


def fetch_day(day: str) -> dict:
    """Raises FetchError. An empty day is a normal, successful result."""
    return get_json(BASE_URL.format(date=day) + "?attr=&lang=" + LANG, day)


def fetch_dates(until: str) -> list[str]:
    """Dates with at least one screening (any film) at the cinema, from the
    site's own date picker endpoint. Confirmed live on 2026-10-19: answers a
    year ahead, ignores film filters, and has the same 60 s Cloudflare cache
    (max-age=60) as film-events. Raises FetchError; an empty list counts as
    invalid since a cinema always has *something* scheduled."""
    payload = get_json(DATES_URL.format(until=until) + "?attr=&lang=" + LANG, "date list")
    dates = (payload.get("body") or {}).get("dates")
    if not isinstance(dates, list) or not dates:
        raise FetchError("invalid", f"date list: empty or malformed ({str(payload)[:200]})")
    try:
        return sorted({date.fromisoformat(d).isoformat() for d in dates})
    except (TypeError, ValueError):
        raise FetchError("invalid", f"date list: malformed dates ({str(dates)[:200]})")


def _booking_site_time(value) -> str | None:
    """"2026-10-20 09:00:00" or "2026-10-20 09:00" -> "2026-10-20T09:00:00",
    or None if malformed."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except (TypeError, ValueError):
            pass
    return None


def fetch_sale_info(event_id: str) -> dict:
    """Raises FetchError. The endpoint answers HTTP 200 even for errors, so
    the body decides. Shapes seen live (Dune: Part Three, event 267914):
    open:   {"presentation": {..., "ticketSaleStart": "2026-10-20 09:00:00"}}
    locked: {"error": {"error": "TICKETING_NOT_STARTED", "data": "2026-10-20 09:00"}}
            (2026-10-20, before the drop - no other data while locked)
    bad id: {"error": {"error": "PRESENTATION_NOT_FOUND", "message": "Event not found"}}
    Member-only presales may use other error codes (not seen yet) - those
    get logged by the caller."""
    what = f"presentation {event_id}"
    payload = get_json(PRESENTATION_URL.format(id=event_id), what)
    p = payload.get("presentation")
    if isinstance(p, dict):
        return {
            "sale_start": _booking_site_time(p.get("ticketSaleStart")),
            "sale_stop": _booking_site_time(p.get("ticketSaleStop")),
            "member_only": bool(p.get("isMemberSaleOnly")),
        }
    err = payload.get("error")
    if not isinstance(err, dict):
        raise FetchError("invalid", f"{what}: unexpected JSON shape")
    code = err.get("error")
    if code == "TICKETING_NOT_STARTED":
        sale_start = _booking_site_time(err.get("data"))
        if not sale_start:
            raise FetchError("invalid", f"{what}: locked, unreadable sale start {err.get('data')!r}")
        # "locked" makes attach_sale_info fetch once more after the drop,
        # for the fields only an open screening returns.
        return {"sale_start": sale_start, "sale_stop": None, "member_only": False, "locked": True}
    raise FetchError("invalid", f"{what}: {code or 'unknown error'} ({err.get('message')})")


def attach_sale_info(results: list[dict], state: dict, now: datetime, may_fetch: bool) -> None:
    """Adds sale_start/sale_stop/member_only to each result from a per-event
    cache in state, fetching events that aren't cached yet or whose sale
    start is still ahead and was last checked PRESENTATION_REFRESH_SECONDS
    ago. A failed fetch just leaves the cached (or no) value - it must never
    hold back the cycle or its notifications."""
    cache: dict[str, dict] = state.setdefault("sale_info", {})
    now_s = now.isoformat(timespec="seconds")

    def needs_fetch(eid: str) -> bool:
        info = cache.get(eid)
        if not info:
            return True
        if info.get("failed"):
            return _seconds_since(info.get("checked_at"), now) >= PRESENTATION_REFRESH_SECONDS
        if not info.get("sale_start"):
            return False
        if info["sale_start"] <= now_s:
            return bool(info.get("locked"))
        return _seconds_since(info.get("checked_at"), now) >= PRESENTATION_REFRESH_SECONDS

    if may_fetch and _seconds_since(state.get("sale_info_backoff_until"), now) >= 0:
        # Never-fetched events first, so a new showtime's push can say when
        # booking opens.
        todo = sorted({r["event_id"] for r in results if needs_fetch(r["event_id"])}, key=lambda e: e in cache)
        for eid in todo[:PRESENTATION_FETCHES_PER_CYCLE]:
            try:
                cache[eid] = dict(fetch_sale_info(eid), checked_at=now_s)
            except FetchError as e:
                if e.kind in ("network", "blocked"):
                    if e.kind == "blocked":
                        until = now + timedelta(seconds=max(BACKOFF_MAX_SECONDS, e.retry_after or 0))
                        state["sale_info_backoff_until"] = until.isoformat(timespec="seconds")
                    log.warning("Stopped fetching sale start times this cycle: %s", e)
                    break
                log.warning("No sale start time for event %s: %s", eid, e)
                # Keeps a previously known time; retried after PRESENTATION_REFRESH_SECONDS.
                cache[eid] = dict(cache.get(eid) or {}, failed=True, checked_at=now_s)
    for r in results:
        info = cache.get(r["event_id"]) or {}
        r["sale_start"] = info.get("sale_start")
        r["sale_stop"] = info.get("sale_stop")
        r["member_only"] = info.get("member_only", False)


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
    """Longest matching query wins, so "dune: part three" claims
    "Dune: Part Three" instead of the shorter "dune" query that is also a
    substring of it."""
    name_lower = movie_name.lower()
    matches = [m for m in movies if m["query"] and m["query"] in name_lower]
    return max(matches, key=lambda m: len(m["query"]), default=None)


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


def process_day(
    day: str,
    payload: dict,
    seen_auditoriums: set,
    movies: list[dict],
    due_ids: set[str],
    results: list[dict],
    fetched_at: str,
    first_fetched_by_event: dict[str, str],
) -> None:
    if DEBUG:
        log.debug("Raw payload for %s:\n%s", day, json.dumps(payload, indent=2)[:3000])
    events = extract_events(payload)
    if not events:
        log.debug("%s: no film-events", day)
        return

    for ev in events:
        if ev["auditorium"]:
            seen_auditoriums.add(ev["auditorium"])

    for ev in events:
        # Match against every configured movie, not just the due ones, so a
        # not-due "Dune: Part Three" isn't claimed by a due "dune" query.
        movie = matching_movie(ev["movie_name"], movies)
        if not movie or movie["id"] not in due_ids:
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
            "fetched_at": first_fetched_by_event.get(eid, fetched_at),
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


def pick_notifications(fresh: list[dict], state: dict) -> list[dict]:
    """Which freshly fetched showtimes to notify about: never-notified ones
    that aren't sold out ("new"), plus already-notified ones whose
    availability rose by more than AVAILABILITY_RISE_THRESHOLD over the lowest value
    seen since their last notification ("more_seats"). The second case is
    what catches a ticket drop on screenings that were already listed but
    not bookable yet. Updates the low-water marks in state as a side effect;
    the caller resets them for the events it actually notified about."""
    notified = set(state["notified_event_ids"])
    baselines: dict[str, float] = state.setdefault("availability_baseline", {})
    picks = []
    for r in fresh:
        eid = r["event_id"]
        avail = r["availability"]
        if eid not in notified:
            if not r["sold_out"]:
                picks.append(dict(r, notify_kind="new"))
            continue
        if avail is None:
            continue
        if eid not in baselines:
            # Notified before baselines existed - start tracking from now.
            baselines[eid] = avail
            continue
        baselines[eid] = min(baselines[eid], avail)
        if not r["sold_out"] and avail - baselines[eid] > AVAILABILITY_RISE_THRESHOLD:
            picks.append(dict(r, notify_kind="more_seats", previous_availability=baselines[eid]))
    return picks


def _sale_upcoming(p: dict, now: datetime) -> bool:
    return bool(p.get("sale_start")) and p["sale_start"] > now.isoformat(timespec="seconds")


def summarize_new_showtimes(picks: list[dict], now: datetime) -> tuple[str, str]:
    """One line per movie and booking opening time, so a push about
    showtimes that can't be booked yet says when they can."""
    plural = "s" if len(picks) != 1 else ""
    if all(_sale_upcoming(p, now) for p in picks):
        title = f"{len(picks)} new showtime{plural} listed, booking not open yet"
    else:
        title = f"{len(picks)} new showtime{plural} available"
    counts: dict[tuple[str, str | None], int] = {}
    for p in picks:
        key = (p["movie_name"], p["sale_start"] if _sale_upcoming(p, now) else None)
        counts[key] = counts.get(key, 0) + 1
    lines = []
    for (name, opens), count in sorted(counts.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        line = f"{name}: {count}"
        if opens:
            line += f", booking opens {datetime.fromisoformat(opens).strftime('%a %d %b %H:%M')}"
        lines.append(line)
    return title, "\n".join(lines)


def summarize_more_seats(picks: list[dict]) -> tuple[str, str]:
    """One line per showtime - returned seats go fast, so the push itself
    has to say which showtime to open."""
    title = f"Seats freed up in {len(picks)} showtime{'s' if len(picks) != 1 else ''}"
    lines = []
    for p in sorted(picks, key=lambda p: p["start_time"] or ""):
        try:
            when = datetime.fromisoformat(p["start_time"]).strftime("%a %d %b %H:%M")
        except (TypeError, ValueError):
            when = p["date"]
        lines.append(
            f"{p['movie_name']} {p['format']}, {when}: "
            f"{p['previous_availability']:.1%} -> {p['availability']:.1%} free"
        )
    return title, "\n".join(lines)


def push_picks(picks: list[dict], kind: str, settings: dict, now: datetime) -> bool:
    """Sends one push for every pick of this notify_kind. Returns whether
    the caller may mark them as notified: True when the push went through
    or this case is turned off (so nothing floods through once it's turned
    back on), False when the push failed and should be retried next cycle."""
    if kind == "new":
        enabled_key, title, message = "notify_on_new_showtime", *summarize_new_showtimes(picks, now)
    else:
        enabled_key, title, message = "notify_on_more_seats", *summarize_more_seats(picks)
    if not settings.get(enabled_key, True):
        return True
    # A single showtime opens its booking page straight from the push.
    url = picks[0]["booking_link"] if len(picks) == 1 else WEBUI_URL
    return notify(
        title,
        message,
        url=url,
        priority=settings.get(f"{enabled_key}_priority", "urgent"),
        ntfy_endpoint=settings.get("ntfy_endpoint") or None,
    )


CHECKING = threading.Event()
# Movie ids actually included in the check cycle currently running - lets
# /status report per-movie checking state instead of "is the scraper doing
# *anything* right now", which used to make every movie's page show
# "Checking..." whenever any other movie's cycle was running, including
# ones with checking turned off entirely (see CHECKING_IDS reset below).
CHECKING_IDS: set[str] = set()
# Movie ids queued by webui.py's "Check now" button to force a check this
# cycle even if their own check_interval_seconds says they're not due yet.
FORCE_CHECK_LOCK = threading.Lock()
FORCE_CHECK_IDS: set[str] = set()


def _seconds_since(iso: str | None, now: datetime) -> float:
    if not iso:
        return float("inf")
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return float("inf")


def choose_dates(
    state: dict, today: date, now: datetime, result_dates: set[str]
) -> tuple[list[str], bool, FetchError | None]:
    """Returns (dates to fetch in order, whether the date list is complete
    enough to drop old showtimes on dates missing from it, error that should
    end the cycle). Always also fetches the previous valid list's future dates
    and every date we currently hold monitored showtimes for, so a short or
    flaky list can never hide a known screening."""
    today_s = today.isoformat()
    previous = sorted({d for d in state.get("last_dates", []) if d >= today_s} | result_dates)
    full_scan_days = [(today + timedelta(days=i)).isoformat() for i in range(FALLBACK_RANGE_DAYS)]
    until = (today + timedelta(days=DATES_LOOKAHEAD_DAYS)).isoformat()
    try:
        listed = [d for d in fetch_dates(until) if d >= today_s]
    except FetchError as e:
        if e.kind in ("network", "blocked"):
            return [], False, e
        if e.kind == "not_found":
            log.warning("Date list endpoint 404s - API probably changed, falling back to day-by-day scan")
            if _seconds_since(state.get("last_full_scan_at"), now) >= FALLBACK_SCAN_MIN_INTERVAL_SECONDS:
                state["last_full_scan_at"] = now.isoformat(timespec="seconds")
                return sorted(set(full_scan_days) | set(previous)), True, None
            return previous, False, None
        log.warning("Date list unusable (%s), checking the previous list's dates instead", e)
        return previous, False, None

    state["last_dates"] = listed
    dates = set(listed) | set(previous)
    if _seconds_since(state.get("last_full_scan_at"), now) >= FULL_SCAN_INTERVAL_SECONDS:
        # Hourly cross-check that the date list really covers every date
        # with screenings (unverified assumption) - see run_once's warning.
        state["last_full_scan_at"] = now.isoformat(timespec="seconds")
        state["full_scan_pending_check"] = listed
        dates |= set(full_scan_days)
    return sorted(dates), True, None


def handle_blocked(state: dict, err: FetchError, now: datetime) -> None:
    backoff = min(max(state.get("backoff_seconds", 0) * 2, POLL_INTERVAL_SECONDS, 60), BACKOFF_MAX_SECONDS)
    if err.retry_after:
        backoff = max(backoff, err.retry_after)
    state["backoff_seconds"] = backoff
    state["backoff_until"] = (now + timedelta(seconds=backoff)).isoformat(timespec="seconds")
    state["blocked_status"] = err.status
    log.error("Blocked by Cinema City (%s) - backing off for %ss", err, backoff)


def update_health(state: dict, cycle_failed: bool, blocked: bool, now: datetime, settings: dict) -> None:
    """One ntfy alert per outage (immediately when blocked, otherwise after
    OUTAGE_ALERT_AFTER_SECONDS), retried every cycle until it goes through
    (ntfy shares the network, so it can't get out during a full outage),
    and one recovery message once a cycle works again."""
    endpoint = settings.get("ntfy_endpoint") or None
    if not cycle_failed:
        if state.get("failing_since"):
            down_min = round(_seconds_since(state["failing_since"], now) / 60)
            if state.get("outage_alert_sent"):
                notify(
                    "cinematix back online",
                    f"Cinema City reachable again after {down_min} min.",
                    url=WEBUI_URL, priority="default", ntfy_endpoint=endpoint,
                )
            log.info("Recovered after %s min", down_min)
        for key in ("failing_since", "outage_alert_sent", "backoff_seconds", "backoff_until", "blocked_status"):
            state.pop(key, None)
        return

    state.setdefault("failing_since", now.isoformat(timespec="seconds"))
    if state.get("outage_alert_sent"):
        return
    if not blocked and _seconds_since(state["failing_since"], now) < OUTAGE_ALERT_AFTER_SECONDS:
        return
    since = datetime.fromisoformat(state["failing_since"]).strftime("%H:%M")
    if state.get("blocked_status"):
        title = f"cinematix BLOCKED by Cinema City (HTTP {state['blocked_status']})"
    else:
        title = "cinematix can't reach Cinema City"
    if notify(
        title,
        f"No successful check since {since}. Check for tickets manually.",
        url=WEBUI_URL, priority="high", ntfy_endpoint=endpoint,
    ):
        state["outage_alert_sent"] = True


def run_once(state: dict) -> None:
    global CHECKING_IDS
    CHECKING.set()
    try:
        config = storage.load_config()
        settings = config["settings"]
        now = datetime.now()
        with FORCE_CHECK_LOCK:
            forced_ids = set(FORCE_CHECK_IDS)
            FORCE_CHECK_IDS.clear()
        due_movies = [m for m in config["movies"] if m["id"] in forced_ids or is_movie_due(m, now)]
        if not due_movies:
            log.debug("No movies due for a check this cycle (per-movie check_interval_seconds)")
            return
        if _seconds_since(state.get("backoff_until"), now) < 0:
            log.info("Backing off until %s (blocked earlier), skipping cycle", state["backoff_until"])
            return
        due_ids = {m["id"] for m in due_movies}
        CHECKING_IDS = set(due_ids)

        today = date.today()
        today_s = today.isoformat()
        seen_auditoriums = set()
        results: list[dict] = []
        fetched_at = now.isoformat(timespec="seconds")
        # "Fetched" should read as when we *first* saw this event, not when
        # it was last re-fetched - carry the earliest known fetched_at
        # forward per event_id instead of overwriting it every cycle.
        previous_matches = storage.load_results().get("matches", [])
        first_fetched_by_event = {
            r["event_id"]: r["fetched_at"] for r in previous_matches if r.get("fetched_at")
        }

        result_dates = {r["date"] for r in previous_matches if (r.get("date") or "") >= today_s}
        dates, list_complete, cycle_error = choose_dates(state, today, now, result_dates)
        fetched_days: set[str] = set()
        days_with_events: set[str] = set()
        consecutive_failures = 0
        for day in dates:
            try:
                payload = fetch_day(day)
            except FetchError as e:
                if e.kind in ("network", "blocked"):
                    cycle_error = e
                    break
                consecutive_failures += 1
                log.error("Skipping %s: %s", day, e)
                if consecutive_failures >= MAX_CONSECUTIVE_DAY_FAILURES:
                    cycle_error = e
                    break
                continue
            consecutive_failures = 0
            fetched_days.add(day)
            if extract_events(payload):
                days_with_events.add(day)
            process_day(
                day, payload, seen_auditoriums, config["movies"], due_ids, results,
                fetched_at, first_fetched_by_event,
            )
        if cycle_error:
            log.warning("Cycle stopped early (%s), keeping previous results for unfetched days", cycle_error)
        # Offline or blocked: don't add requests to another host on top, use
        # cached sale times only.
        attach_sale_info(
            results, state, now, may_fetch=not (cycle_error and cycle_error.kind in ("network", "blocked"))
        )
        if seen_auditoriums:
            log.debug("Auditoriums seen this cycle: %s", sorted(seen_auditoriums))
        listed_at_scan = state.pop("full_scan_pending_check", None)
        if listed_at_scan is not None and not cycle_error:
            missing = sorted(days_with_events - set(listed_at_scan))
            if missing:
                log.warning("Full scan found screenings on dates missing from the date list: %s", missing)
            else:
                log.info("Full scan: date list covered every date with screenings")

        # Old showtimes are kept for movies not due this cycle, and for due
        # movies on days this cycle didn't manage to fetch. They're dropped
        # when the day was re-fetched (fresh results replace them), when the
        # day is past, or when a complete date list no longer has that day.
        # Movies removed from the monitor list entirely get dropped too.
        configured_ids = {m["id"] for m in config["movies"]}
        dates_set = set(dates)

        def keep_previous(r: dict) -> bool:
            if r["movie_id"] not in configured_ids:
                return False
            if r["movie_id"] not in due_ids:
                return True
            d = r.get("date") or ""
            if d in fetched_days or d < today_s:
                return False
            if list_complete and not cycle_error and d not in dates_set:
                return False
            return True

        preserved = [r for r in previous_matches if keep_previous(r)]
        # The same event can come back for two dates (and so twice in a
        # cycle, or once fresh and once preserved) - keep one per event_id,
        # preferring the freshly fetched entry.
        fresh = list({r["event_id"]: r for r in results}.values())
        combined = list({r["event_id"]: r for r in preserved + fresh}.values())
        combined.sort(key=lambda r: r["start_time"] or "")
        storage.save_results(combined)

        blocked = bool(cycle_error and cycle_error.kind == "blocked")
        if blocked:
            handle_blocked(state, cycle_error, now)
        # Network/blocked failures leave the movies due so the next cycle
        # retries right away; a few skipped bad days don't.
        cycle_failed = bool(cycle_error and cycle_error.kind in ("network", "blocked"))
        if not cycle_failed:
            storage.mark_movies_checked(due_ids, now.isoformat(timespec="seconds"))

        # Notify about whatever was found even if the cycle failed partway -
        # a broken later day must never hold back a ticket drop.
        picks = pick_notifications(fresh, state)
        for kind in ("new", "more_seats"):
            group = [p for p in picks if p["notify_kind"] == kind]
            # Only mark these as notified if the push actually went through, so a
            # failed/rate-limited push gets retried next cycle instead of being
            # silently dropped.
            if group and push_picks(group, kind, settings, now):
                for p in group:
                    if p["notify_kind"] == "new":
                        state["notified_event_ids"].append(p["event_id"])
                    if p["availability"] is not None:
                        state["availability_baseline"][p["event_id"]] = p["availability"]
        # Only track baselines and sale times for showtimes still in the results.
        live_ids = {r["event_id"] for r in combined}
        for key in ("availability_baseline", "sale_info"):
            state[key] = {k: v for k, v in state.get(key, {}).items() if k in live_ids}

        update_health(state, cycle_failed, blocked, now, settings)
        save_state(state)
    finally:
        CHECKING.clear()
        CHECKING_IDS = set()


def start_control_server(wake_event: threading.Event) -> None:
    """Internal-only HTTP control port so webui.py can wake the poll loop
    early instead of waiting out the full interval."""
    control_app = Flask(__name__)
    control_app.logger.disabled = True
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    last_check_now = [0.0]

    @control_app.route("/check-now", methods=["POST"])
    def check_now():
        # Every press is a full cycle of API requests - rate limit it so
        # button mashing can't add up to a ban.
        wait = CHECK_NOW_COOLDOWN_SECONDS - (time.monotonic() - last_check_now[0])
        if wait > 0:
            return {"ok": False, "retry_in": round(wait)}
        last_check_now[0] = time.monotonic()
        body = request.get_json(silent=True) or {}
        movie_id = body.get("movie_id")
        if movie_id:
            with FORCE_CHECK_LOCK:
                FORCE_CHECK_IDS.add(movie_id)
        wake_event.set()
        return {"ok": True}

    @control_app.route("/status", methods=["GET"])
    def control_status():
        movie_id = request.args.get("movie_id")
        if movie_id:
            return {"checking": movie_id in CHECKING_IDS}
        return {"checking": CHECKING.is_set()}

    control_app.run(host="0.0.0.0", port=CONTROL_PORT)


def main() -> None:
    config = storage.load_config()
    titles = [m["title"] for m in config["movies"]]
    log.info(
        "Starting cinematix: cinema=%s movies=%r interval=%ss",
        CINEMA_ID, titles, POLL_INTERVAL_SECONDS,
    )
    state = load_state()
    settings = config["settings"]
    if settings.get("notify_on_start", True):
        notify(
            "cinematix started",
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
