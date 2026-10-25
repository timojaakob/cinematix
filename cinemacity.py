"""
Client for Cinema City Czech Republic's unofficial internal APIs, shared by
scraper.py (polling) and webui.py (cinema picker).

Two hosts are used, both reverse-engineered from the public website (the
film-events feed builds on ra100/cinema-loader):

- www.cinemacity.cz/cz/data-api-service - the showtime feed behind the
  site's "buy tickets" pages: cinemas, dates with screenings, and one day's
  films and events.
- tickets.cinemacity.cz/api - the booking site's own API. Its presentation
  endpoint gives the booking open time, which the feed doesn't carry, and
  the hall's ids. The seat plan and seat status endpoints give the hall
  layout and which seats are free.

Schema confirmed against live data on 2026-07-17 (feed) and 2026-10-21
(cinema list, presentations, formats across all 13 CZ cinemas).
"""

import logging
import time
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from version import __version__

log = logging.getLogger("cinematix")

# Every time the APIs return (showtimes, sale start/stop) is Prague local
# time without an offset. Comparing them with now() instead of the
# container clock means the container doesn't need TZ set.
TZ = ZoneInfo("Europe/Prague")


def now() -> datetime:
    """Current Prague time as a naive datetime, comparable with API times."""
    return datetime.now(TZ).replace(tzinfo=None)


SITE_ID = "10101"  # Czech Republic
FEED_BASE = f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{SITE_ID}"
# lang=en_US returns film names in English ("The Odyssey" instead of
# "Odyssea") for the same events as cs_CZ, so match text can default to
# the TMDb title.
FEED_PARAMS = "?attr=&lang=en_US"
PRESENTATION_URL = "https://tickets.cinemacity.cz/api/presentations/{id}"
SEATPLAN_URL = "https://tickets.cinemacity.cz/api/seats/seatplan?venueId={venue}&seatplanId={plan}"
SEAT_STATUS_URL = (
    "https://tickets.cinemacity.cz/api/seats/seats-statusV2"
    "?presentationId={id}&venueTypeId={venue_type}&isReserved={reserved}"
)

HEADERS = {
    "User-Agent": f"cinematix/{__version__} (+https://github.com/timojaakob/cinematix)",
    "Accept": "application/json",
}

# The seat status endpoint answers 403 without a "uuid" header. The booking
# site makes one up per visitor (any UUID v4 works, no cookie needed -
# checked live on 2026-10-25), so one per process is enough.
BOOKING_SESSION_HEADERS = {"uuid": str(uuid.uuid4())}

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


def get_json(url: str, what: str, extra_headers: dict | None = None) -> dict:
    headers = {**HEADERS, **(extra_headers or {})}
    for attempt in range(len(FETCH_RETRY_DELAYS_SECONDS) + 1):
        last_attempt = attempt == len(FETCH_RETRY_DELAYS_SECONDS)
        try:
            resp = requests.get(url, headers=headers, timeout=15)
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


# --- Feed ------------------------------------------------------------------

def fetch_cinemas(until: str) -> list[dict]:
    """Every CZ cinema with screenings before `until`, sorted by name, as
    {"id", "name", "link"}. Confirmed live on 2026-10-21: 13 cinemas, e.g.
    {"id": "1052", "displayName": "Prague Flora, OC FLORA",
    "link": "https://www.cinemacity.cz/cinemas/flora"}. Raises FetchError."""
    payload = get_json(f"{FEED_BASE}/cinemas/with-event/until/{until}{FEED_PARAMS}", "cinema list")
    cinemas = (payload.get("body") or {}).get("cinemas")
    if not isinstance(cinemas, list) or not cinemas:
        raise FetchError("invalid", f"cinema list: empty or malformed ({str(payload)[:200]})")
    out = [
        {"id": str(c["id"]), "name": c.get("displayName") or str(c["id"]), "link": c.get("link") or ""}
        for c in cinemas
        if isinstance(c, dict) and c.get("id")
    ]
    return sorted(out, key=lambda c: c["name"])


def fetch_day(cinema_id: str, day: str) -> dict:
    """Raises FetchError. An empty day is a normal, successful result
    (unpublished future dates return HTTP 200 with empty arrays)."""
    return get_json(f"{FEED_BASE}/film-events/in-cinema/{cinema_id}/at-date/{day}{FEED_PARAMS}", day)


def fetch_dates(cinema_id: str, until: str) -> list[str]:
    """Dates with at least one screening (any film) at the cinema, from the
    site's own date picker endpoint. Confirmed live on 2026-10-19: answers a
    year ahead, ignores film filters, and has the same 60 s Cloudflare cache
    (max-age=60) as film-events. Raises FetchError; an empty list counts as
    invalid since a cinema always has *something* scheduled."""
    payload = get_json(f"{FEED_BASE}/dates/in-cinema/{cinema_id}/until/{until}{FEED_PARAMS}", "date list")
    dates = (payload.get("body") or {}).get("dates")
    if not isinstance(dates, list) or not dates:
        raise FetchError("invalid", f"date list: empty or malformed ({str(payload)[:200]})")
    try:
        return sorted({date.fromisoformat(d).isoformat() for d in dates})
    except (TypeError, ValueError):
        raise FetchError("invalid", f"date list: malformed dates ({str(dates)[:200]})")


def clean_booking_link(link: str | None) -> str | None:
    """Raw bookingLink from the API is tickets.cinemacity.cz/api/order/{id};
    the working link on the actual site drops the /api/ segment."""
    if not link:
        return link
    return link.replace("tickets.cinemacity.cz/api/order/", "tickets.cinemacity.cz/order/")


def cinema_day_link(cinema: dict, day: str) -> str:
    """The cinema's own showtime list for a day - the fallback when an event
    has no booking link."""
    base = cinema.get("link") or "https://www.cinemacity.cz/cinemas"
    return f"{base}/{cinema['id']}#/buy-tickets-by-cinema?in-cinema={cinema['id']}&at={day}&view-mode=list"


# Formats, in detection priority: a hall type wins over 3D, so a 4DX 3D
# showing counts as 4DX. Attribute tags confirmed live on 2026-10-21 across
# all 13 CZ cinemas: "4dx", "superscreen", "vip", "imax", "70-mm", "3d",
# "2d" ("dolby-atmos" is sound, not a hall, and is ignored). The only IMAX
# hall is Flora's, which reports auditoriumTinyName="IMAX" (auditorium="IMAX
# VOLVO") and the "70-mm" tag; some of its showings carry "imax" instead.
FORMAT_TAGS = [("4DX", "4dx"), ("Superscreen", "superscreen"), ("VIP", "vip"), ("3D", "3d")]
IMAX_TAGS = {"imax", "70-mm"}


def detect_format(attrs: set[str], auditorium_tiny: str) -> str:
    """One of storage.FORMAT_OPTIONS."""
    if auditorium_tiny == "imax" or attrs & IMAX_TAGS:
        return "IMAX"
    for name, tag in FORMAT_TAGS:
        if tag in attrs:
            return name
    return "2D"


# Each event carries a "languages" object ({"original": [...], "dubbed":
# [...], "voiceover": [...], "subtitles": [...]}, each a list of ISO 639-1
# codes) independent of the film-level attributeIds (which list every
# language version offered across all showtimes, not this specific one).
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
    return language_name(audio_code), audio_code, language_name(subtitle_code), subtitle_code


def extract_events(payload: dict) -> list[dict]:
    """Normalizes one day's feed (body.films[] joined to body.events[] on
    filmId) into flat event dicts."""
    if not payload:
        return []
    body = payload.get("body", payload)
    events = body.get("events") or []
    films = {f.get("id"): f for f in (body.get("films") or [])}

    normalized = []
    for ev in events:
        film = films.get(ev.get("filmId"), {})
        attrs = {str(a).lower() for a in ev.get("attributeIds") or []}
        availability = ev.get("availabilityRatio")
        # soldOut alone is unreliable (seen false at <1% availability), so a
        # zero ratio counts too.
        sold_out = bool(ev.get("soldOut", False)) or (availability is not None and availability <= 0)
        audio, audio_code, subtitle, subtitle_code = audio_and_subtitle_languages(ev.get("languages") or {})
        format_ = detect_format(attrs, (ev.get("auditoriumTinyName") or "").lower())
        normalized.append(
            {
                "event_id": ev.get("id"),
                "movie_name": (film.get("name") or "").strip(),
                "sold_out": sold_out,
                "availability": availability,
                "is_imax": format_ == "IMAX",
                "format": format_,
                "auditorium": ev.get("auditorium"),
                "start_time": ev.get("eventDateTime"),
                "booking_link": clean_booking_link(ev.get("bookingLink")),
                "audio_language": audio,
                "audio_language_code": audio_code,
                "subtitle_language": subtitle,
                "subtitle_language_code": subtitle_code,
            }
        )
    return normalized


# --- Booking site ----------------------------------------------------------

def booking_site_time(value) -> str | None:
    """"2026-10-20 09:00:00" or "2026-10-20 09:00" -> "2026-10-20T09:00:00",
    or None if malformed."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except (TypeError, ValueError):
            pass
    return None


def fetch_sale_info(event_id: str) -> dict:
    """Booking open/close time for an event, from the endpoint the booking
    site's order page calls before any reCAPTCHA step. The feed has no sale
    start (its compositeBookingLink.blockOnlineSales was false/null for all
    378 Flora showtimes on 2026-10-21).

    Raises FetchError. The endpoint answers HTTP 200 even for errors, so
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
            "sale_start": booking_site_time(p.get("ticketSaleStart")),
            "sale_stop": booking_site_time(p.get("ticketSaleStop")),
            "member_only": bool(p.get("isMemberSaleOnly")),
            # The hall, for the seat plan and seat status endpoints. None
            # when missing, so the caller doesn't fetch again for it.
            "venue_id": p.get("venueId"),
            "seatplan_id": p.get("seatplanId"),
            "venue_type_id": p.get("venueTypeId"),
            "is_reserved": bool(p.get("isReserved")),
        }
    err = payload.get("error")
    if not isinstance(err, dict):
        raise FetchError("invalid", f"{what}: unexpected JSON shape")
    code = err.get("error")
    if code == "TICKETING_NOT_STARTED":
        sale_start = booking_site_time(err.get("data"))
        if not sale_start:
            raise FetchError("invalid", f"{what}: locked, unreadable sale start {err.get('data')!r}")
        # "locked" makes the scraper fetch once more after the drop, for the
        # fields only an open screening returns.
        return {"sale_start": sale_start, "sale_stop": None, "member_only": False, "locked": True}
    raise FetchError("invalid", f"{what}: {code or 'unknown error'} ({err.get('message')})")


def fetch_seatplan(venue_id, seatplan_id) -> dict[str, list[str]]:
    """Every seat of a hall as {key: [row name, seat name]}. The key is
    "{section}_{x}_{y}", the same key the seat status endpoint uses. x and y
    are grid positions: seats next to each other differ by 1 in x, an aisle
    is a missing x, and the row behind has y + 1 (row 1 is at the screen).
    Halls are drawn on this grid, so the same x in the next row is directly
    behind. Seat names starting with "V" are wheelchair spaces. Checked live
    on 2026-10-25 against all 27 halls of Flora and Chodov: one section each,
    and the seat count equals the section's Capacity.

    Raises FetchError."""
    what = f"seat plan {venue_id}/{seatplan_id}"
    payload = get_json(SEATPLAN_URL.format(venue=venue_id, plan=seatplan_id), what)
    seats: dict[str, list[str]] = {}
    for section_id, section in (payload.get("sections") or {}).items():
        for group in (section.get("groups") or {}).values():
            for row in (group.get("rows") or {}).values():
                for x, seat in (row.get("seats") or {}).items():
                    key = f"{section_id}_{x}_{seat.get('YCoordinate', row.get('YCoordinate'))}"
                    seats[key] = [str(seat.get("RowName") or row.get("RowName") or ""), str(seat.get("SeatName") or "")]
    if not seats:
        raise FetchError("invalid", f"{what}: no seats ({str(payload)[:200]})")
    return seats


def fetch_free_seats(event_id: str, venue_type_id, is_reserved: bool) -> list[str]:
    """Keys (see fetch_seatplan) of the seats that are free right now. The
    endpoint lists only free seats, each with the value 0: for Dune at Flora
    IMAX on 2026-10-25 it listed 55 seats while the feed said 14.29% of 385.
    Raises FetchError."""
    what = f"seat status {event_id}"
    url = SEAT_STATUS_URL.format(id=event_id, venue_type=venue_type_id, reserved=1 if is_reserved else 0)
    seats = get_json(url, what, BOOKING_SESSION_HEADERS).get("seats")
    if seats in (None, [], {}):
        return []
    if not isinstance(seats, dict):
        raise FetchError("invalid", f"{what}: unexpected JSON shape")
    return sorted(key for key, value in seats.items() if value == 0)
