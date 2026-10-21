import pytest

import cinemacity
from cinemacity import FetchError
from conftest import load_fixture


def test_extract_events_parses_real_feed():
    events = cinemacity.extract_events(load_fixture("film_events.json"))
    formats = {e["format"] for e in events}
    assert formats == {"IMAX", "2D", "4DX", "VIP", "3D"}
    imax = next(e for e in events if e["format"] == "IMAX")
    assert imax["is_imax"]
    for e in events:
        assert e["movie_name"]
        assert e["start_time"]
        assert "/api/order/" not in e["booking_link"]
        assert e["booking_link"].startswith("https://tickets.cinemacity.cz/order/")


def test_extract_events_treats_zero_availability_as_sold_out():
    payload = {"body": {"films": [{"id": "f", "name": "Film"}], "events": [
        {"id": "1", "filmId": "f", "availabilityRatio": 0, "soldOut": False},
        {"id": "2", "filmId": "f", "availabilityRatio": 0.4, "soldOut": False},
    ]}}
    sold = {e["event_id"]: e["sold_out"] for e in cinemacity.extract_events(payload)}
    assert sold == {"1": True, "2": False}


@pytest.mark.parametrize("attrs, tiny, expected", [
    ({"2d", "70-mm"}, "imax", "IMAX"),
    ({"2d", "imax"}, "7", "IMAX"),
    ({"2d", "70-mm"}, "7", "IMAX"),
    ({"3d", "4dx"}, "4", "4DX"),
    ({"2d", "dolby-atmos", "superscreen"}, "1", "Superscreen"),
    ({"2d", "vip", "recliners"}, "9", "VIP"),
    ({"3d"}, "2", "3D"),
    ({"2d", "dolby-atmos"}, "3", "2D"),
    (set(), "", "2D"),
])
def test_detect_format(attrs, tiny, expected):
    assert cinemacity.detect_format(attrs, tiny) == expected


def test_detect_format_only_returns_known_formats():
    import storage
    for name, _ in cinemacity.FORMAT_TAGS:
        assert name in storage.FORMAT_OPTIONS
    assert {"IMAX", "2D"} <= set(storage.FORMAT_OPTIONS)


def test_audio_prefers_dub_over_original():
    audio, code, subs, subs_code = cinemacity.audio_and_subtitle_languages(
        {"original": ["en"], "dubbed": ["cs"], "subtitles": []}
    )
    assert (audio, code, subs, subs_code) == ("Czech", "cs", None, None)


@pytest.mark.parametrize("value, expected", [
    ("2026-10-20 09:00:00", "2026-10-20T09:00:00"),
    ("2026-10-20 09:00", "2026-10-20T09:00:00"),
    ("20.10.2026", None),
    (None, None),
])
def test_booking_site_time(value, expected):
    assert cinemacity.booking_site_time(value) == expected


def test_fetch_sale_info_open(monkeypatch):
    monkeypatch.setattr(cinemacity, "get_json", lambda url, what: load_fixture("presentation_open.json"))
    info = cinemacity.fetch_sale_info("267914")
    assert info["sale_start"] == "2026-10-20T09:00:00"
    assert info["sale_stop"] == "2026-12-19T17:35:00"
    assert info["member_only"] is False
    assert "locked" not in info


def test_fetch_sale_info_locked(monkeypatch):
    monkeypatch.setattr(cinemacity, "get_json", lambda url, what: load_fixture("presentation_locked.json"))
    info = cinemacity.fetch_sale_info("267914")
    assert info == {"sale_start": "2026-10-20T09:00:00", "sale_stop": None, "member_only": False, "locked": True}


@pytest.mark.parametrize("payload", [
    load_fixture("presentation_not_found.json"),
    {"error": {"error": "SOMETHING_NEW", "message": "?"}},
    {"error": {"error": "TICKETING_NOT_STARTED", "data": "soon"}},
    {"unexpected": True},
])
def test_fetch_sale_info_errors(monkeypatch, payload):
    monkeypatch.setattr(cinemacity, "get_json", lambda url, what: payload)
    with pytest.raises(FetchError) as exc:
        cinemacity.fetch_sale_info("1")
    assert exc.value.kind == "invalid"


def test_fetch_cinemas(monkeypatch):
    monkeypatch.setattr(cinemacity, "get_json", lambda url, what: load_fixture("cinemas.json"))
    cinemas = cinemacity.fetch_cinemas("2027-10-01")
    assert len(cinemas) == 13
    assert [c["name"] for c in cinemas] == sorted(c["name"] for c in cinemas)
    flora = next(c for c in cinemas if c["id"] == "1052")
    assert flora["name"].startswith("Prague Flora")
    assert flora["link"] == "https://www.cinemacity.cz/cinemas/flora"


def test_fetch_cinemas_rejects_empty(monkeypatch):
    monkeypatch.setattr(cinemacity, "get_json", lambda url, what: {"body": {"cinemas": []}})
    with pytest.raises(FetchError):
        cinemacity.fetch_cinemas("2027-10-01")


def test_fetch_dates(monkeypatch):
    monkeypatch.setattr(cinemacity, "get_json", lambda url, what: load_fixture("dates.json"))
    dates = cinemacity.fetch_dates("1052", "2027-10-01")
    assert dates == sorted(dates) and len(dates) > 1


def test_cinema_day_link():
    link = cinemacity.cinema_day_link({"id": "1056", "link": "https://www.cinemacity.cz/cinemas/chodov"}, "2026-11-01")
    assert link.startswith("https://www.cinemacity.cz/cinemas/chodov/1056#")
    assert "in-cinema=1056" in link and "at=2026-11-01" in link


def test_user_agent_identifies_the_project():
    assert "cinematix/" in cinemacity.HEADERS["User-Agent"]
    assert "github.com/timojaakob/cinematix" in cinemacity.HEADERS["User-Agent"]
