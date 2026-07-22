#!/usr/bin/env python3
"""
Local-only web UI for cinematix.

Search TMDb to pick movies to monitor (added to /data/config.json, picked up
by scraper.py on its next poll cycle), and view each movie's matching
showtimes (from /data/results.json) with clickable booking links. No auth —
only bind this to a LAN-only port.
"""

import os
from datetime import datetime

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import storage

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"
SCRAPER_CONTROL_URL = os.environ.get("SCRAPER_CONTROL_URL", "http://localhost:5001")

# (seconds, label) options for the per-movie check-frequency dropdown.
CHECK_INTERVAL_OPTIONS = [
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

app = Flask(__name__)


def sanitize_required_formats(values: list[str]) -> list[str]:
    """Keeps only known formats and their storage.FORMAT_OPTIONS order,
    regardless of what the client sent - defends against a stale page
    submitting a format storage.FORMAT_OPTIONS no longer contains."""
    return [f for f in storage.FORMAT_OPTIONS if f in values]


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


@app.template_filter("relativedate")
def relativedate(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    seconds = max((datetime.now() - dt).total_seconds(), 0)
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


def trigger_check(movie_id: str | None = None) -> None:
    """Wakes the scraper's poll loop early and forces it to (re)check
    movie_id this cycle regardless of its own check_interval_seconds - best
    effort, since the scraper container may be temporarily unreachable."""
    try:
        requests.post(f"{SCRAPER_CONTROL_URL}/check-now", json={"movie_id": movie_id}, timeout=5)
    except requests.RequestException:
        pass


def get_tmdb_api_key() -> str:
    """Live value comes from the settings dialog (config.json); the
    TMDB_API_KEY env var is only the one-time seed for a fresh config."""
    return storage.load_config()["settings"].get("tmdb_api_key") or TMDB_API_KEY


def sanitize_ntfy_priority(value: str, fallback: str) -> str:
    return value if value in storage.NTFY_PRIORITY_OPTIONS else fallback


def sanitize_default_view(value: str, fallback: str) -> str:
    return value if value in storage.VIEW_OPTIONS else fallback


def card_status_tier(showtime_count: int, best_availability: float | None) -> str:
    """Buckets a movie's current showtimes into a coarse status for the
    monitored-movies grid, echoing the calendar view's red/amber/green
    read without needing to duplicate its HSL gradient math server-side."""
    if showtime_count == 0:
        return "none"
    if best_availability is None:
        return "sold-out"
    if best_availability >= 0.5:
        return "good"
    if best_availability >= 0.15:
        return "fair"
    return "low"


def tmdb_search(query: str) -> list[dict]:
    if not query.strip():
        return []
    try:
        resp = requests.get(
            f"{TMDB_BASE}/search/movie",
            params={"api_key": get_tmdb_api_key(), "query": query, "include_adult": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return []

    results = []
    for r in data.get("results", []):
        results.append(
            {
                "tmdb_id": r["id"],
                "title": r.get("title") or r.get("original_title") or "",
                "year": (r.get("release_date") or "")[:4],
                "poster_thumb": f"{TMDB_IMG_BASE}/w92{r['poster_path']}" if r.get("poster_path") else None,
                "overview": r.get("overview") or "",
            }
        )
    return results


def tmdb_details(tmdb_id: int) -> dict | None:
    try:
        resp = requests.get(
            f"{TMDB_BASE}/movie/{tmdb_id}",
            params={"api_key": get_tmdb_api_key()},
            timeout=10,
        )
        resp.raise_for_status()
        d = resp.json()
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


@app.route("/", methods=["GET"])
def index():
    config = storage.load_config()
    # Shown to the user as-is, including the TMDB_API_KEY env var fallback
    # (see get_tmdb_api_key()) so the field reflects the key actually in use,
    # not just what happens to be saved in config.json.
    settings = {**config["settings"], "tmdb_api_key": config["settings"].get("tmdb_api_key") or TMDB_API_KEY}

    matches_by_movie: dict[str, list[dict]] = {}
    for m in storage.load_results()["matches"]:
        matches_by_movie.setdefault(m["movie_id"], []).append(m)

    movies = []
    for movie in config["movies"]:
        movie_matches = matches_by_movie.get(movie["id"], [])
        open_matches = [m for m in movie_matches if not m.get("sold_out") and m.get("availability") is not None]
        best_availability = max((m["availability"] for m in open_matches), default=None)
        showtime_count = len(movie_matches)
        movies.append(
            {
                **movie,
                "showtime_count": showtime_count,
                "status_tier": card_status_tier(showtime_count, best_availability),
            }
        )

    return render_template(
        "index.html",
        movies=movies,
        all_formats=storage.FORMAT_OPTIONS,
        settings=settings,
        check_interval_options=CHECK_INTERVAL_OPTIONS,
        ntfy_priority_options=storage.NTFY_PRIORITY_OPTIONS,
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
    if not tmdb_id:
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
        "added_at": datetime.now().isoformat(timespec="seconds"),
        "check_interval_seconds": config["settings"].get(
            "default_check_interval_seconds", storage.DEFAULT_CHECK_INTERVAL_SECONDS
        ),
        "last_checked_at": None,
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

    results = storage.load_results()
    matches = [m for m in results["matches"] if m["movie_id"] == movie_id]
    audio_languages = sorted({m.get("audio_language") or "Unknown" for m in matches})
    format_options = sorted({m.get("format") or "Unknown" for m in matches})
    # Shown to the user as-is, including the TMDB_API_KEY env var fallback
    # (see get_tmdb_api_key()) so the field reflects the key actually in use,
    # not just what happens to be saved in config.json.
    settings = {**config["settings"], "tmdb_api_key": config["settings"].get("tmdb_api_key") or TMDB_API_KEY}
    return render_template(
        "movie.html",
        movie=movie,
        matches=matches,
        audio_languages=audio_languages,
        format_options=format_options,
        all_formats=storage.FORMAT_OPTIONS,
        # Per-movie, not the global results.json timestamp: movies can now
        # have different check_interval_seconds, so the last poll cycle
        # doesn't necessarily mean *this* movie was just refreshed.
        updated_at=movie.get("last_checked_at"),
        check_interval_options=CHECK_INTERVAL_OPTIONS,
        settings=settings,
        ntfy_priority_options=storage.NTFY_PRIORITY_OPTIONS,
    )


@app.route("/movies/<movie_id>/settings", methods=["POST"])
def update_settings(movie_id):
    config = storage.load_config()
    movie = next((m for m in config["movies"] if m["id"] == movie_id), None)
    if movie is None:
        abort(404)

    interval_raw = request.form.get("check_interval", "")
    if interval_raw == "off":
        check_interval_seconds = None
    else:
        try:
            check_interval_seconds = int(interval_raw)
        except ValueError:
            check_interval_seconds = storage.DEFAULT_CHECK_INTERVAL_SECONDS

    required_formats = sanitize_required_formats(request.form.getlist("required_formats"))
    new_query = request.form.get("query", "").strip().lower()

    previous_interval = movie.get("check_interval_seconds")
    # None means "off", effectively an infinite interval, so turning
    # checking on at all counts as an increase too.
    frequency_increased = check_interval_seconds is not None and (
        previous_interval is None or check_interval_seconds < previous_interval
    )
    results_now_stale = (
        required_formats != movie.get("required_formats", [])
        or new_query != movie.get("query", "")
    )

    storage.update_movie_settings(
        config, movie_id, request.form.get("query", ""), check_interval_seconds, required_formats
    )
    # A shorter interval means the user wants fresher data sooner, and a
    # query/required_formats change makes the existing results stale either
    # way - both are worth an immediate recheck instead of waiting out the
    # new check_interval_seconds. A longer interval on its own shouldn't
    # trigger one: the user asked to be checked less often, not right now.
    # Skip entirely if checking is turned off, since the user just asked us
    # to stop checking this movie.
    if check_interval_seconds is not None and (frequency_increased or results_now_stale):
        trigger_check(movie_id)
    return redirect(url_for("movie_detail", movie_id=movie_id))


@app.route("/settings", methods=["POST"])
def update_global_settings():
    config = storage.load_config()

    interval_raw = request.form.get("default_check_interval", "")
    if interval_raw == "off":
        default_check_interval_seconds = None
    else:
        try:
            default_check_interval_seconds = int(interval_raw)
        except ValueError:
            default_check_interval_seconds = storage.DEFAULT_CHECK_INTERVAL_SECONDS

    storage.update_settings(
        config,
        {
            "tmdb_api_key": request.form.get("tmdb_api_key", "").strip(),
            "ntfy_endpoint": request.form.get("ntfy_endpoint", "").strip(),
            "notify_on_start": "notify_on_start" in request.form,
            "notify_on_start_priority": sanitize_ntfy_priority(
                request.form.get("notify_on_start_priority", ""), "low"
            ),
            "notify_on_new_showtime": "notify_on_new_showtime" in request.form,
            "notify_on_new_showtime_priority": sanitize_ntfy_priority(
                request.form.get("notify_on_new_showtime_priority", ""), "urgent"
            ),
            "default_check_interval_seconds": default_check_interval_seconds,
            "default_view": sanitize_default_view(request.form.get("default_view", ""), "list"),
        },
    )
    # Return to wherever the form was submitted from (index or a movie page)
    # rather than always bouncing to index - only trust a same-site path.
    next_path = request.form.get("next", "")
    if next_path.startswith("/") and not next_path.startswith("//"):
        return redirect(next_path)
    return redirect(url_for("index"))


@app.route("/settings/test-notification", methods=["POST"])
def test_notification():
    """Sends a test push straight to whatever endpoint is currently in the
    field, unsaved edits included - this is a live probe, not tied to
    the saved config.json value."""
    body = request.get_json(silent=True) or {}
    endpoint = (body.get("ntfy_endpoint") or "").strip()
    if not endpoint:
        return jsonify({"ok": False, "error": "Enter an ntfy endpoint first."}), 400
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
        # Scoped to movie_id so a movie with checking turned off doesn't
        # show "Checking..." just because some *other* movie's poll cycle
        # happens to be running - see scraper.py's CHECKING_IDS.
        params = {"movie_id": movie_id} if movie_id else {}
        resp = requests.get(f"{SCRAPER_CONTROL_URL}/status", params=params, timeout=3)
        checking = resp.json().get("checking", False)
    except requests.RequestException:
        pass

    if movie_id:
        config = storage.load_config()
        movie = next((m for m in config["movies"] if m["id"] == movie_id), None)
        updated_at = movie.get("last_checked_at") if movie else None
    else:
        updated_at = storage.load_results()["updated_at"]
    return jsonify({"updated_at": updated_at, "checking": checking})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
