#!/usr/bin/env python3
"""
Local-only web UI for cinemacity-scraper.

Search TMDb to pick movies to watch (added to /data/config.json, picked up
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
SCRAPER_CONTROL_URL = os.environ.get("SCRAPER_CONTROL_URL", "http://cinemacity-scraper:5001")

app = Flask(__name__)


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


def tmdb_search(query: str) -> list[dict]:
    if not query.strip():
        return []
    try:
        resp = requests.get(
            f"{TMDB_BASE}/search/movie",
            params={"api_key": TMDB_API_KEY, "query": query, "include_adult": "false"},
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
            params={"api_key": TMDB_API_KEY},
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
    }


@app.route("/", methods=["GET"])
def index():
    config = storage.load_config()
    return render_template(
        "index.html",
        movies=config["movies"],
        require_imax=config["require_imax"],
    )


@app.route("/config", methods=["POST"])
def update_config():
    config = storage.load_config()
    config["require_imax"] = request.form.get("require_imax") == "on"
    storage.save_config(config)
    return redirect(url_for("index"))


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

    movie = {
        "id": f"tmdb-{tmdb_id}",
        "tmdb_id": tmdb_id,
        "title": details["title"],
        "query": details["title"].lower(),
        "poster_url": details["poster_url"],
        "overview": details["overview"],
        "runtime": details["runtime"],
        "release_date": details["release_date"],
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }
    config = storage.load_config()
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
    return render_template(
        "movie.html",
        movie=movie,
        matches=matches,
        updated_at=results["updated_at"],
    )


@app.route("/movies/<movie_id>/query", methods=["POST"])
def update_query(movie_id):
    config = storage.load_config()
    if not any(m["id"] == movie_id for m in config["movies"]):
        abort(404)
    storage.update_movie_query(config, movie_id, request.form.get("query", ""))
    return redirect(url_for("movie_detail", movie_id=movie_id))


@app.route("/check-now", methods=["POST"])
def check_now():
    try:
        requests.post(f"{SCRAPER_CONTROL_URL}/check-now", timeout=5)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def api_status():
    checking = False
    try:
        resp = requests.get(f"{SCRAPER_CONTROL_URL}/status", timeout=3)
        checking = resp.json().get("checking", False)
    except requests.RequestException:
        pass
    return jsonify({"updated_at": storage.load_results()["updated_at"], "checking": checking})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
