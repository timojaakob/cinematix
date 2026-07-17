#!/usr/bin/env python3
"""
Shared JSON storage for cinemacity-scraper: the watched-movie config and the
latest scrape results. Used by both scraper.py (writer of results, reader of
config) and webui.py (writer of config, reader of results).
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger("cinemacity-scraper")

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/data/config.json"))
RESULTS_FILE = Path(os.environ.get("RESULTS_FILE", "/data/results.json"))

DEFAULT_MOVIE_QUERIES = [
    q.strip().lower() for q in os.environ.get("MOVIE_QUERY", "dune").split(",") if q.strip()
]
DEFAULT_REQUIRE_IMAX = os.environ.get("REQUIRE_IMAX", "true").lower() == "true"


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload)
    tmp.replace(path)


def _default_config() -> dict:
    return {
        "require_imax": DEFAULT_REQUIRE_IMAX,
        "movies": [
            {
                "id": f"legacy-{q}",
                "tmdb_id": None,
                "title": q,
                "query": q,
                "poster_url": None,
                "overview": None,
                "runtime": None,
                "release_date": None,
                "added_at": None,
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

    if "movies" not in raw and "movie_queries" in raw:
        # Migrate the old flat-query-list schema to movie objects.
        queries = [q.strip().lower() for q in raw.get("movie_queries", []) if q.strip()]
        cfg = {
            "require_imax": bool(raw.get("require_imax", DEFAULT_REQUIRE_IMAX)),
            "movies": [
                {
                    "id": f"legacy-{q}",
                    "tmdb_id": None,
                    "title": q,
                    "query": q,
                    "poster_url": None,
                    "overview": None,
                    "runtime": None,
                    "release_date": None,
                    "added_at": None,
                }
                for q in queries
            ],
        }
        save_config(cfg)
        return cfg

    return {
        "require_imax": bool(raw.get("require_imax", DEFAULT_REQUIRE_IMAX)),
        "movies": raw.get("movies", []),
    }


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


def update_movie_query(cfg: dict, movie_id: str, new_query: str) -> dict:
    for m in cfg["movies"]:
        if m["id"] == movie_id:
            m["query"] = new_query.strip().lower()
    save_config(cfg)
    return cfg


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
