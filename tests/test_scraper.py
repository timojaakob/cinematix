from datetime import datetime, timedelta

import pytest

import cinemacity
import scraper
import storage
from cinemacity import FetchError
from conftest import load_fixture

NOW = datetime(2026, 10, 21, 14, 0)


def show(eid, avail, sold=False, sale_start=None, start="2026-12-18T20:30:00"):
    return {
        "event_id": eid, "movie_name": "Dune: Part Three", "format": "IMAX", "date": start[:10],
        "start_time": start, "availability": avail, "sold_out": sold, "sale_start": sale_start,
        "booking_link": f"https://tickets.cinemacity.cz/order/{eid}",
    }


# --- pick_notifications ----------------------------------------------------------

def run_picks(state, shows):
    """One cycle of picking, marking the picks as notified like run_once does."""
    picks = scraper.pick_notifications(shows, state)
    for p in picks:
        if p["notify_kind"] == "new":
            state["notified_event_ids"].append(p["event_id"])
        state["availability_baseline"][p["event_id"]] = p["availability"]
    return [(p["event_id"], p["notify_kind"]) for p in picks]


def test_seat_return_sequence():
    state = {"notified_event_ids": []}
    assert run_picks(state, [show("1", 0.5)]) == [("1", "new")]
    assert run_picks(state, [show("1", 0.01)]) == []           # seats sold: no push
    assert run_picks(state, [show("1", 0.01)]) == []           # unchanged
    assert run_picks(state, [show("1", 0.0124)]) == [("1", "more_seats")]  # one seat back
    assert run_picks(state, [show("1", 0.0124)]) == []
    assert run_picks(state, [show("1", 0.0, sold=True)]) == []
    assert run_picks(state, [show("1", 0.0024)]) == [("1", "more_seats")]  # reopened


def test_sold_out_new_showtime_waits():
    state = {"notified_event_ids": []}
    assert run_picks(state, [show("1", 0.0, sold=True)]) == []
    assert run_picks(state, [show("1", 0.2)]) == [("1", "new")]


def test_summaries():
    picks = [show("1", 0.5, sale_start="2026-10-22T09:00:00"), show("2", 0.5, sale_start="2026-10-22T09:00:00")]
    title, body = scraper.summarize_new_showtimes(picks, NOW)
    assert title == "2 new showtimes listed, booking not open yet"
    assert body == "Dune: Part Three: 2, booking opens Thu 22 Oct 09:00"
    title, _ = scraper.summarize_new_showtimes(picks + [show("3", 0.5)], NOW)
    assert title == "3 new showtimes available"
    title, body = scraper.summarize_more_seats([dict(show("1", 0.0124), previous_availability=0.01)])
    assert title == "Seats freed up in 1 showtime"
    assert body == "Dune: Part Three IMAX, Fri 18 Dec 20:30: 1.0% -> 1.2% free"


def test_push_links_single_showtime_to_booking_page(monkeypatch):
    sent = []
    monkeypatch.setattr(scraper, "notify", lambda t, m, url, p, s: sent.append(url) or True)
    settings = {"public_url": "https://app.example"}
    scraper.push_picks([show("1", 0.5)], "new", settings, NOW)
    scraper.push_picks([show("1", 0.5), show("2", 0.5)], "new", settings, NOW)
    assert sent == ["https://tickets.cinemacity.cz/order/1", "https://app.example"]


def test_disabled_push_still_marks_notified(monkeypatch):
    monkeypatch.setattr(scraper, "notify", lambda *a: pytest.fail("should not send"))
    assert scraper.push_picks([show("1", 0.5)], "more_seats", {"notify_on_more_seats": False}, NOW)


def test_notify_without_endpoint_does_not_pile_up():
    assert scraper.notify("t", "m", None, "low", {"ntfy_endpoint": ""}) is True


# --- attach_sale_info --------------------------------------------------------------

@pytest.fixture
def sale_calls(monkeypatch):
    calls = []

    def fake(eid):
        calls.append(eid)
        return {"sale_start": "2026-10-22T09:00:00", "sale_stop": None, "member_only": eid == "2", "locked": True}

    monkeypatch.setattr(cinemacity, "fetch_sale_info", fake)
    return calls


def test_sale_info_cache_and_refresh(sale_calls):
    state = {}
    rows = lambda: [{"event_id": "1"}, {"event_id": "2"}, {"event_id": "1"}]  # noqa: E731
    r = rows()
    scraper.attach_sale_info(r, state, NOW, True)
    assert sorted(sale_calls) == ["1", "2"]
    assert r[1]["member_only"] is True and r[0]["sale_start"] == "2026-10-22T09:00:00"

    sale_calls.clear()
    scraper.attach_sale_info(rows(), state, NOW + timedelta(minutes=2), True)
    assert sale_calls == []  # cached
    scraper.attach_sale_info(rows(), state, NOW + timedelta(minutes=61), True)
    assert sorted(sale_calls) == ["1", "2"]  # upcoming: rechecked hourly

    sale_calls.clear()
    scraper.attach_sale_info(rows(), state, datetime(2026, 10, 22, 9, 5), True)
    assert sorted(sale_calls) == ["1", "2"]  # locked: fetched once after the drop
    for info in state["sale_info"].values():
        info.pop("locked")
    sale_calls.clear()
    scraper.attach_sale_info(rows(), state, datetime(2026, 10, 22, 12, 0), True)
    assert sale_calls == []  # on sale: never again


def test_sale_info_not_fetched_when_offline(sale_calls):
    r = [{"event_id": "1"}]
    scraper.attach_sale_info(r, {}, NOW, may_fetch=False)
    assert sale_calls == [] and r[0]["sale_start"] is None


def test_sale_info_blocked_backs_off(monkeypatch):
    calls = []

    def blocked(eid):
        calls.append(eid)
        raise FetchError("blocked", "HTTP 429", 429)

    monkeypatch.setattr(cinemacity, "fetch_sale_info", blocked)
    state = {}
    scraper.attach_sale_info([{"event_id": "1"}, {"event_id": "2"}], state, NOW, True)
    scraper.attach_sale_info([{"event_id": "1"}], state, NOW + timedelta(minutes=2), True)
    assert len(calls) == 1  # stopped at the first 429, nothing in the next cycle
    assert state["sale_info_backoff_until"] == "2026-10-21T14:15:00"


def test_sale_info_bad_response_retried_hourly(monkeypatch):
    calls = []

    def bad(eid):
        calls.append(eid)
        raise FetchError("invalid", "PRESENTATION_NOT_FOUND")

    monkeypatch.setattr(cinemacity, "fetch_sale_info", bad)
    state = {}
    for minutes in (0, 2, 61):
        scraper.attach_sale_info([{"event_id": "9"}], state, NOW + timedelta(minutes=minutes), True)
    assert calls == ["9", "9"]


# --- due checks, dates, cinema switching ----------------------------------------------

def test_is_movie_due():
    movie = {"id": "m", "check_interval_seconds": 120}
    assert scraper.is_movie_due(movie, {}, NOW)
    state = {"last_checked": {"m": (NOW - timedelta(seconds=60)).isoformat()}}
    assert not scraper.is_movie_due(movie, state, NOW)
    assert scraper.is_movie_due(movie, state, NOW + timedelta(seconds=61))
    assert not scraper.is_movie_due(dict(movie, check_interval_seconds=None), {}, NOW)


def test_choose_dates_merges_previous(monkeypatch):
    monkeypatch.setattr(cinemacity, "fetch_dates", lambda cid, until: ["2026-10-21", "2026-10-25"])
    state = {"last_dates": ["2026-10-20", "2026-10-23"], "last_full_scan_at": NOW.isoformat()}
    dates, complete, err = scraper.choose_dates("1052", state, NOW, {"2026-10-30"})
    assert dates == ["2026-10-21", "2026-10-23", "2026-10-25", "2026-10-30"]
    assert complete and err is None


def test_choose_dates_offline(monkeypatch):
    def offline(cid, until):
        raise FetchError("network", "down")

    monkeypatch.setattr(cinemacity, "fetch_dates", offline)
    dates, complete, err = scraper.choose_dates("1052", {}, NOW, set())
    assert dates == [] and not complete and err.kind == "network"


def test_switch_cinema(data_dir):
    state = {"notified_event_ids": ["1"], "sale_info": {"1": {}}}
    scraper.switch_cinema(state, "1052")  # pre-1.0 state: kept
    assert state["notified_event_ids"] == ["1"] and state["cinema_id"] == "1052"
    scraper.switch_cinema(state, "1052")
    assert state["notified_event_ids"] == ["1"]
    storage.save_results([show("1", 0.5)])
    scraper.switch_cinema(state, "1056")
    assert state == {"cinema_id": "1056", "notified_event_ids": []}
    assert storage.load_results()["matches"] == []


# --- run_once end to end ---------------------------------------------------------------

@pytest.fixture
def live_like(set_up_config, monkeypatch):
    """run_once against the captured feed, with the clock fixed and pushes recorded."""
    feed = load_fixture("film_events.json")
    events = feed["body"]["events"]
    imax = next(e for e in events if e["auditoriumTinyName"] == "IMAX" or "70-mm" in e["attributeIds"])
    film = next(f for f in feed["body"]["films"] if f["id"] == imax["filmId"])
    day = imax["eventDateTime"][:10]
    cfg = storage.load_config()
    cfg["movies"] = [{"id": "m1", "title": film["name"], "query": film["name"].lower(),
                      "required_formats": ["IMAX"], "check_interval_seconds": 60}]
    storage.save_config(cfg)

    monkeypatch.setattr(cinemacity, "now", lambda: datetime.fromisoformat(day + "T08:00:00"))
    monkeypatch.setattr(cinemacity, "fetch_dates", lambda cid, until: [day])
    monkeypatch.setattr(cinemacity, "fetch_day", lambda cid, d: feed if d == day else {"body": {}})
    monkeypatch.setattr(cinemacity, "fetch_sale_info",
                        lambda eid: {"sale_start": "2026-01-01T09:00:00", "sale_stop": None, "member_only": False})
    sent = []
    monkeypatch.setattr(scraper, "notify", lambda title, message, url, priority, settings: sent.append(title) or True)
    return {"imax": imax, "sent": sent, "day": day}


def test_run_once_end_to_end(live_like):
    state = storage.load_state()
    scraper.run_once(state)
    matches = storage.load_results()["matches"]
    assert [m["event_id"] for m in matches] == [str(live_like["imax"]["id"])]
    assert matches[0]["sale_start"] == "2026-01-01T09:00:00"
    assert live_like["sent"] == ["1 new showtime available"]
    assert state["cinema_id"] == "1052" and "m1" in state["last_checked"]

    live_like["sent"].clear()
    scraper.FORCE_CHECK_IDS.add("m1")
    scraper.run_once(state)
    assert live_like["sent"] == []  # nothing new


def test_run_once_waits_for_setup(data_dir, monkeypatch):
    monkeypatch.setattr(cinemacity, "fetch_dates", lambda *a: pytest.fail("must not poll before setup"))
    scraper.run_once(storage.load_state())
