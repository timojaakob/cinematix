#!/usr/bin/env python3
"""
cinematix scraper: polls Cinema City's showtime feed for the cinema chosen
in the web UI, matches showtimes against the monitored movies, and pushes
ntfy notifications for new showtimes and freed-up seats.

Reads config.json (movies and settings, reloaded every cycle so web UI
changes apply without a restart). Writes state.json (what was already
notified, seat baselines, free seats, sale times, per-movie last check),
results.json (current matches, shown by the web UI) and seatplans.json
(hall layouts). The API client and parsing live in cinemacity.py, the
seat group rules in seats.py.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, request

import cinemacity
import seats
import storage
from cinemacity import FetchError
from version import __version__

# --- Advanced settings (env vars) ----------------------------------------

# Dates normally come from the site's date-list endpoint (see fetch_dates).
# This many days get scanned one by one instead, only as a cross-check
# (FULL_SCAN_INTERVAL_SECONDS) or as a fallback if that endpoint 404s
# (FALLBACK_SCAN_MIN_INTERVAL_SECONDS). 150 sequential day requests every
# 2 minutes ran fine for hours on 2026-10-19, so these rarer scans are safe.
FALLBACK_RANGE_DAYS = int(os.environ.get("FALLBACK_RANGE_DAYS", "150"))
DATES_LOOKAHEAD_DAYS = int(os.environ.get("DATES_LOOKAHEAD_DAYS", "365"))
FULL_SCAN_INTERVAL_SECONDS = int(os.environ.get("FULL_SCAN_INTERVAL_SECONDS", "3600"))
FALLBACK_SCAN_MIN_INTERVAL_SECONDS = int(os.environ.get("FALLBACK_SCAN_MIN_INTERVAL_SECONDS", "300"))
# Without seat data for a showtime (no seat plan, or the booking site
# fails), notify again for an already-notified event when its availability
# rises by more than this over the lowest value seen since the last
# notification - catches screenings listed before going on sale, sold-out
# shows that reopen, and seats released from expired baskets after a drop.
# availabilityRatio has 4 decimals (checked live on 2026-10-21), fine
# enough to show a single seat even in the IMAX hall, so the default 0
# means "any returned seat".
AVAILABILITY_RISE_THRESHOLD = float(os.environ.get("AVAILABILITY_RISE_THRESHOLD", "0"))
OUTAGE_ALERT_AFTER_SECONDS = int(os.environ.get("OUTAGE_ALERT_AFTER_SECONDS", "300"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# --- Constants -----------------------------------------------------------

# How often the loop wakes up. Each movie's own check_interval_seconds
# (60 s minimum, see webui.CHECK_INTERVAL_OPTIONS) decides whether it's due.
LOOP_TICK_SECONDS = 30
# Stop the cycle after this many single-day HTTP failures in a row (a lone
# bad day gets skipped instead of blocking every later date).
MAX_CONSECUTIVE_DAY_FAILURES = 3
BACKOFF_MIN_SECONDS = 60
BACKOFF_MAX_SECONDS = 900
CHECK_NOW_COOLDOWN_SECONDS = 15
# A sale start can still move until it passes, so recheck upcoming ones now
# and then. Once a showtime is on sale it's never fetched again.
PRESENTATION_REFRESH_SECONDS = 3600
# Caps the extra requests a newly monitored movie with many showtimes causes;
# the rest get fetched over the next cycles.
PRESENTATION_FETCHES_PER_CYCLE = 40
# Free seats are fetched for showtimes whose availability changed since the
# last fetch, at most this many per cycle.
SEAT_FETCHES_PER_CYCLE = 40
SEATPLAN_FETCHES_PER_CYCLE = 10
# Hall layouts hardly ever change; a seat missing from the cached layout
# also triggers a refetch.
SEATPLAN_REFRESH_SECONDS = 7 * 86400
# Internal port webui.py calls to trigger an immediate check. Bound to
# localhost: both processes run in the same container.
CONTROL_PORT = 5001

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cinematix")


# --- ntfy ----------------------------------------------------------------

def notify(title: str, message: str, url: str | None, priority: str, settings: dict) -> bool:
    """True if the push went through, or if no ntfy endpoint is set up (so
    nothing piles up to flood out once one is)."""
    endpoint = settings.get("ntfy_endpoint")
    if not endpoint:
        log.warning("No ntfy endpoint set, not sending: %s", title)
        return True
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


def public_url(settings: dict) -> str | None:
    return settings.get("public_url") or None


def cinema_from(settings: dict) -> dict:
    return {
        "id": settings["cinema_id"],
        "name": settings.get("cinema_name") or f"cinema {settings['cinema_id']}",
        "link": settings.get("cinema_link") or "",
    }


# --- Booking site ----------------------------------------------------------

def _seconds_since(iso: str | None, now: datetime) -> float:
    if not iso:
        return float("inf")
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return float("inf")


def booking_site_reachable(state: dict, now: datetime) -> bool:
    """False while backing off after the booking site blocked us. Sale times,
    seat plans and free seats all come from it, so they share the backoff."""
    return _seconds_since(state.get("booking_site_backoff_until"), now) >= 0


def booking_site_failed(state: dict, err: FetchError, now: datetime, what: str) -> None:
    """For network and blocked errors: stop asking the booking site this
    cycle, and for a while if it blocked us."""
    if err.kind == "blocked":
        until = now + timedelta(seconds=max(BACKOFF_MAX_SECONDS, err.retry_after or 0))
        state["booking_site_backoff_until"] = until.isoformat(timespec="seconds")
    log.warning("Stopped fetching %s this cycle: %s", what, err)


# --- Sale times ------------------------------------------------------------


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
        if not info.get("locked") and "venue_id" not in info:
            return True  # cached before seat plans existed
        if not info.get("sale_start"):
            return False
        if info["sale_start"] <= now_s:
            return bool(info.get("locked"))
        return _seconds_since(info.get("checked_at"), now) >= PRESENTATION_REFRESH_SECONDS

    if may_fetch and booking_site_reachable(state, now):
        # Never-fetched events first, so a new showtime's push can say when
        # booking opens.
        todo = sorted({r["event_id"] for r in results if needs_fetch(r["event_id"])}, key=lambda e: e in cache)
        for eid in todo[:PRESENTATION_FETCHES_PER_CYCLE]:
            try:
                cache[eid] = dict(cinemacity.fetch_sale_info(eid), checked_at=now_s)
            except FetchError as e:
                if e.kind in ("network", "blocked"):
                    booking_site_failed(state, e, now, "sale start times")
                    break
                log.warning("No sale start time for event %s: %s", eid, e)
                # Keeps a previously known time; retried after PRESENTATION_REFRESH_SECONDS.
                cache[eid] = dict(cache.get(eid) or {}, failed=True, checked_at=now_s)
    for r in results:
        info = cache.get(r["event_id"]) or {}
        r["sale_start"] = info.get("sale_start")
        r["sale_stop"] = info.get("sale_stop")
        r["member_only"] = info.get("member_only", False)
        for key in ("venue_id", "seatplan_id", "venue_type_id", "is_reserved"):
            r[key] = info.get(key)


# --- Seats -------------------------------------------------------------------

def areas_for(r: dict, settings: dict) -> dict:
    if r.get("venue_id") is None:
        return {}
    return storage.seat_areas(settings, storage.seatplan_key(r["venue_id"], r["seatplan_id"]))


def plan_for(r: dict, plans: dict) -> dict | None:
    """The cached layout of a showtime's hall, or None if there's no usable
    one (unknown hall, a hall without seat numbers, or a failed fetch)."""
    if r.get("venue_id") is None or not r.get("is_reserved"):
        return None
    plan = plans.get(storage.seatplan_key(r["venue_id"], r["seatplan_id"]))
    return (plan or {}).get("seats") or None


def update_seatplans(results: list[dict], plans: dict, state: dict, now: datetime, may_fetch: bool) -> bool:
    """Fetches the layout of each hall in results that isn't cached, is
    older than SEATPLAN_REFRESH_SECONDS, or was marked stale. Returns
    whether plans changed."""
    if not may_fetch or not booking_site_reachable(state, now):
        return False
    todo: dict[str, tuple] = {}
    for r in results:
        if r.get("venue_id") is None or not r.get("is_reserved"):
            continue
        key = storage.seatplan_key(r["venue_id"], r["seatplan_id"])
        plan = plans.get(key)
        if plan and not plan.get("stale") and _seconds_since(plan.get("fetched_at"), now) < SEATPLAN_REFRESH_SECONDS:
            continue
        todo[key] = (r["venue_id"], r["seatplan_id"])
    now_s = now.isoformat(timespec="seconds")
    changed = False
    for key, (venue_id, seatplan_id) in list(todo.items())[:SEATPLAN_FETCHES_PER_CYCLE]:
        try:
            plans[key] = {"seats": cinemacity.fetch_seatplan(venue_id, seatplan_id), "fetched_at": now_s}
        except FetchError as e:
            if e.kind in ("network", "blocked"):
                booking_site_failed(state, e, now, "seat plans")
                break
            log.warning("No seat plan for hall %s: %s", key, e)
            # Keeps a previously known layout; retried after SEATPLAN_REFRESH_SECONDS.
            plans[key] = {"seats": (plans.get(key) or {}).get("seats") or {}, "fetched_at": now_s}
        changed = True
    return changed


def record_free_seats(seat_state: dict, eid: str, free: list[str], ratio: float | None, now: datetime) -> None:
    """Stores a fetch of free seats. "baseline" is the low-water mark: the
    seats free in every fetch since the last push, so free minus baseline
    are the seats freed since then. "freed" keeps when each still-free seat
    came back, for the web UI."""
    now_s = now.isoformat(timespec="seconds")
    free_set = set(free)
    rec = seat_state.get(eid)
    if rec is None:
        # First look at a showtime on sale: nothing counts as freed yet.
        baseline, previous, freed = free_set, free_set, {}
    else:
        baseline = set(rec["baseline"]) & free_set
        # Seats appearing when booking opens weren't freed by anyone.
        previous = free_set if rec.get("closed") == "upcoming" else set(rec["free"])
        freed = {
            k: t for k, t in rec.get("freed", {}).items()
            if k in free_set and _seconds_since(t, now) < storage.FREED_SEATS_WINDOW_SECONDS
        }
        freed.update({k: now_s for k in free_set - previous})
    seat_state[eid] = {
        "free": sorted(free_set), "baseline": sorted(baseline), "ratio": ratio, "checked_at": now_s, "freed": freed,
    }


def update_seats(
    results: list[dict], state: dict, plans: dict, movies_by_id: dict, now: datetime, may_fetch: bool,
) -> bool:
    """Fetches the free seats of showtimes whose availability changed since
    the last fetch - all of them, not only watched ones, since the web UI
    counts free seats in the seating areas. A showtime that is sold out or not on sale yet is
    recorded with no free seats and isn't fetched, so every seat counts as
    freed once it opens. Sets r["seat_pending"] when a fetch was due but
    hit SEAT_FETCHES_PER_CYCLE, so the push waits for the seat data.
    Returns whether a hall layout was marked stale."""
    seat_state: dict[str, dict] = state.setdefault("seats", {})
    now_s = now.isoformat(timespec="seconds")
    allowed = may_fetch and booking_site_reachable(state, now)
    fetches = 0
    stale = False
    for r in results:
        eid = r["event_id"]
        if r["sold_out"] or _sale_upcoming(r, now):
            # Recorded even without a known hall: a locked showtime has no
            # hall ids until booking opens.
            seat_state[eid] = {
                "free": [], "baseline": [], "ratio": r["availability"], "checked_at": now_s, "freed": {},
                "closed": "sold" if r["sold_out"] else "upcoming",
            }
            continue
        plan = plan_for(r, plans)
        if plan is None:
            continue
        rec = seat_state.get(eid)
        if rec and not rec.get("closed") and rec.get("ratio") == r["availability"]:
            continue
        if not allowed:
            continue
        if fetches >= SEAT_FETCHES_PER_CYCLE:
            r["seat_pending"] = True
            continue
        fetches += 1
        try:
            free = cinemacity.fetch_free_seats(eid, r["venue_type_id"], r["is_reserved"])
        except FetchError as e:
            if e.kind in ("network", "blocked"):
                booking_site_failed(state, e, now, "free seats")
                allowed = False
            else:
                log.warning("No free seats for event %s: %s", eid, e)
            continue
        if any(k not in plan for k in free):
            log.info("Seat plan of hall %s/%s changed, fetching it again", r["venue_id"], r["seatplan_id"])
            plans[storage.seatplan_key(r["venue_id"], r["seatplan_id"])]["stale"] = True
            stale = True
        record_free_seats(seat_state, eid, free, r["availability"], now)
    return stale


def current_seats(r: dict, state: dict) -> dict | None:
    """The showtime's seat record if it matches its current availability."""
    rec = state.get("seats", {}).get(r["event_id"])
    if rec and not rec.get("closed") and rec.get("ratio") == r["availability"]:
        return rec
    return None


def annotate_seats(results: list[dict], state: dict, plans: dict, now: datetime, settings: dict) -> None:
    """Adds what the web UI shows, all counted inside the hall's seating
    areas (the whole hall without areas) and without wheelchair spaces:
    capacity, free_seats and seat_ratio (None until the seats were fetched
    for the current availability), freed_seats and freed_at (seats freed
    within FREED_SEATS_WINDOW_SECONDS that are still free), in_areas and
    has_seat_map."""
    for r in results:
        plan = plan_for(r, plans)
        areas = areas_for(r, settings)
        rec = current_seats(r, state)
        r["has_seat_map"] = plan is not None
        r["in_areas"] = plan is not None and any(areas.values())
        r["capacity"] = len(seats.in_areas(seats.usable(plan, plan), areas)) if plan else None
        if rec and plan and r["capacity"]:
            r["free_seats"] = len(seats.in_areas(seats.usable(rec["free"], plan), areas))
            r["seat_ratio"] = r["free_seats"] / r["capacity"]
        else:
            r["free_seats"] = r["seat_ratio"] = None
        window = storage.FREED_SEATS_WINDOW_SECONDS
        freed = [
            t for k, t in (rec or {}).get("freed", {}).items()
            if _seconds_since(t, now) < window and seats.area_of(k, areas)
            and k in (plan or {}) and not seats.is_wheelchair(plan[k][1])
        ]
        r["freed_seats"] = len(freed)
        r["freed_at"] = max(freed, default=None)


# --- Matching --------------------------------------------------------------

def matching_movie(movie_name: str, movies: list[dict]) -> dict | None:
    """Longest matching query wins, so "dune: part three" claims
    "Dune: Part Three" instead of the shorter "dune" query that is also a
    substring of it."""
    name_lower = movie_name.lower()
    matches = [m for m in movies if m["query"] and m["query"] in name_lower]
    return max(matches, key=lambda m: len(m["query"]), default=None)


def is_movie_due(movie: dict, state: dict, now: datetime) -> bool:
    """check_interval_seconds of None means checking is turned off for this
    movie. A missing key (config predating this field) defaults to on."""
    interval = movie.get("check_interval_seconds", storage.DEFAULT_CHECK_INTERVAL_SECONDS)
    if interval is None:
        return False
    return _seconds_since(storage.last_checked_at(movie, state), now) >= interval


def process_day(
    day: str,
    payload: dict,
    cinema: dict,
    movies: list[dict],
    due_ids: set[str],
    results: list[dict],
    fetched_at: str,
    first_fetched_by_event: dict[str, str],
) -> None:
    if DEBUG:
        log.debug("Raw payload for %s:\n%s", day, json.dumps(payload, indent=2)[:3000])
    for ev in cinemacity.extract_events(payload):
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
        results.append(
            {
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
                "booking_link": ev["booking_link"] or cinemacity.cinema_day_link(cinema, day),
                "audio_language": ev["audio_language"],
                "audio_language_code": ev["audio_language_code"],
                "subtitle_language": ev["subtitle_language"],
                "subtitle_language_code": ev["subtitle_language_code"],
            }
        )
        if ev["sold_out"]:
            log.info("%s: '%s' found but SOLD OUT (event %s)", day, ev["movie_name"], eid)


# --- Notifications ---------------------------------------------------------

def pick_notifications(
    fresh: list[dict], state: dict, movies_by_id: dict | None = None, plans: dict | None = None,
    settings: dict | None = None,
) -> list[dict]:
    """Which freshly fetched showtimes to notify about: never-notified ones
    that aren't sold out ("new"), plus already-notified, watched ones where
    seats freed up since their last notification ("more_seats").

    With current seat data, seats freed up means the free seats now hold a
    group of the movie's seats_wanted inside the hall's seating areas that
    uses at least one freed seat (see seats.py). Without it,
    the availability must have risen by more than
    AVAILABILITY_RISE_THRESHOLD over the lowest value seen since the last
    notification, and by at least seats_wanted seats if the hall size is
    known. Updates the availability low-water marks in state as a side
    effect; the caller resets them for the events it actually notified
    about."""
    movies_by_id = movies_by_id or {}
    plans = plans or {}
    settings = settings or {}
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
        movie = movies_by_id.get(r.get("movie_id"), {})
        if r["sold_out"] or r.get("seat_pending") or not storage.watches_showtime(movie, r):
            continue
        wanted = storage.seats_wanted(movie)
        pick = dict(r, notify_kind="more_seats", previous_availability=baselines[eid], seats_wanted=wanted)
        rec = current_seats(r, state)
        plan = plan_for(r, plans)
        if rec and plan:
            areas = areas_for(r, settings)
            free = seats.usable(rec["free"], plan)
            freed = free - set(rec["baseline"])
            groups = seats.best_groups(free, freed, wanted, areas)
            if groups:
                area, group = groups[0]
                picks.append(dict(
                    pick,
                    freed_count=len(seats.in_areas(freed, areas)),
                    free_count=len(seats.in_areas(free, areas)),
                    # Only worth naming when the hall has areas.
                    area=area if any(areas.values()) else None,
                    group=seats.describe(group, plan) if wanted > 1 else None,
                ))
            continue
        if avail - baselines[eid] <= AVAILABILITY_RISE_THRESHOLD:
            continue
        capacity = r.get("capacity")
        if capacity:
            freed_count = round((avail - baselines[eid]) * capacity)
            if freed_count < wanted:
                continue
            pick.update(freed_count=freed_count, free_count=round(avail * capacity))
        picks.append(pick)
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
        line = f"{p['movie_name']} {p['format']}, {when}: "
        if p.get("freed_count") is None:
            line += f"{p['previous_availability']:.1%} -> {p['availability']:.1%} free"
        else:
            there = " in your areas" if p.get("area") else ""
            line += f"+{p['freed_count']} seat{'s' if p['freed_count'] != 1 else ''}{there}, {p['free_count']} free now"
            if p.get("group"):
                line += f". {p['seats_wanted']} together"
                if p.get("area"):
                    line += f" in the {p['area']} area"
                line += f": {p['group']}"
        lines.append(line)
    return title, "\n".join(lines)


def push_picks(picks: list[dict], kind: str, settings: dict, now: datetime) -> bool:
    """Sends one push for every pick of this notify_kind. Returns whether
    the caller may mark them as notified: True when the push went through
    or this case is turned off (so nothing floods through once it's turned
    back on), False when the push failed and should be retried next cycle."""
    enabled_key = "notify_on_new_showtime" if kind == "new" else "notify_on_more_seats"
    if not settings.get(enabled_key, True):
        return True
    title, message = summarize_new_showtimes(picks, now) if kind == "new" else summarize_more_seats(picks)
    # A single showtime opens its booking page straight from the push.
    url = picks[0]["booking_link"] if len(picks) == 1 else public_url(settings)
    return notify(title, message, url, settings.get(f"{enabled_key}_priority", "urgent"), settings)


# --- Poll cycle ------------------------------------------------------------

CHECKING = threading.Event()
# Movie ids in the check cycle currently running, so /status can report
# per-movie checking state.
CHECKING_IDS: set[str] = set()
# Movie ids queued by the web UI's "Check now" button to force a check this
# cycle even if their own check_interval_seconds says they're not due yet.
FORCE_CHECK_LOCK = threading.Lock()
FORCE_CHECK_IDS: set[str] = set()

# Per-event and per-cinema state that must not carry over to another cinema.
CINEMA_SCOPED_STATE = (
    "notified_event_ids", "availability_baseline", "seats", "sale_info", "last_dates",
    "last_checked", "last_full_scan_at", "full_scan_pending_check",
)


def switch_cinema(state: dict, cinema_id: str) -> None:
    """A state.json from before 1.0 has no cinema_id; it belonged to the
    cinema the migrated config now names, so it's kept. Any later change
    drops the old cinema's showtimes and notification history."""
    previous = state.get("cinema_id")
    state["cinema_id"] = cinema_id
    if previous is None or previous == cinema_id:
        return
    log.info("Cinema changed from %s to %s, starting fresh", previous, cinema_id)
    for key in CINEMA_SCOPED_STATE:
        state.pop(key, None)
    state["notified_event_ids"] = []
    storage.save_results([])


def choose_dates(
    cinema_id: str, state: dict, now: datetime, result_dates: set[str]
) -> tuple[list[str], bool, FetchError | None]:
    """Returns (dates to fetch in order, whether the date list is complete
    enough to drop old showtimes on dates missing from it, error that should
    end the cycle). Always also fetches the previous valid list's future dates
    and every date we currently hold monitored showtimes for, so a short or
    flaky list can never hide a known screening."""
    today = now.date()
    today_s = today.isoformat()
    previous = sorted({d for d in state.get("last_dates", []) if d >= today_s} | result_dates)
    full_scan_days = [(today + timedelta(days=i)).isoformat() for i in range(FALLBACK_RANGE_DAYS)]
    until = (today + timedelta(days=DATES_LOOKAHEAD_DAYS)).isoformat()
    try:
        listed = [d for d in cinemacity.fetch_dates(cinema_id, until) if d >= today_s]
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
        # with screenings - see run_once's warning.
        state["last_full_scan_at"] = now.isoformat(timespec="seconds")
        state["full_scan_pending_check"] = listed
        dates |= set(full_scan_days)
    return sorted(dates), True, None


def handle_blocked(state: dict, err: FetchError, now: datetime) -> None:
    backoff = min(max(state.get("backoff_seconds", 0) * 2, BACKOFF_MIN_SECONDS), BACKOFF_MAX_SECONDS)
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
    if not cycle_failed:
        if state.get("failing_since"):
            down_min = round(_seconds_since(state["failing_since"], now) / 60)
            if state.get("outage_alert_sent"):
                notify(
                    "cinematix back online", f"Cinema City reachable again after {down_min} min.",
                    public_url(settings), "default", settings,
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
    if notify(title, f"No successful check since {since}. Check for tickets manually.",
              public_url(settings), "high", settings):
        state["outage_alert_sent"] = True


def run_once(state: dict) -> None:
    global CHECKING_IDS
    CHECKING.set()
    try:
        config = storage.load_config()
        settings = config["settings"]
        if not storage.is_set_up(config):
            log.debug("Not set up yet (no cinema chosen), skipping cycle")
            return
        cinema = cinema_from(settings)
        switch_cinema(state, cinema["id"])
        now = cinemacity.now()
        with FORCE_CHECK_LOCK:
            forced_ids = set(FORCE_CHECK_IDS)
            FORCE_CHECK_IDS.clear()
        due_movies = [m for m in config["movies"] if m["id"] in forced_ids or is_movie_due(m, state, now)]
        if not due_movies:
            storage.save_state(state)
            return
        if _seconds_since(state.get("backoff_until"), now) < 0:
            log.info("Backing off until %s (blocked earlier), skipping cycle", state["backoff_until"])
            return
        due_ids = {m["id"] for m in due_movies}
        CHECKING_IDS = set(due_ids)

        today_s = now.date().isoformat()
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
        dates, list_complete, cycle_error = choose_dates(cinema["id"], state, now, result_dates)
        fetched_days: set[str] = set()
        days_with_events: set[str] = set()
        consecutive_failures = 0
        for day in dates:
            try:
                payload = cinemacity.fetch_day(cinema["id"], day)
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
            if cinemacity.extract_events(payload):
                days_with_events.add(day)
            process_day(
                day, payload, cinema, config["movies"], due_ids, results, fetched_at, first_fetched_by_event,
            )
        if cycle_error:
            log.warning("Cycle stopped early (%s), keeping previous results for unfetched days", cycle_error)
        # Offline or blocked: don't add requests to another host on top, use
        # cached sale times only.
        may_fetch = not (cycle_error and cycle_error.kind in ("network", "blocked"))
        attach_sale_info(results, state, now, may_fetch)
        # The same event can come back for two dates (and so twice in a
        # cycle) - keep one per event_id.
        fresh = list({r["event_id"]: r for r in results}.values())
        movies_by_id = {m["id"]: m for m in config["movies"]}
        plans = storage.load_seatplans()
        plans_changed = update_seatplans(fresh, plans, state, now, may_fetch)
        plans_changed |= update_seats(fresh, state, plans, movies_by_id, now, may_fetch)
        if plans_changed:
            storage.save_seatplans(plans)
        annotate_seats(fresh, state, plans, now, settings)
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
        # An event can be both fresh and preserved - prefer the fresh entry.
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
            last_checked = state.setdefault("last_checked", {})
            for movie_id in due_ids:
                last_checked[movie_id] = fetched_at

        # Notify about whatever was found even if the cycle failed partway -
        # a broken later day must never hold back a ticket drop.
        picks = pick_notifications(fresh, state, movies_by_id, plans, settings)
        for kind in ("new", "more_seats"):
            group = [p for p in picks if p["notify_kind"] == kind]
            # Only mark these as notified if the push actually went through, so a
            # failed/rate-limited push gets retried next cycle instead of being
            # silently dropped.
            if group and push_picks(group, kind, settings, now):
                for p in group:
                    if kind == "new":
                        state["notified_event_ids"].append(p["event_id"])
                    if p["availability"] is not None:
                        state["availability_baseline"][p["event_id"]] = p["availability"]
                    seat_rec = state.get("seats", {}).get(p["event_id"])
                    if seat_rec:
                        seat_rec["baseline"] = seat_rec["free"]
        # Only track baselines, seats and sale times for showtimes still in the results.
        live_ids = {r["event_id"] for r in combined}
        for key in ("availability_baseline", "seats", "sale_info"):
            state[key] = {k: v for k, v in state.get(key, {}).items() if k in live_ids}
        state["last_checked"] = {k: v for k, v in state.get("last_checked", {}).items() if k in configured_ids}

        update_health(state, cycle_failed, blocked, now, settings)
        storage.save_state(state)
    finally:
        CHECKING.clear()
        CHECKING_IDS = set()


def start_control_server(wake_event: threading.Event) -> None:
    """Internal HTTP control port so webui.py can wake the poll loop early
    instead of waiting out the full interval."""
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

    control_app.run(host="127.0.0.1", port=CONTROL_PORT)


def main() -> None:
    config = storage.load_config()
    settings = config["settings"]
    titles = [m["title"] for m in config["movies"]]
    log.info("Starting cinematix %s: cinema=%s movies=%r", __version__, settings.get("cinema_id") or "-", titles)
    state = storage.load_state()
    if storage.is_set_up(config) and settings.get("notify_on_start", True):
        notify(
            "cinematix started",
            f"Monitoring {', '.join(titles) or 'nothing yet'} at {cinema_from(settings)['name']}",
            public_url(settings),
            settings.get("notify_on_start_priority", "low"),
            settings,
        )

    wake_event = threading.Event()
    threading.Thread(target=start_control_server, args=(wake_event,), daemon=True).start()

    while True:
        try:
            run_once(state)
        except Exception:
            log.exception("Unhandled error during poll cycle")
        wake_event.wait(LOOP_TICK_SECONDS)
        wake_event.clear()


if __name__ == "__main__":
    sys.exit(main())
