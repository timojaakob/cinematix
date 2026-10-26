#!/usr/bin/env python3
"""
cinematix web UI: first-run setup, searching TMDb to add movies to monitor
(config.json, picked up by scraper.py on its next cycle), and each movie's
matching showtimes (results.json) with booking links.

Optionally protected by a password set during setup. Every POST must come
from the app's own pages (same-origin check), so other websites can't
change settings through a logged-in browser.
"""

import io
import json
import os
import re
import secrets
import string
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import requests
import segno
from flask import (
    Flask, abort, jsonify, redirect, render_template, request, send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import cinemacity
import seats
import storage
from cinemacity import FetchError
from version import __version__

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"
SCRAPER_CONTROL_URL = "http://127.0.0.1:5001"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"

# (seconds, label) options for the check-frequency dropdowns. The server
# only accepts these, so no install can poll Cinema City more than once a
# minute per movie.
CHECK_INTERVAL_OPTIONS = [
    (60, "Every minute"),
    (120, "Every 2 minutes"),
    (300, "Every 5 minutes"),
    (600, "Every 10 minutes"),
    (900, "Every 15 minutes"),
    (1800, "Every 30 minutes"),
    (3600, "Every hour"),
    (7200, "Every 2 hours"),
    (14400, "Every 4 hours"),
    (21600, "Every 6 hours"),
    (43200, "Every 12 hours"),
    (86400, "Every day"),
]
MIN_PASSWORD_LENGTH = 8

app = Flask(__name__)
app.secret_key = storage.secret_key()
app.config.update(
    # Static URLs carry a ?v=<mtime> cache-buster (see asset()), so browsers
    # can keep them for a year and still pick up a new deploy immediately.
    SEND_FILE_MAX_AGE_DEFAULT=31536000,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=90),
)

# A showtime first seen within this window gets a "New" badge.
NEW_SHOWTIME_WINDOW = timedelta(hours=24)


# --- Request guards ----------------------------------------------------------

# Reachable without login and before setup: the setup wizard itself, the
# login page, health checks and static files (the service worker and its
# loading shell contain no data).
OPEN_ENDPOINTS = {"static", "healthz", "service_worker", "shell", "login"}
SETUP_ENDPOINTS = {"setup", "setup_check_tmdb", "setup_qr", "test_notification"}


def _same_origin() -> bool:
    """Browsers send Origin on every POST from a form or fetch(). Only the
    host is compared, not the scheme: behind a TLS reverse proxy the app
    sees http while the browser sends https. A request with neither header
    isn't from a browser (curl, scripts), so it can't be CSRF."""
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True
    return urlparse(source).netloc == request.host


@app.before_request
def guard():
    if request.method == "POST" and not _same_origin():
        abort(403)
    endpoint = request.endpoint or ""
    if endpoint in OPEN_ENDPOINTS:
        return None
    config = storage.load_config()
    if not storage.is_set_up(config):
        if endpoint in SETUP_ENDPOINTS:
            return None
        return redirect(url_for("setup"))
    if config["settings"].get("password_hash") and not session.get("auth"):
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": "login required"}), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return None


def _safe_next(value: str | None) -> str:
    """Only same-site paths, so a crafted link can't bounce to another site."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return url_for("index")


# --- Template helpers ----------------------------------------------------------

@app.context_processor
def template_helpers():
    def asset(filename: str) -> str:
        try:
            version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            version = 0
        return url_for("static", filename=filename, v=version)

    config = storage.load_config()
    settings = config["settings"]

    def global_settings() -> dict:
        """What the settings dialog on every app page needs. Called by the
        dialog itself, so a new page doesn't have to pass anything."""
        return {
            "settings": page_settings(config),
            "cinemas": cinema_list(),
            "check_interval_options": CHECK_INTERVAL_OPTIONS,
            "ntfy_priority_options": storage.NTFY_PRIORITY_OPTIONS,
        }

    return {
        "asset": asset,
        "global_settings": global_settings,
        "interval_labels": dict(CHECK_INTERVAL_OPTIONS),
        # "Prague Flora, OC FLORA" -> "Cinema City Prague Flora"
        "cinema_name": " ".join(["Cinema City", (settings.get("cinema_name") or "").split(",")[0]]).strip(),
        "has_password": bool(settings.get("password_hash")),
        "app_version": __version__,
    }


def is_new_showtime(match: dict, now) -> bool:
    try:
        return now - datetime.fromisoformat(match.get("fetched_at") or "") < NEW_SHOWTIME_WINDOW
    except ValueError:
        return False


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


@app.template_filter("humandate")
def humandate(value: str | None, with_time: bool = False, with_year: bool = True) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    formatted = dt.strftime(f"%B {_ordinal(dt.day)}")
    if with_year:
        formatted += dt.strftime(" %Y")
    if with_time:
        formatted += dt.strftime(" %H:%M")
    return formatted


@app.template_filter("when")
def when(value: str | None, fmt: str) -> str:
    """An ISO date or time from results.json in strftime format."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime(fmt)
    except ValueError:
        return value


@app.template_filter("relativedate")
def relativedate(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    seconds = max((cinemacity.now() - dt).total_seconds(), 0)
    if seconds < 30:
        return "just now"
    minutes = int((seconds + 30) // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = int((seconds + 1800) // 3600)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int((seconds + 43200) // 86400)
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return humandate(value, with_time=True)


# --- Input sanitizing ----------------------------------------------------------

def sanitize_required_formats(values: list[str]) -> list[str]:
    """Keeps only known formats in storage.FORMAT_OPTIONS order."""
    return [f for f in storage.FORMAT_OPTIONS if f in values]


def sanitize_seats_wanted(raw: str, fallback: int) -> int:
    try:
        n = int(raw)
    except ValueError:
        return fallback
    return n if 1 <= n <= seats.MAX_PARTY_SIZE else fallback


EVENT_ID_RE = re.compile(r"^[0-9A-Za-z-]{1,32}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_WATCHED = 500


def _iso_date(value) -> str | None:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def sanitize_watched(body: dict) -> dict[str, list[str]]:
    """The bells' picks: valid ISO dates and event ids, without duplicates."""
    dates = body.get("watched_dates")
    clean_dates = sorted({d for d in map(_iso_date, dates if isinstance(dates, list) else []) if d})
    picks = {"watched_dates": clean_dates[:MAX_WATCHED]}
    for key in ("watched_event_ids", "unwatched_event_ids"):
        ids = body.get(key)
        clean: list[str] = []
        for e in ids if isinstance(ids, list) else []:
            if isinstance(e, str) and EVENT_ID_RE.match(e) and e not in clean:
                clean.append(e)
        picks[key] = clean[:MAX_WATCHED]
    return picks


def sanitize_watch_rules(raw: str) -> list[dict]:
    """The movie settings' watch rules, sent as JSON. Invalid fields fall
    back to "any"; a rule without days is dropped."""
    try:
        rules = json.loads(raw or "[]")
    except ValueError:
        return []
    clean = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict):
            continue
        days = sorted({d for d in rule.get("days", []) if isinstance(d, int) and 1 <= d <= 7})
        if not days:
            continue
        start = rule.get("from") if TIME_RE.fullmatch(str(rule.get("from", ""))) else "00:00"
        end = rule.get("to") if TIME_RE.fullmatch(str(rule.get("to", ""))) else "23:59"
        clean.append({
            "days": days, "from": start, "to": end,
            "date_from": _iso_date(rule.get("date_from")) or "",
            "date_to": _iso_date(rule.get("date_to")) or "",
        })
    return clean[:storage.MAX_WATCH_RULES]


def sanitize_area(value) -> dict | None:
    """{"rows": [y1, y2], "cols": [x1, x2]} with small positive ints, or None."""
    if not isinstance(value, dict):
        return None
    try:
        rows = sorted(int(v) for v in value["rows"])
        cols = sorted(int(v) for v in value["cols"])
    except (KeyError, TypeError, ValueError):
        return None
    if len(rows) != 2 or len(cols) != 2 or not all(0 < v < 1000 for v in rows + cols):
        return None
    return {"rows": rows, "cols": cols}


def sanitize_interval(raw: str, fallback: int | None) -> int | None:
    """"off" -> None; any value not in CHECK_INTERVAL_OPTIONS -> fallback."""
    if raw == "off":
        return None
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value in dict(CHECK_INTERVAL_OPTIONS) else fallback


def sanitize_ntfy_priority(value: str, fallback: str) -> str:
    return value if value in storage.NTFY_PRIORITY_OPTIONS else fallback


def sanitize_default_view(value: str, fallback: str) -> str:
    return value if value in storage.VIEW_OPTIONS else fallback


def sanitize_http_url(value: str) -> str:
    """An http(s) URL with a host, or "" for anything else."""
    value = (value or "").strip()
    parsed = urlparse(value)
    return value if parsed.scheme in ("http", "https") and parsed.netloc else ""


# --- Scraper control -------------------------------------------------------------

def trigger_check(movie_id: str | None = None) -> None:
    """Wakes the scraper's poll loop early and forces it to (re)check
    movie_id this cycle regardless of its own check_interval_seconds - best
    effort, since the scraper may be restarting."""
    try:
        requests.post(f"{SCRAPER_CONTROL_URL}/check-now", json={"movie_id": movie_id}, timeout=5)
    except requests.RequestException:
        pass


# --- Cinemas ---------------------------------------------------------------------

_cinema_cache: dict = {"at": None, "cinemas": []}


def cinema_list() -> list[dict]:
    """Cached for an hour; an empty list if Cinema City can't be reached."""
    now = cinemacity.now()
    if _cinema_cache["at"] and (now - _cinema_cache["at"]).total_seconds() < 3600 and _cinema_cache["cinemas"]:
        return _cinema_cache["cinemas"]
    until = (now.date() + timedelta(days=365)).isoformat()
    try:
        _cinema_cache["cinemas"] = cinemacity.fetch_cinemas(until)
        _cinema_cache["at"] = now
    except cinemacity.FetchError as e:
        app.logger.warning("Cinema list unavailable: %s", e)
    return _cinema_cache["cinemas"]


def cinema_settings(cinema_id: str) -> dict | None:
    cinema = next((c for c in cinema_list() if c["id"] == cinema_id), None)
    if cinema is None:
        return None
    return {"cinema_id": cinema["id"], "cinema_name": cinema["name"], "cinema_link": cinema["link"]}


def fill_cinema_details(config: dict) -> None:
    """Configs upgraded from before 1.0 have a cinema id but no name/link."""
    settings = config["settings"]
    if settings.get("cinema_id") and not settings.get("cinema_name"):
        details = cinema_settings(settings["cinema_id"])
        if details:
            storage.update_settings(config, details)


# --- TMDb ------------------------------------------------------------------------

def tmdb_get(path: str, key: str, params: dict | None = None) -> dict:
    """Accepts both kinds of TMDb credential: the short v3 "API key" and the
    long v4 "API Read Access Token" (a JWT, starts with "eyJ"). Raises
    requests.RequestException."""
    params = dict(params or {})
    headers = {}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    resp = requests.get(f"{TMDB_BASE}{path}", params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_tmdb_api_key() -> str:
    return storage.load_config()["settings"].get("tmdb_api_key", "")


def seat_ratio(match: dict) -> float:
    """Share of free seats: in the seating areas when the scraper has the
    seats, else the feed's availability for the whole hall."""
    if match.get("seat_ratio") is not None:
        return match["seat_ratio"]
    return match.get("availability") or 0.0


# --- Showtime status -------------------------------------------------------------
# The one place for what a showtime shows: its tier (the green / amber / red
# light), its seat text and whether it changed lately. The movie list, the
# movie page's rows, its calendar and the tour all draw from these fields,
# so a colour means the same thing on every screen.

# Anything that displays as 0% free counts as sold out.
SOLD_OUT_BELOW = 0.005
SEAT_STRIP_LENGTH = 20
HEAT_STRIP_DAYS = 14


def tier_of_ratio(ratio: float | None) -> str:
    if ratio is None:
        return "none"
    if ratio < SOLD_OUT_BELOW:
        return "sold"
    if ratio >= 0.5:
        return "good"
    if ratio >= 0.15:
        return "fair"
    return "low"


def sale_upcoming(match: dict, now_s: str) -> bool:
    """sale_start is Prague local time without an offset, like now_s."""
    return (match.get("sale_start") or "") > now_s


def is_sold_out(match: dict, now_s: str) -> bool:
    """A literal sold_out flag and anything that displays as 0% read the
    same. Before booking opens, the low pre-sale ratio doesn't mean sold out."""
    if sale_upcoming(match, now_s):
        return False
    availability = match.get("availability")
    return bool(match.get("sold_out")) or (availability is not None and availability < SOLD_OUT_BELOW)


def short_when(value: str) -> str:
    """"2026-10-31T10:15:00" -> "Sat 31 Oct, 10:15"."""
    return datetime.fromisoformat(value).strftime("%a %-d %b, %H:%M")


def lit_seats(ratio: float | None) -> int:
    """How many of the seat strip's 20 seats are lit: at least one while
    any seat is free."""
    if ratio is None or ratio < SOLD_OUT_BELOW:
        return 0
    return max(1, int(ratio * SEAT_STRIP_LENGTH + 0.5))


def showtime_view(match: dict, now: datetime) -> dict:
    """A showtime from results.json plus what the pages show for it."""
    now_s = now.isoformat(timespec="seconds")
    upcoming = sale_upcoming(match, now_s)
    sold = is_sold_out(match, now_s)
    ratio = match["seat_ratio"] if match.get("seat_ratio") is not None else match.get("availability")
    tier = "upcoming" if upcoming else "sold" if sold else tier_of_ratio(ratio)
    if upcoming:
        text = f"Opens {short_when(match['sale_start'])}"
    elif sold:
        text = "Sold out"
    elif match.get("free_seats") is not None:
        text = f"{match['free_seats']} of {match.get('capacity')} {'in your area' if match.get('in_areas') else 'free'}"
    elif match.get("availability") is not None:
        text = f"{round(match['availability'] * 100)}% free"
    else:
        text = "Seats unknown"
    is_new = not sold and is_new_showtime(match, now)
    freed = 0 if sold else (match.get("freed_seats") or 0)
    return {
        **match,
        "tier": tier,
        "sale_upcoming": upcoming,
        "sold": sold,
        "ratio": ratio,
        "lit": 0 if tier in ("upcoming", "sold") else lit_seats(ratio),
        "seat_text": text,
        "is_new": is_new,
        "freed": freed,
        "changed": is_new or freed > 0,
    }


def is_open(view: dict) -> bool:
    """On sale, not sold out, and with a known share of free seats."""
    return not view["sale_upcoming"] and not view["sold"] and view["ratio"] is not None


def day_tier(views: list[dict]) -> str:
    """A day's light: its best open showtime, else "upcoming" while
    booking hasn't opened for any of them, else sold out."""
    open_views = [v for v in views if is_open(v)]
    if not open_views:
        return "upcoming" if any(v["sale_upcoming"] for v in views) else "sold"
    return max(open_views, key=lambda v: v["ratio"])["tier"]


def card_status_tier(showtime_count: int, best_availability: float | None) -> str:
    """A movie's light on the movie list: its best open showtime. No free
    seat (in the seating areas) reads like sold out, as on the movie page."""
    if showtime_count == 0:
        return "none"
    if best_availability is None:
        return "sold"
    return tier_of_ratio(best_availability)


def movie_card(movie: dict, matches: list[dict], state: dict, now: datetime) -> dict:
    """One movie on the movie list: its light, counts, badges, and for the
    desktop board its next bookable showtimes and the next two weeks."""
    views = sorted((showtime_view(m, now) for m in matches), key=lambda v: v.get("start_time") or "")
    upcoming = [v for v in views if v["sale_upcoming"]]
    open_views = [v for v in views if is_open(v)]
    best = max(open_views, key=lambda v: v["ratio"], default=None)
    best_ratio = best["ratio"] if best else None
    by_day: dict[str, list[dict]] = {}
    for v in views:
        by_day.setdefault((v.get("start_time") or "")[:10], []).append(v)
    days = []
    for i in range(HEAT_STRIP_DAYS):
        day = now.date() + timedelta(days=i)
        shows = by_day.get(day.isoformat(), [])
        days.append({
            "date": day.isoformat(),
            "label": day.strftime("%a %-d %b"),
            "tier": day_tier(shows) if shows else "empty",
            "count": len(shows),
        })
    next_sale_start = min((v["sale_start"] for v in upcoming), default=None)
    return {
        **movie,
        "last_checked_at": storage.last_checked_at(movie, state),
        "showtime_count": len(views),
        "open_count": len(open_views),
        "upcoming_count": len(upcoming),
        "next_sale_start": datetime.fromisoformat(next_sale_start) if next_sale_start else None,
        "new_count": sum(1 for v in open_views if v["is_new"]),
        "freed_seats": sum(v["freed"] for v in open_views),
        "best_availability": best_ratio,
        "best_lit": lit_seats(best_ratio),
        "best_free_seats": best.get("free_seats") if best else None,
        "best_in_areas": bool(best and best.get("in_areas")),
        "status_tier": card_status_tier(len(views), best_ratio),
        "next_shows": [v for v in open_views if v["tier"] != "sold"][:3],
        "days": days,
    }


def tmdb_search(query: str) -> list[dict]:
    if not query.strip():
        return []
    try:
        data = tmdb_get("/search/movie", get_tmdb_api_key(), {"query": query, "include_adult": "false"})
    except requests.RequestException:
        return []
    return [
        {
            "tmdb_id": r["id"],
            "title": r.get("title") or r.get("original_title") or "",
            "year": (r.get("release_date") or "")[:4],
            "poster_thumb": f"{TMDB_IMG_BASE}/w92{r['poster_path']}" if r.get("poster_path") else None,
            "overview": r.get("overview") or "",
        }
        for r in data.get("results", [])
    ]


def tmdb_details(tmdb_id: int) -> dict | None:
    try:
        d = tmdb_get(f"/movie/{tmdb_id}", get_tmdb_api_key())
    except requests.RequestException:
        return None
    return {
        "title": d.get("title") or d.get("original_title") or "",
        "overview": d.get("overview") or "",
        "runtime": d.get("runtime"),
        "poster_url": f"{TMDB_IMG_BASE}/w342{d['poster_path']}" if d.get("poster_path") else None,
        "release_date": d.get("release_date") or "",
        "imdb_id": d.get("imdb_id") or None,
        "rating": round(d["vote_average"], 1) if d.get("vote_average") else None,
    }


def page_settings(config: dict) -> dict:
    """Settings as the templates see them - never the password hash."""
    return {k: v for k, v in config["settings"].items() if k != "password_hash"}


# --- Open routes -------------------------------------------------------------------

@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok"


@app.route("/sw.js", methods=["GET"])
def service_worker():
    """Served from the root (not /static/) so its scope covers every page.
    no-cache: browsers must revalidate it, so a new deploy takes effect on
    the next visit."""
    resp = send_from_directory(app.static_folder, "sw.js", max_age=0)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/shell", methods=["GET"])
def shell():
    """Loading placeholder that sw.js caches and shows instantly when the
    app opens; it contains no data, only the page frame (see shell.html)."""
    return render_template("shell.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    config = storage.load_config()
    password_hash = config["settings"].get("password_hash")
    next_path = _safe_next(request.values.get("next"))
    if not password_hash or session.get("auth"):
        return redirect(next_path)
    error = None
    if request.method == "POST":
        if check_password_hash(password_hash, request.form.get("password", "")):
            session.clear()
            session["auth"] = True
            session.permanent = True
            return redirect(next_path)
        error = "Wrong password."
    return render_template("login.html", error=error, next_path=next_path), (401 if error else 200)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Setup ---------------------------------------------------------------------

SETUP_STEP_FIELDS = ["cinema_id", "tmdb_api_key", "ntfy_endpoint", "password"]


def new_ntfy_endpoint() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return f"{DEFAULT_NTFY_SERVER}/cinematix-{''.join(secrets.choice(alphabet) for _ in range(16))}"


@app.route("/setup", methods=["GET", "POST"])
def setup():
    config = storage.load_config()
    if storage.is_set_up(config):
        return redirect(url_for("index"))
    form = request.form
    errors: dict[str, str] = {}
    if request.method == "POST":
        cinema = cinema_settings(form.get("cinema_id", ""))
        if cinema is None:
            errors["cinema_id"] = "Choose a cinema."
        tmdb_key = form.get("tmdb_api_key", "").strip()
        if not tmdb_key:
            errors["tmdb_api_key"] = "Enter a TMDb API key."
        ntfy = sanitize_http_url(form.get("ntfy_endpoint", ""))
        if not ntfy:
            errors["ntfy_endpoint"] = "Enter an http(s) URL."
        password = form.get("password", "")
        if password and len(password) < MIN_PASSWORD_LENGTH:
            errors["password"] = f"Use at least {MIN_PASSWORD_LENGTH} characters, or leave it empty."
        elif password != form.get("password_confirm", ""):
            errors["password"] = "The passwords don't match."
        if not errors:
            storage.update_settings(config, {
                **cinema,
                "tmdb_api_key": tmdb_key,
                "ntfy_endpoint": ntfy,
                "public_url": sanitize_http_url(form.get("public_url", "")),
                "password_hash": generate_password_hash(password) if password else "",
            })
            if password:
                session.clear()
                session["auth"] = True
                session.permanent = True
            trigger_check()
            return redirect(url_for("index"))
    return render_template(
        "setup.html",
        cinemas=cinema_list(),
        form=form,
        errors=errors,
        ntfy_endpoint=form.get("ntfy_endpoint") or new_ntfy_endpoint(),
        public_url=form.get("public_url") or request.host_url.rstrip("/"),
        min_password_length=MIN_PASSWORD_LENGTH,
        # The wizard opens on the first step with an error.
        first_step=next((i for i, key in enumerate(SETUP_STEP_FIELDS) if key in errors), 0),
    )


@app.route("/setup/qr", methods=["GET"])
def setup_qr():
    """QR code (SVG) for the ntfy topic URL shown in the setup wizard."""
    data = sanitize_http_url(request.args.get("data", ""))[:300]
    if not data:
        abort(400)
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=5, border=2, dark="#000", light="#fff")
    return app.response_class(buf.getvalue(), mimetype="image/svg+xml")


@app.route("/setup/check-tmdb", methods=["POST"])
def setup_check_tmdb():
    key = ((request.get_json(silent=True) or {}).get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "Enter a key first."}), 400
    try:
        tmdb_get("/configuration", key)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            return jsonify({"ok": False, "error": "TMDb rejected this key."})
        return jsonify({"ok": False, "error": f"TMDb answered HTTP {e.response.status_code}."}), 502
    except requests.RequestException:
        return jsonify({"ok": False, "error": "Could not reach TMDb."}), 502
    return jsonify({"ok": True})


# --- Pages ---------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    config = storage.load_config()
    fill_cinema_details(config)
    state = storage.load_state()

    matches_by_movie: dict[str, list[dict]] = {}
    for m in storage.load_results()["matches"]:
        matches_by_movie.setdefault(m["movie_id"], []).append(m)

    now = cinemacity.now()
    movies = [movie_card(movie, matches_by_movie.get(movie["id"], []), state, now) for movie in config["movies"]]
    # Movies with new showtimes or freed seats come first, so the one a
    # push was about is at the top. Otherwise the order they were added in.
    movies.sort(key=lambda m: not (m["new_count"] or m["freed_seats"]))

    return render_template(
        "index.html",
        # Once after setup, or again from the "Show the tour" link in settings.
        show_tour=not config["settings"].get("tour_seen") or request.args.get("tour") == "1",
        movies=movies,
        all_formats=storage.FORMAT_OPTIONS,
        # Lets the search dropdown flag results already being monitored,
        # since re-adding one silently resets its check interval/formats/
        # history (storage.add_movie() overwrites by id) - see movie-search.
        existing_tmdb_ids=[m["tmdb_id"] for m in config["movies"] if m.get("tmdb_id")],
    )


@app.route("/api/search", methods=["GET"])
def api_search():
    return jsonify(tmdb_search(request.args.get("q", "")))


@app.route("/movies", methods=["POST"])
def add_movie():
    body = request.get_json(silent=True) or {}
    tmdb_id = body.get("tmdb_id")
    if not isinstance(tmdb_id, int):
        return jsonify({"error": "tmdb_id required"}), 400

    details = tmdb_details(tmdb_id)
    if details is None:
        return jsonify({"error": "could not fetch movie details from TMDb"}), 502

    config = storage.load_config()
    movie = {
        "id": f"tmdb-{tmdb_id}",
        "tmdb_id": tmdb_id,
        "imdb_id": details["imdb_id"],
        "title": details["title"],
        "query": details["title"].lower(),
        "poster_url": details["poster_url"],
        "overview": details["overview"],
        "runtime": details["runtime"],
        "release_date": details["release_date"],
        "rating": details["rating"],
        "added_at": cinemacity.now().isoformat(timespec="seconds"),
        "check_interval_seconds": config["settings"].get(
            "default_check_interval_seconds", storage.DEFAULT_CHECK_INTERVAL_SECONDS
        ),
        "required_formats": sanitize_required_formats(body.get("required_formats") or []),
    }
    storage.add_movie(config, movie)
    return jsonify({"ok": True, "movie": movie})


@app.route("/movies/<movie_id>/remove", methods=["POST"])
def remove_movie(movie_id):
    config = storage.load_config()
    storage.remove_movie(config, movie_id)
    return redirect(url_for("index"))


@app.route("/movies/<movie_id>", methods=["GET"])
def movie_detail(movie_id):
    config = storage.load_config()
    movie = next((m for m in config["movies"] if m["id"] == movie_id), None)
    if movie is None:
        abort(404)

    return render_template(
        "movie.html",
        movie=movie,
        matches=movie_matches(movie_id),
        all_formats=storage.FORMAT_OPTIONS,
        # Per-movie, not the global results.json timestamp: movies have
        # different check intervals.
        updated_at=storage.last_checked_at(movie, storage.load_state()),
        check_interval_options=CHECK_INTERVAL_OPTIONS,
        max_party_size=seats.MAX_PARTY_SIZE,
        default_view=config["settings"].get("default_view", "list"),
    )


def movie_matches(movie_id: str) -> list[dict]:
    """The movie's showtimes with what the page shows for each, by start."""
    now = cinemacity.now()
    views = [showtime_view(m, now) for m in storage.load_results()["matches"] if m["movie_id"] == movie_id]
    return sorted(views, key=lambda v: v.get("start_time") or "")


@app.route("/api/movies/<movie_id>/matches", methods=["GET"])
def api_movie_matches(movie_id):
    """What the movie page redraws itself from after a check, without a
    page reload."""
    config = storage.load_config()
    movie = next((m for m in config["movies"] if m["id"] == movie_id), None)
    if movie is None:
        abort(404)
    return jsonify({
        "updated_at": storage.last_checked_at(movie, storage.load_state()),
        "matches": movie_matches(movie_id),
    })


@app.route("/movies/<movie_id>/settings", methods=["POST"])
def update_settings(movie_id):
    config = storage.load_config()
    movie = next((m for m in config["movies"] if m["id"] == movie_id), None)
    if movie is None:
        abort(404)

    previous_interval = movie.get("check_interval_seconds")
    check_interval_seconds = sanitize_interval(request.form.get("check_interval", ""), previous_interval)
    required_formats = sanitize_required_formats(request.form.getlist("required_formats"))
    new_query = request.form.get("query", "").strip().lower()

    # None means "off", effectively an infinite interval, so turning
    # checking on at all counts as an increase too.
    frequency_increased = check_interval_seconds is not None and (
        previous_interval is None or check_interval_seconds < previous_interval
    )
    results_now_stale = (
        required_formats != movie.get("required_formats", [])
        or new_query != movie.get("query", "")
    )

    seats_wanted = sanitize_seats_wanted(request.form.get("seats_wanted", ""), storage.seats_wanted(movie))
    seat_alerts = request.form.get("seat_alerts", "")
    if seat_alerts not in storage.SEAT_ALERT_OPTIONS:
        seat_alerts = movie.get("seat_alerts", "all")
    if "watch_rules" in request.form:
        watch_rules = sanitize_watch_rules(request.form["watch_rules"])
    else:
        watch_rules = movie.get("watch_rules", [])

    storage.update_movie_settings(
        config, movie_id, new_query, check_interval_seconds, required_formats, seats_wanted, seat_alerts,
        watch_rules,
    )
    # A shorter interval or changed matching is worth an immediate recheck;
    # a longer interval alone isn't, and neither is turning checking off.
    if check_interval_seconds is not None and (frequency_increased or results_now_stale):
        trigger_check(movie_id)
    return redirect(url_for("movie_detail", movie_id=movie_id))


@app.route("/movies/<movie_id>/watch", methods=["POST"])
def update_watched(movie_id):
    """Saves the bells on the movie page: picked dates and showtimes, and
    showtimes turned off although a watch rule covers them. The page sends
    the complete lists."""
    config = storage.load_config()
    if not any(m["id"] == movie_id for m in config["movies"]):
        abort(404)
    picks = sanitize_watched(request.get_json(silent=True) or {})
    storage.set_watched(config, movie_id, picks)
    return jsonify(picks)


@app.route("/api/movies/<movie_id>/showtimes/<event_id>/seats", methods=["GET"])
def api_seat_map(movie_id, event_id):
    """The hall of one showtime with every seat's state, for the seat map.
    Free seats are fetched live from the booking site; if that fails, the
    scraper's last fetch is used. "fits" marks the seats where a group of
    the movie's seats_wanted fits."""
    config = storage.load_config()
    movie = next((m for m in config["movies"] if m["id"] == movie_id), None)
    match = next((m for m in movie_matches(movie_id) if m["event_id"] == event_id), None)
    if movie is None or match is None:
        abort(404)
    if match.get("venue_id") is None or not match.get("is_reserved"):
        return jsonify({"error": "There is no seat map for this showtime."}), 404

    plan_key = storage.seatplan_key(match["venue_id"], match["seatplan_id"])
    plan = (storage.load_seatplans().get(plan_key) or {}).get("seats")
    try:
        if not plan:
            plan = cinemacity.fetch_seatplan(match["venue_id"], match["seatplan_id"])
        free = cinemacity.fetch_free_seats(event_id, match.get("venue_type_id"), match["is_reserved"])
        checked_at = cinemacity.now().isoformat(timespec="seconds")
    except FetchError as e:
        app.logger.warning("Seat map for %s unavailable: %s", event_id, e)
        rec = storage.load_state().get("seats", {}).get(event_id)
        if not plan or not rec:
            return jsonify({"error": "Cinema City didn't answer. Try again in a minute."}), 502
        free, checked_at = rec["free"], rec["checked_at"]

    free_set = set(free) & set(plan)
    usable_free = seats.usable(free_set, plan)
    areas = storage.seat_areas(config["settings"], plan_key)
    rec = storage.load_state().get("seats", {}).get(event_id) or {}
    window_start = (cinemacity.now() - timedelta(seconds=storage.FREED_SEATS_WINDOW_SECONDS)).isoformat()
    freed = {k for k, at in rec.get("freed", {}).items() if k in usable_free and at > window_start}
    wanted = storage.seats_wanted(movie)
    groups = seats.find_groups(seats.in_areas(usable_free, areas), wanted) if wanted > 1 else []
    fits = set().union(*groups) if groups else set()
    # Wheelchair spaces are shown but never counted.
    free_by_area = {a: sum(1 for k in usable_free if seats.area_of(k, areas) == a) for a in seats.AREAS}

    rows: dict[tuple[str, int], dict] = {}
    for key, (row_name, seat_name) in plan.items():
        section, x, y = seats.parse_key(key)
        row = rows.setdefault((section, y), {"name": row_name, "y": y, "seats": []})
        row["seats"].append({
            "x": x, "name": seat_name,
            "state": "freed" if key in freed else "free" if key in free_set else "taken",
            "wheelchair": seats.is_wheelchair(seat_name),
            "fits": key in fits,
            "area": seats.area_of(key, areas),
        })
    for row in rows.values():
        row["seats"].sort(key=lambda seat: seat["x"])
    xs = [seats.parse_key(k)[1] for k in plan]
    return jsonify({
        "hall": plan_key,
        "areas": areas,
        "rows": [rows[k] for k in sorted(rows)],
        "min_x": min(xs), "max_x": max(xs),
        "capacity": len(seats.usable(plan, plan)),
        "free": len(usable_free),
        "free_by_area": free_by_area if any(areas.values()) else None,
        "freed": len(freed),
        "seats_wanted": wanted,
        "fits": bool(groups),
        "checked_at": checked_at,
    })


@app.route("/settings/seat-areas", methods=["POST"])
def update_seat_areas():
    """Saves a hall's seating areas from the seat map. Both empty means no
    areas, so every seat is acceptable."""
    body = request.get_json(silent=True) or {}
    hall = str(body.get("hall", ""))
    if not re.fullmatch(r"\d{1,9}/\d{1,9}", hall):
        return jsonify({"error": "Unknown hall."}), 400
    areas = {a: area for a in seats.AREAS if (area := sanitize_area(body.get(a)))}
    storage.set_seat_areas(storage.load_config(), hall, areas)
    return jsonify({"hall": hall, "areas": areas})


@app.route("/settings", methods=["POST"])
def update_global_settings():
    config = storage.load_config()
    settings = config["settings"]
    form = request.form
    updates = {
        "tmdb_api_key": form.get("tmdb_api_key", "").strip(),
        "ntfy_endpoint": sanitize_http_url(form.get("ntfy_endpoint", "")),
        "public_url": sanitize_http_url(form.get("public_url", "")),
        "notify_on_start": "notify_on_start" in form,
        "notify_on_start_priority": sanitize_ntfy_priority(form.get("notify_on_start_priority", ""), "low"),
        "notify_on_new_showtime": "notify_on_new_showtime" in form,
        "notify_on_new_showtime_priority": sanitize_ntfy_priority(
            form.get("notify_on_new_showtime_priority", ""), "urgent"
        ),
        "notify_on_more_seats": "notify_on_more_seats" in form,
        "notify_on_more_seats_priority": sanitize_ntfy_priority(
            form.get("notify_on_more_seats_priority", ""), "urgent"
        ),
        "default_check_interval_seconds": sanitize_interval(
            form.get("default_check_interval", ""), settings.get("default_check_interval_seconds")
        ),
        "default_view": sanitize_default_view(form.get("default_view", ""), "list"),
    }
    cinema_changed = False
    cinema_id = form.get("cinema_id")
    if cinema_id and cinema_id != settings.get("cinema_id"):
        cinema = cinema_settings(cinema_id)
        if cinema:
            updates.update(cinema)
            cinema_changed = True

    password = form.get("new_password", "")
    if form.get("remove_password"):
        updates["password_hash"] = ""
    elif len(password) >= MIN_PASSWORD_LENGTH:
        updates["password_hash"] = generate_password_hash(password)

    storage.update_settings(config, updates)
    if updates.get("password_hash"):
        # Keeps the browser that set the password logged in.
        session["auth"] = True
        session.permanent = True
    if cinema_changed:
        trigger_check()
    return redirect(_safe_next(form.get("next")))


@app.route("/settings/tour-seen", methods=["POST"])
def tour_seen():
    """The tour was finished or closed: don't open it on its own again."""
    storage.update_settings(storage.load_config(), {"tour_seen": True})
    return jsonify({"ok": True})


@app.route("/settings/test-notification", methods=["POST"])
def test_notification():
    """Sends a test push straight to whatever endpoint is currently in the
    field, unsaved edits included."""
    body = request.get_json(silent=True) or {}
    endpoint = sanitize_http_url(body.get("ntfy_endpoint") or "")
    if not endpoint:
        return jsonify({"ok": False, "error": "Enter an http(s) ntfy topic URL first."}), 400
    try:
        resp = requests.post(
            endpoint,
            data="This is a test notification from cinematix.".encode("utf-8"),
            headers={"Title": "Test notification", "Priority": "default", "Tags": "movie_camera,tickets"},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/check-now", methods=["POST"])
def check_now():
    body = request.get_json(silent=True) or {}
    try:
        requests.post(f"{SCRAPER_CONTROL_URL}/check-now", json={"movie_id": body.get("movie_id")}, timeout=5)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def api_status():
    movie_id = request.args.get("movie_id")
    checking = False
    try:
        params = {"movie_id": movie_id} if movie_id else {}
        resp = requests.get(f"{SCRAPER_CONTROL_URL}/status", params=params, timeout=3)
        checking = resp.json().get("checking", False)
    except (requests.RequestException, ValueError):
        pass

    if movie_id:
        config = storage.load_config()
        movie = next((m for m in config["movies"] if m["id"] == movie_id), None)
        updated_at = storage.last_checked_at(movie, storage.load_state()) if movie else None
    else:
        updated_at = storage.load_results()["updated_at"]
    return jsonify({"updated_at": updated_at, "checking": checking})


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threads=8)
