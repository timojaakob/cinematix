#!/usr/bin/env python3
"""
Shared JSON storage for cinemacity-scraper: the watched-movie config and the
latest scrape results. Used by both scraper.py (writer of results, reader of
config) and webui.py (writer of config, reader of results).
"""

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

log = logging.getLogger("cinemacity-scraper")

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/data/config.json"))
RESULTS_FILE = Path(os.environ.get("RESULTS_FILE", "/data/results.json"))

DEFAULT_MOVIE_QUERIES = [
    q.strip().lower() for q in os.environ.get("MOVIE_QUERY", "dune").split(",") if q.strip()
]
# Formats scraper.py can distinguish at this cinema (see detect_format() and
# its docstring for how each is identified from the live API's attribute
# tags). A movie's required_formats is a subset of this list; empty means no
# format is wanted, so the movie matches nothing until the user picks at
# least one - see the format filtering in scraper.py's check_date().
FORMAT_OPTIONS = ["IMAX", "3D", "2D"]
# Historical env var: used only as the one-time default for required_formats
# on movies that predate that field (see load_config()'s migration) and for
# MOVIE_QUERY-seeded movies on a fresh config. There is no live global toggle
# anymore - each movie now picks its own required formats.
DEFAULT_REQUIRE_IMAX = os.environ.get("REQUIRE_IMAX", "true").lower() == "true"
DEFAULT_REQUIRED_FORMATS = ["IMAX"] if DEFAULT_REQUIRE_IMAX else []
# Matches scraper.py's own default POLL_INTERVAL_SECONDS, so a freshly added
# movie is checked on every poll cycle unless the user slows it down.
DEFAULT_CHECK_INTERVAL_SECONDS = 120


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call: webui.py and scraper.py are separate processes that
    # can both write this file around the same time (e.g. both running a
    # config migration on startup). A shared tmp filename lets their writes
    # interleave before either renames, corrupting the file - .replace()
    # itself is atomic, but only if each writer has its own tmp file.
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(payload)
    tmp.replace(path)


def _default_config() -> dict:
    return {
        "movies": [
            {
                "id": f"legacy-{q}",
                "tmdb_id": None,
                "imdb_id": None,
                "title": q,
                "query": q,
                "poster_url": None,
                "overview": None,
                "runtime": None,
                "release_date": None,
                "added_at": None,
                "check_interval_seconds": DEFAULT_CHECK_INTERVAL_SECONDS,
                "last_checked_at": None,
                "required_formats": list(DEFAULT_REQUIRED_FORMATS),
            }
            for q in DEFAULT_MOVIE_QUERIES
        ],
    }


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        cfg = _default_config()
        save_config(cfg)
        return cfg

    try:
        raw = json.loads(CONFIG_FILE.read_text())
    except json.JSONDecodeError:
        log.warning("Config file corrupt, falling back to defaults")
        cfg = _default_config()
        save_config(cfg)
        return cfg

    # A movie's required_formats might be missing (config predating that
    # field) or its old global equivalent might still be sitting in the
    # top-level "require_imax" key - both get carried forward here so the
    # migration only has to run once per movie.
    legacy_required_formats = ["IMAX"] if raw.get("require_imax", DEFAULT_REQUIRE_IMAX) else []

    if "movies" not in raw and "movie_queries" in raw:
        # Migrate the old flat-query-list schema to movie objects.
        queries = [q.strip().lower() for q in raw.get("movie_queries", []) if q.strip()]
        cfg = {
            "movies": [
                {
                    "id": f"legacy-{q}",
                    "tmdb_id": None,
                    "imdb_id": None,
                    "title": q,
                    "query": q,
                    "poster_url": None,
                    "overview": None,
                    "runtime": None,
                    "release_date": None,
                    "added_at": None,
                    "check_interval_seconds": DEFAULT_CHECK_INTERVAL_SECONDS,
                    "last_checked_at": None,
                    "required_formats": list(legacy_required_formats),
                }
                for q in queries
            ],
        }
        save_config(cfg)
        return cfg

    movies = raw.get("movies", [])
    changed = "require_imax" in raw
    for m in movies:
        if "required_formats" not in m:
            m["required_formats"] = list(legacy_required_formats)
            changed = True
    cfg = {"movies": movies}
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2))


def add_movie(cfg: dict, movie: dict) -> dict:
    movies = [m for m in cfg["movies"] if m["id"] != movie["id"]]
    movies.append(movie)
    cfg["movies"] = movies
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
) -> dict:
    for m in cfg["movies"]:
        if m["id"] == movie_id:
            m["query"] = new_query.strip().lower()
            m["check_interval_seconds"] = check_interval_seconds
            m["required_formats"] = required_formats
    save_config(cfg)
    return cfg


def mark_movies_checked(movie_ids: set[str], when: str) -> None:
    """Called by scraper.py after a poll cycle to record which movies were
    actually checked, so per-movie check_interval_seconds can be honored on
    the next cycle. Reloads config right before writing to keep the
    read-modify-write window with webui.py's own config writes short."""
    if not movie_ids:
        return
    cfg = load_config()
    for m in cfg["movies"]:
        if m["id"] in movie_ids:
            m["last_checked_at"] = when
    save_config(cfg)


def load_results() -> dict:
    if RESULTS_FILE.exists():
        try:
            return json.loads(RESULTS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"updated_at": None, "matches": []}


def save_results(results: list[dict]) -> None:
    payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "matches": results}
    _atomic_write(RESULTS_FILE, json.dumps(payload, indent=2))
