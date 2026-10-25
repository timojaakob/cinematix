"""
Everything cinematix keeps on disk, all in DATA_DIR:

- config.json  - movies and settings. Written only by webui.py.
- state.json   - scraper bookkeeping (notified events, seat baselines, sale
                 times, per-movie last check). Written only by scraper.py.
- results.json - the latest matching showtimes. Written only by scraper.py.
- seatplans.json - hall layouts from the booking site, by venue. Written
                 only by scraper.py.
- secret_key   - signs the web UI's login session.

One writer per file, so the two processes can't overwrite each other's
read-modify-write.
"""

import json
import logging
import os
import secrets
import uuid
from datetime import datetime
from pathlib import Path

import cinemacity

log = logging.getLogger("cinematix")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
RESULTS_FILE = DATA_DIR / "results.json"
STATE_FILE = DATA_DIR / "state.json"
SEATPLANS_FILE = DATA_DIR / "seatplans.json"
SECRET_FILE = DATA_DIR / "secret_key"

# Formats scraper.py can tell apart (see cinemacity.detect_format). A
# movie's required_formats is a subset of this list; empty means the movie
# matches nothing until the user picks at least one.
FORMAT_OPTIONS = ["IMAX", "4DX", "Superscreen", "VIP", "3D", "2D"]
DEFAULT_CHECK_INTERVAL_SECONDS = 120

# ntfy's own priority levels (https://docs.ntfy.sh/publish/#message-priority).
NTFY_PRIORITY_OPTIONS = ["min", "low", "default", "high", "urgent"]

VIEW_OPTIONS = ["list", "calendar"]

# Which showtimes of a movie can send a "seats freed up" push: all of them,
# or the ones matching the movie's watch rules or picked with the bells on
# the movie page ("custom").
SEAT_ALERT_OPTIONS = ["all", "custom"]
MAX_WATCH_RULES = 20

# Seating areas per hall (storage.seatplan_key), as rectangles on the seat
# grid - see seats.py. Used until the user saves areas for that hall in the
# seat map. Flora's IMAX hall (venue 80) is from the owner's drawing of
# 2026-10-25: preferred rows 5-8, acceptable rows 4-12. Its rows are
# symmetric around grid column 20.5 (row 8 is columns 2-39), so both areas
# have an even width to sit in the middle: 12 and 16 seats.
DEFAULT_SEAT_AREAS = {
    "80/1": {
        "preferred": {"rows": [5, 8], "cols": [15, 26]},
        "acceptable": {"rows": [4, 12], "cols": [13, 28]},
    },
}
# How long the web UI marks freed seats as freed.
FREED_SEATS_WINDOW_SECONDS = 24 * 3600

# All set in the web UI (the first-run wizard, then the settings dialog).
# An empty cinema_id means setup hasn't been completed.
DEFAULT_SETTINGS = {
    "cinema_id": "",
    "cinema_name": "",
    "cinema_link": "",
    "public_url": "",
    "password_hash": "",
    "tmdb_api_key": "",
    "ntfy_endpoint": "",
    "notify_on_start": True,
    "notify_on_start_priority": "low",
    "notify_on_new_showtime": True,
    "notify_on_new_showtime_priority": "urgent",
    "notify_on_more_seats": True,
    "notify_on_more_seats_priority": "urgent",
    "default_check_interval_seconds": DEFAULT_CHECK_INTERVAL_SECONDS,
    "default_view": "list",
}


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call, so two writers never share a tmp file. .replace()
    # itself is atomic.
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(payload)
    tmp.replace(path)


def _read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("%s is corrupt, starting fresh", path.name)
    return default


# --- config.json -----------------------------------------------------------

def _legacy_env_settings() -> dict:
    """Before 1.0 the cinema, TMDb key, ntfy topic and web UI URL came from
    env vars, and the cinema was Prague Flora unless CINEMA_ID said
    otherwise. A config from then has no cinema_id key; it gets these once
    so an upgraded install keeps working without the setup wizard."""
    env = os.environ
    ntfy = env.get("NTFY_URL") or (f"https://ntfy.sh/{env['NTFY_TOPIC']}" if env.get("NTFY_TOPIC") else "")
    return {
        "cinema_id": env.get("CINEMA_ID", "1052"),
        "tmdb_api_key": env.get("TMDB_API_KEY", ""),
        "ntfy_endpoint": ntfy,
        "public_url": env.get("WEBUI_URL", ""),
    }


def load_config() -> dict:
    raw = _read_json(CONFIG_FILE, None)
    if not isinstance(raw, dict):
        return {"movies": [], "settings": dict(DEFAULT_SETTINGS)}

    movies = raw.get("movies", [])
    for m in movies:
        # Configs from before per-movie formats wanted IMAX.
        m.setdefault("required_formats", ["IMAX"])
        # "selected" was the bells-only mode before watch rules.
        if m.get("seat_alerts") == "selected":
            m["seat_alerts"] = "custom"
    stored = raw.get("settings", {})
    settings = {**DEFAULT_SETTINGS, **stored}
    if "cinema_id" not in stored:
        for key, value in _legacy_env_settings().items():
            if not settings.get(key):
                settings[key] = value
    return {"movies": movies, "settings": settings}


def save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2))


def is_set_up(cfg: dict) -> bool:
    return bool(cfg["settings"].get("cinema_id"))


def add_movie(cfg: dict, movie: dict) -> dict:
    cfg["movies"] = [m for m in cfg["movies"] if m["id"] != movie["id"]] + [movie]
    save_config(cfg)
    return cfg


def remove_movie(cfg: dict, movie_id: str) -> dict:
    cfg["movies"] = [m for m in cfg["movies"] if m["id"] != movie_id]
    save_config(cfg)
    return cfg


def update_movie_settings(
    cfg: dict,
    movie_id: str,
    new_query: str,
    check_interval_seconds: int | None,
    required_formats: list[str],
    seats_wanted: int = 1,
    seat_alerts: str = "all",
    watch_rules: list[dict] | None = None,
) -> dict:
    for m in cfg["movies"]:
        if m["id"] == movie_id:
            m["query"] = new_query.strip().lower()
            m["check_interval_seconds"] = check_interval_seconds
            m["required_formats"] = required_formats
            m["seats_wanted"] = seats_wanted
            m["seat_alerts"] = seat_alerts
            m["watch_rules"] = watch_rules or []
    save_config(cfg)
    return cfg


def set_watched(cfg: dict, movie_id: str, picks: dict[str, list[str]]) -> dict:
    """picks has watched_dates, watched_event_ids and unwatched_event_ids
    (the bells on the movie page)."""
    for m in cfg["movies"]:
        if m["id"] == movie_id:
            m.update(picks)
    save_config(cfg)
    return cfg


def seat_areas(settings: dict, hall: str) -> dict:
    """The hall's seating areas: what the user saved, else the default."""
    saved = settings.get("seat_areas", {})
    return saved[hall] if hall in saved else DEFAULT_SEAT_AREAS.get(hall, {})


def set_seat_areas(cfg: dict, hall: str, areas: dict) -> dict:
    """An empty dict is kept: it means "no areas", also for a hall with a default."""
    cfg["settings"].setdefault("seat_areas", {})[hall] = areas
    save_config(cfg)
    return cfg


def seats_wanted(movie: dict) -> int:
    """How many seats together a "seats freed up" push needs. 1, the
    default, means any freed seat."""
    return movie.get("seats_wanted") or 1


def matches_rule(rule: dict, start: datetime) -> bool:
    """rule: {"days": ISO weekdays 1-7, "from"/"to": "HH:MM" start time
    window, "date_from"/"date_to": "YYYY-MM-DD" or "" for open-ended}."""
    day = start.date().isoformat()
    hhmm = start.strftime("%H:%M")
    return (
        start.isoweekday() in rule.get("days", range(1, 8))
        and rule.get("from", "00:00") <= hhmm <= rule.get("to", "23:59")
        and (not rule.get("date_from") or day >= rule["date_from"])
        and (not rule.get("date_to") or day <= rule["date_to"])
    )


def watches_showtime(movie: dict, match: dict) -> bool:
    """Whether a showtime can send a "seats freed up" push. In "custom" mode
    a showtime is watched if it matches a watch rule or its date or itself
    was picked with a bell, unless its bell was turned off. A picked date
    (the start date, as the movie page groups showtimes) also covers
    showtimes listed on it later."""
    if movie.get("seat_alerts", "all") == "all":
        return True
    eid = match.get("event_id")
    if eid in movie.get("unwatched_event_ids", []):
        return False
    try:
        start = datetime.fromisoformat(match.get("start_time") or "")
    except ValueError:
        return False
    return (
        start.date().isoformat() in movie.get("watched_dates", [])
        or eid in movie.get("watched_event_ids", [])
        or any(matches_rule(rule, start) for rule in movie.get("watch_rules", []))
    )


def update_settings(cfg: dict, updates: dict) -> dict:
    cfg["settings"].update(updates)
    save_config(cfg)
    return cfg


# --- state.json --------------------------------------------------------------

def load_state() -> dict:
    state = _read_json(STATE_FILE, {})
    state.setdefault("notified_event_ids", [])
    return state


def save_state(state: dict) -> None:
    _atomic_write(STATE_FILE, json.dumps(state))


def last_checked_at(movie: dict, state: dict) -> str | None:
    # Before 1.0 this lived on the movie in config.json.
    return state.get("last_checked", {}).get(movie["id"]) or movie.get("last_checked_at")


# --- results.json ------------------------------------------------------------

def load_results() -> dict:
    return _read_json(RESULTS_FILE, {"updated_at": None, "matches": []})


def save_results(results: list[dict]) -> None:
    payload = {"updated_at": cinemacity.now().isoformat(timespec="seconds"), "matches": results}
    _atomic_write(RESULTS_FILE, json.dumps(payload, indent=2))


# --- seatplans.json ------------------------------------------------------------

def seatplan_key(venue_id, seatplan_id) -> str:
    return f"{venue_id}/{seatplan_id}"


def load_seatplans() -> dict:
    """{seatplan_key: {"seats": {seat key: [row, seat]}, "fetched_at": ...}}"""
    plans = _read_json(SEATPLANS_FILE, {})
    return plans if isinstance(plans, dict) else {}


def save_seatplans(plans: dict) -> None:
    _atomic_write(SEATPLANS_FILE, json.dumps(plans))


# --- secret_key --------------------------------------------------------------

def secret_key() -> str:
    """Created once with O_EXCL, so concurrent first starts agree on it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return SECRET_FILE.read_text().strip()
    with os.fdopen(fd, "w") as f:
        key = secrets.token_hex(32)
        f.write(key)
    return key
