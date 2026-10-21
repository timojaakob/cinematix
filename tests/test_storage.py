import json

import storage


def test_fresh_install_is_not_set_up(data_dir):
    cfg = storage.load_config()
    assert cfg["movies"] == []
    assert cfg["settings"] == storage.DEFAULT_SETTINGS
    assert not storage.is_set_up(cfg)


def test_fresh_install_ignores_legacy_env(data_dir, monkeypatch):
    monkeypatch.setenv("CINEMA_ID", "1056")
    assert not storage.is_set_up(storage.load_config())


def test_pre_1_0_config_imports_env_once(data_dir, monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "env-key")
    monkeypatch.setenv("NTFY_TOPIC", "old-topic")
    monkeypatch.setenv("WEBUI_URL", "https://cine.example")
    storage.CONFIG_FILE.write_text(json.dumps({
        "movies": [{"id": "tmdb-1", "title": "Dune", "query": "dune", "last_checked_at": "2026-10-01T10:00:00"}],
        "settings": {"ntfy_endpoint": "", "default_view": "calendar"},
    }))
    cfg = storage.load_config()
    s = cfg["settings"]
    assert storage.is_set_up(cfg)
    assert s["cinema_id"] == "1052"  # the only cinema before 1.0
    assert s["tmdb_api_key"] == "env-key"
    assert s["ntfy_endpoint"] == "https://ntfy.sh/old-topic"
    assert s["public_url"] == "https://cine.example"
    assert s["default_view"] == "calendar"
    assert cfg["movies"][0]["required_formats"] == ["IMAX"]


def test_stored_values_win_over_legacy_env(data_dir, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://env.example/topic")
    storage.CONFIG_FILE.write_text(json.dumps({"movies": [], "settings": {"ntfy_endpoint": "https://kept.example/t"}}))
    assert storage.load_config()["settings"]["ntfy_endpoint"] == "https://kept.example/t"


def test_config_from_1_0_never_reads_env(data_dir, monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "env-key")
    storage.CONFIG_FILE.write_text(json.dumps({"movies": [], "settings": {"cinema_id": "1056", "tmdb_api_key": ""}}))
    assert storage.load_config()["settings"]["tmdb_api_key"] == ""


def test_secret_key_is_created_once(data_dir):
    first = storage.secret_key()
    assert len(first) == 64
    assert storage.secret_key() == first


def test_last_checked_prefers_state(data_dir):
    movie = {"id": "m", "last_checked_at": "2026-01-01T00:00:00"}
    assert storage.last_checked_at(movie, {}) == "2026-01-01T00:00:00"
    assert storage.last_checked_at(movie, {"last_checked": {"m": "2026-02-02T00:00:00"}}) == "2026-02-02T00:00:00"
    assert storage.last_checked_at({"id": "x"}, {}) is None


def test_corrupt_files_start_fresh(data_dir):
    storage.STATE_FILE.write_text("{not json")
    storage.RESULTS_FILE.write_text("[broken")
    assert storage.load_state() == {"notified_event_ids": []}
    assert storage.load_results()["matches"] == []
