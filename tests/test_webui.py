import pytest

import storage
import webui

CINEMAS = [
    {"id": "1052", "name": "Prague Flora, OC FLORA", "link": "https://www.cinemacity.cz/cinemas/flora"},
    {"id": "1056", "name": "Prague Chodov, Westfield Chodov", "link": "https://www.cinemacity.cz/cinemas/chodov"},
]
SAME_ORIGIN = {"Origin": "http://localhost"}


@pytest.fixture
def client(data_dir, monkeypatch):
    monkeypatch.setattr(webui, "cinema_list", lambda: CINEMAS)
    checks = []
    monkeypatch.setattr(webui, "trigger_check", lambda movie_id=None: checks.append(movie_id))
    c = webui.app.test_client()
    c.checks = checks
    return c


def finish_setup(client, password=""):
    return client.post("/setup", headers=SAME_ORIGIN, data={
        "cinema_id": "1052", "tmdb_api_key": "key", "ntfy_endpoint": "https://ntfy.sh/topic",
        "public_url": "http://localhost", "password": password, "password_confirm": password,
    })


def test_fresh_install_redirects_to_setup(client):
    for path in ("/", "/movies/x", "/api/status"):
        resp = client.get(path)
        assert resp.status_code == 302 and resp.headers["Location"] == "/setup"
    assert client.get("/healthz").status_code == 200
    assert client.get("/sw.js").status_code == 200


def test_setup_validation(client):
    resp = client.post("/setup", headers=SAME_ORIGIN, data={
        "cinema_id": "9999", "tmdb_api_key": "", "ntfy_endpoint": "file:///etc/passwd",
        "password": "short", "password_confirm": "short",
    })
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    for message in ("Choose a cinema.", "Enter a TMDb API key.", "Enter an http(s) URL.", "at least 8"):
        assert message in html
    assert "let current = 0" in html
    assert not storage.is_set_up(storage.load_config())


def test_setup_saves_and_logs_in(client):
    resp = finish_setup(client, password="secret123")
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    s = storage.load_config()["settings"]
    assert (s["cinema_id"], s["cinema_name"]) == ("1052", "Prague Flora, OC FLORA")
    assert s["ntfy_endpoint"] == "https://ntfy.sh/topic"
    assert s["password_hash"] and s["password_hash"] != "secret123"
    assert client.checks == [None]
    assert client.get("/").status_code == 200
    assert client.get("/setup").status_code == 302  # done once


def test_setup_suggests_private_ntfy_topic(client):
    html = client.get("/setup").get_data(as_text=True)
    assert "https://ntfy.sh/cinematix-" in html
    assert webui.new_ntfy_endpoint() != webui.new_ntfy_endpoint()


def test_csrf_blocks_cross_origin_posts(client):
    finish_setup(client)
    for headers in ({"Origin": "https://evil.example"}, {"Referer": "https://evil.example/page"}):
        assert client.post("/settings", headers=headers, data={}).status_code == 403
        assert client.post("/movies/x/remove", headers=headers).status_code == 403
    # Same origin, and non-browser clients without Origin/Referer, are fine.
    assert client.post("/settings", headers=SAME_ORIGIN, data={"next": "/"}).status_code == 302
    assert client.post("/settings", data={"next": "/"}).status_code == 302


def test_csrf_ignores_scheme_behind_tls_proxy(client):
    finish_setup(client)
    assert client.post("/settings", headers={"Origin": "https://localhost"}, data={}).status_code == 302


def test_login_required_when_password_set(client):
    finish_setup(client, password="secret123")
    anon = webui.app.test_client()
    resp = anon.get("/movies/x")
    assert resp.status_code == 302 and resp.headers["Location"] == "/login?next=/movies/x"
    assert anon.get("/api/status").status_code == 401
    assert anon.get("/healthz").status_code == 200
    assert anon.post("/login", data={"password": "wrong"}).status_code == 401
    resp = anon.post("/login", data={"password": "secret123", "next": "//evil.example"})
    assert resp.headers["Location"] == "/"
    assert anon.get("/").status_code == 200
    anon.post("/logout")
    assert anon.get("/").status_code == 302


def test_no_login_without_password(client):
    finish_setup(client)
    assert webui.app.test_client().get("/").status_code == 200


def test_settings_sanitizing(client):
    finish_setup(client)
    client.post("/settings", headers=SAME_ORIGIN, data={
        "default_check_interval": "1", "ntfy_endpoint": "javascript:alert(1)", "public_url": "ftp://x",
        "default_view": "bogus", "notify_on_start_priority": "loud",
    })
    s = storage.load_config()["settings"]
    assert s["default_check_interval_seconds"] == storage.DEFAULT_CHECK_INTERVAL_SECONDS
    assert s["ntfy_endpoint"] == "" and s["public_url"] == ""
    assert s["default_view"] == "list" and s["notify_on_start_priority"] == "low"


@pytest.mark.parametrize("raw, expected", [("60", 60), ("86400", 86400), ("off", None), ("1", 300), ("x", 300)])
def test_sanitize_interval(raw, expected):
    assert webui.sanitize_interval(raw, 300) == expected


def test_changing_cinema_saves_details_and_rechecks(client):
    finish_setup(client)
    client.checks.clear()
    client.post("/settings", headers=SAME_ORIGIN, data={"cinema_id": "1056"})
    s = storage.load_config()["settings"]
    assert (s["cinema_id"], s["cinema_link"]) == ("1056", "https://www.cinemacity.cz/cinemas/chodov")
    assert client.checks == [None]
    client.post("/settings", headers=SAME_ORIGIN, data={"cinema_id": "nope"})
    assert storage.load_config()["settings"]["cinema_id"] == "1056"


def test_password_can_be_set_and_removed(client):
    finish_setup(client)
    client.post("/settings", headers=SAME_ORIGIN, data={"new_password": "short"})
    assert storage.load_config()["settings"]["password_hash"] == ""
    client.post("/settings", headers=SAME_ORIGIN, data={"new_password": "longenough"})
    assert storage.load_config()["settings"]["password_hash"]
    assert client.get("/").status_code == 200  # the browser that set it stays logged in
    client.post("/settings", headers=SAME_ORIGIN, data={"remove_password": "on"})
    assert storage.load_config()["settings"]["password_hash"] == ""


def test_password_hash_never_reaches_pages(client):
    finish_setup(client, password="secret123")
    hash_ = storage.load_config()["settings"]["password_hash"]
    assert hash_ not in client.get("/").get_data(as_text=True)


def test_test_notification_only_posts_to_http(client):
    finish_setup(client)
    resp = client.post("/settings/test-notification", headers=SAME_ORIGIN, json={"ntfy_endpoint": "file:///etc/hosts"})
    assert resp.status_code == 400


def test_setup_qr_rejects_non_http(client):
    assert client.get("/setup/qr?data=javascript:x").status_code == 400
    resp = client.get("/setup/qr?data=https://ntfy.sh/topic")
    assert resp.mimetype == "image/svg+xml" and b"<svg" in resp.data


def test_movie_settings_interval_is_sanitized(client):
    finish_setup(client)
    cfg = storage.load_config()
    cfg["movies"] = [{"id": "m", "title": "Dune", "query": "dune", "check_interval_seconds": 600, "required_formats": ["IMAX"]}]
    storage.save_config(cfg)
    client.post("/movies/m/settings", headers=SAME_ORIGIN, data={"check_interval": "5", "query": "dune", "required_formats": ["IMAX", "Bogus"]})
    movie = storage.load_config()["movies"][0]
    assert movie["check_interval_seconds"] == 600
    assert movie["required_formats"] == ["IMAX"]


def test_movie_matches_api(client):
    finish_setup(client, password="secret123")
    cfg = storage.load_config()
    cfg["movies"] = [{"id": "m", "title": "Dune", "query": "dune", "check_interval_seconds": 120, "required_formats": ["IMAX"]}]
    storage.save_config(cfg)
    storage.save_results([{"event_id": "1", "movie_id": "m"}, {"event_id": "2", "movie_id": "other"}])
    storage.save_state({"notified_event_ids": [], "last_checked": {"m": "2026-10-21T13:00:00"}})
    data = client.get("/api/movies/m/matches").get_json()
    assert data == {"updated_at": "2026-10-21T13:00:00", "matches": [{"event_id": "1", "movie_id": "m"}]}
    assert client.get("/api/movies/nope/matches").status_code == 404
    assert webui.app.test_client().get("/api/movies/m/matches").status_code == 401
