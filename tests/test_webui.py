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


def add_dune(seat_alerts="all"):
    cfg = storage.load_config()
    cfg["movies"] = [{"id": "m", "title": "Dune", "query": "dune", "check_interval_seconds": 120,
                      "required_formats": ["IMAX"], "seat_alerts": seat_alerts}]
    storage.save_config(cfg)


def test_movie_seat_settings(client):
    finish_setup(client)
    add_dune()
    form = {"check_interval": "120", "query": "dune", "required_formats": ["IMAX"]}
    rules = '[{"days": [1, 2, 9], "from": "17:00", "to": "25:00", "date_from": "soon"}, {"days": []}, "x"]'
    client.post("/movies/m/settings", headers=SAME_ORIGIN,
                data=dict(form, seats_wanted="4", seat_alerts="custom", watch_rules=rules))
    movie = storage.load_config()["movies"][0]
    assert (movie["seats_wanted"], movie["seat_alerts"]) == (4, "custom")
    assert movie["watch_rules"] == [{"days": [1, 2], "from": "17:00", "to": "23:59", "date_from": "", "date_to": ""}]
    client.post("/movies/m/settings", headers=SAME_ORIGIN, data=dict(form, seats_wanted="99", seat_alerts="bogus"))
    movie = storage.load_config()["movies"][0]
    assert (movie["seats_wanted"], movie["seat_alerts"]) == (4, "custom")
    assert len(movie["watch_rules"]) == 1  # not sent: kept


def test_watch_list_is_sanitized(client):
    finish_setup(client)
    add_dune("custom")
    resp = client.post("/movies/m/watch", headers=SAME_ORIGIN, json={
        "watched_dates": ["2026-12-19", "2026-12-18", "2026-12-19", "soon"],
        "watched_event_ids": ["267914", "../x", 5, "267914"],
        "unwatched_event_ids": ["267915"],
    })
    assert resp.get_json() == {
        "watched_dates": ["2026-12-18", "2026-12-19"], "watched_event_ids": ["267914"],
        "unwatched_event_ids": ["267915"],
    }
    movie = storage.load_config()["movies"][0]
    assert movie["watched_dates"] == ["2026-12-18", "2026-12-19"]
    assert client.post("/movies/nope/watch", headers=SAME_ORIGIN, json={}).status_code == 404


def test_seat_map(client, monkeypatch):
    finish_setup(client)
    add_dune()
    plan = {"1_1_1": ["1", "2"], "1_2_1": ["1", "1"], "1_1_2": ["2", "2"], "1_2_2": ["2", "V1"]}
    storage.save_results([{"event_id": "7", "movie_id": "m", "venue_id": 80, "seatplan_id": 1,
                           "venue_type_id": 1, "is_reserved": True, "freed_seats": 1}])
    storage.save_seatplans({"80/1": {"seats": plan, "fetched_at": "2026-10-25T20:00:00"}})
    storage.save_state({"notified_event_ids": [], "seats": {"7": {
        "free": ["1_1_1"], "baseline": [], "checked_at": "2026-10-25T20:00:00",
        "freed": {"1_1_1": webui.cinemacity.now().isoformat(timespec="seconds")},
    }}})
    monkeypatch.setattr(webui.cinemacity, "fetch_seatplan", lambda *a: pytest.fail("plan is cached"))
    monkeypatch.setattr(webui.cinemacity, "fetch_free_seats", lambda eid, vt, r: ["1_1_1", "1_2_2"])
    data = client.get("/api/movies/m/showtimes/7/seats").get_json()
    # 1_2_2 is a wheelchair space: shown, never counted.
    assert (data["free"], data["capacity"], data["freed"], data["min_x"], data["max_x"]) == (1, 3, 1, 1, 2)
    assert [s["state"] for row in data["rows"] for s in row["seats"]] == ["freed", "taken", "taken", "free"]
    assert data["rows"][1]["seats"][1]["wheelchair"] is True
    assert data["hall"] == "80/1" and data["areas"]["preferred"]  # Flora IMAX default

    def down(*a):
        raise webui.FetchError("network", "down")

    monkeypatch.setattr(webui.cinemacity, "fetch_free_seats", down)
    data = client.get("/api/movies/m/showtimes/7/seats").get_json()
    assert data["free"] == 1 and data["checked_at"] == "2026-10-25T20:00:00"  # the scraper's last fetch
    assert client.get("/api/movies/m/showtimes/8/seats").status_code == 404


def test_seat_areas_are_saved_per_hall(client):
    finish_setup(client)
    resp = client.post("/settings/seat-areas", headers=SAME_ORIGIN, json={
        "hall": "80/1", "preferred": {"rows": [8, 5], "cols": [15, 27]}, "acceptable": {"rows": "x"},
    })
    assert resp.get_json()["areas"] == {"preferred": {"rows": [5, 8], "cols": [15, 27]}}
    assert storage.seat_areas(storage.load_config()["settings"], "80/1") == {"preferred": {"rows": [5, 8], "cols": [15, 27]}}
    assert client.post("/settings/seat-areas", headers=SAME_ORIGIN, json={"hall": "../x"}).status_code == 400


def test_pages_show_freed_seats(client):
    finish_setup(client)
    add_dune("custom")
    storage.save_results([{"event_id": "7", "movie_id": "m", "availability": 0.5, "sold_out": False,
                           "start_time": "2026-12-19T17:20:00", "freed_seats": 3}])
    assert "+3 seats freed" in client.get("/").get_data(as_text=True)
    page = client.get("/movies/m").get_data(as_text=True)
    assert "showtimes from 0 rules and the bells" in page


def test_tour_opens_once_and_on_request(client):
    finish_setup(client)
    assert 'id="tour-overlay"' in client.get("/").get_data(as_text=True)
    client.post("/settings/tour-seen", headers=SAME_ORIGIN)
    assert 'id="tour-overlay"' not in client.get("/").get_data(as_text=True)
    assert 'id="tour-overlay"' in client.get("/?tour=1").get_data(as_text=True)
