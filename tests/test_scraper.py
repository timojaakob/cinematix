from datetime import datetime, timedelta

import pytest

import cinemacity
import scraper
import storage
from cinemacity import FetchError
from conftest import load_fixture

NOW = datetime(2026, 10, 21, 14, 0)


def show(eid, avail, sold=False, sale_start=None, start="2026-12-18T20:30:00", movie_id="tmdb-1"):
    return {
        "event_id": eid, "movie_id": movie_id, "movie_name": "Dune: Part Three", "format": "IMAX", "date": start[:10],
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
    settings = {"public_url": "https://app.example/"}
    scraper.push_picks([show("1", 0.5)], "new", settings, NOW)
    scraper.push_picks([show("1", 0.5), show("2", 0.5)], "new", settings, NOW)
    scraper.push_picks([show("1", 0.5), show("2", 0.5, movie_id="tmdb-2")], "new", settings, NOW)
    assert sent == [
        "https://tickets.cinemacity.cz/order/1",
        "https://app.example/movies/tmdb-1?changed=1",  # one movie: its page, changes on top
        "https://app.example/",  # several movies: the movie list
    ]
    assert scraper.push_link([show("1", 0.5), show("2", 0.5)], {}) is None


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
        info["venue_id"] = 80  # what the fetch after the drop returns
    sale_calls.clear()
    scraper.attach_sale_info(rows(), state, datetime(2026, 10, 22, 12, 0), True)
    assert sale_calls == []  # on sale: never again

    del state["sale_info"]["1"]["venue_id"]
    scraper.attach_sale_info(rows(), state, datetime(2026, 10, 22, 12, 2), True)
    assert sale_calls == ["1"]  # cached before seat plans: fetched once more


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
    assert state["booking_site_backoff_until"] == "2026-10-21T14:15:00"


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


# --- seats ------------------------------------------------------------------------------

# A 2-row hall, 4 seats per row: keys "1_{x}_{y}". Not venue 80, which has default areas.
PLAN = {f"1_{x}_{y}": [str(y), str(x)] for y in (1, 2) for x in range(1, 5)}
PLANS = {"999/1": {"seats": PLAN, "fetched_at": NOW.isoformat()}}


def seat_show(eid, free, sold=False, sale_start=None):
    """A showtime in the PLAN hall with these seats free."""
    return dict(
        show(eid, len(free) / len(PLAN), sold=sold or not free and sale_start is None, sale_start=sale_start),
        movie_id="m", venue_id=999, seatplan_id=1, venue_type_id=1, is_reserved=True,
    )


@pytest.fixture
def seat_cycle(monkeypatch):
    """Runs the seat part of a cycle and the picking, marking pushes like run_once."""
    free_now: dict[str, list[str]] = {}
    fetched = []

    def fetch(eid, venue_type_id, is_reserved):
        fetched.append(eid)
        return free_now[eid]

    monkeypatch.setattr(cinemacity, "fetch_free_seats", fetch)
    state = {"notified_event_ids": []}

    def cycle(movie, free, now=NOW, areas=None, **kw):
        free_now["1"] = free
        r = seat_show("1", free, **kw)
        settings = {"seat_areas": {"999/1": areas or {}}}
        scraper.update_seats([r], state, PLANS, {"m": movie}, now, True)
        scraper.annotate_seats([r], state, PLANS, now, settings)
        picks = scraper.pick_notifications([r], state, {"m": movie}, PLANS, settings)
        cycle.last = picks
        for p in picks:
            if p["notify_kind"] == "new":
                state["notified_event_ids"].append("1")
            state["availability_baseline"]["1"] = p["availability"]
            if "1" in state.get("seats", {}):
                state["seats"]["1"]["baseline"] = state["seats"]["1"]["free"]
        return [(p["notify_kind"], p.get("freed_count")) for p in picks]

    cycle.state = state
    cycle.fetched = fetched
    return cycle


def test_freed_seats_must_form_a_group(seat_cycle):
    movie = {"id": "m", "seats_wanted": 2}
    assert seat_cycle(movie, ["1_1_1"]) == [("new", None)]
    assert seat_cycle(movie, ["1_1_1", "1_3_1"]) == []       # one seat, alone
    assert seat_cycle(movie, ["1_1_1", "1_3_1", "1_3_2"]) == []  # two, not side by side
    assert seat_cycle(movie, ["1_1_1", "1_3_1", "1_3_2", "1_4_2"]) == [("more_seats", 3)]
    assert seat_cycle(movie, ["1_1_1", "1_3_1", "1_3_2", "1_4_2"]) == []  # unchanged: not fetched
    assert seat_cycle.fetched == ["1", "1", "1", "1"]


def test_freed_seat_next_to_a_free_one_counts(seat_cycle):
    movie = {"id": "m", "seats_wanted": 2}
    seat_cycle(movie, ["1_1_1"])
    assert seat_cycle(movie, ["1_1_1", "1_2_1"]) == [("more_seats", 1)]


def test_group_of_already_free_seats_does_not_count(seat_cycle):
    movie = {"id": "m", "seats_wanted": 2}
    seat_cycle(movie, ["1_1_1", "1_2_1"])
    # A group was free all along; the freed seat 1_4_2 is alone.
    assert seat_cycle(movie, ["1_1_1", "1_2_1", "1_4_2"]) == []


# Rows 1-2: preferred is columns 2-3 of row 1, acceptable columns 1-4 of row 1.
AREAS = {"preferred": {"rows": [1, 1], "cols": [2, 3]}, "acceptable": {"rows": [1, 1], "cols": [1, 4]}}


def test_groups_must_be_inside_the_areas(seat_cycle):
    movie = {"id": "m", "seats_wanted": 2}
    seat_cycle(movie, [], areas=AREAS, sale_start="2026-10-22T09:00:00")
    later = datetime(2026, 10, 22, 9, 1)
    assert seat_cycle(movie, ["1_1_2", "1_2_2"], now=later, areas=AREAS) == []  # row 2: outside
    assert seat_cycle(movie, ["1_1_2", "1_2_2", "1_1_1", "1_2_1"], now=later, areas=AREAS) == [("more_seats", 2)]
    assert seat_cycle.last[0]["area"] == "acceptable"


def test_preferred_groups_come_first(seat_cycle):
    movie = {"id": "m", "seats_wanted": 2}
    seat_cycle(movie, ["1_1_2"], areas=AREAS)
    assert seat_cycle(movie, ["1_1_2"] + [f"1_{x}_1" for x in range(1, 5)], areas=AREAS) == [("more_seats", 4)]
    assert seat_cycle.last[0]["area"] == "preferred"
    assert seat_cycle.last[0]["group"] == "row 1, seats 2-3"


def test_four_freed_as_two_behind_two(seat_cycle):
    movie = {"id": "m", "seats_wanted": 4}
    seat_cycle(movie, ["1_4_1"])
    assert seat_cycle(movie, ["1_4_1", "1_1_1", "1_2_1", "1_1_2", "1_2_2"]) == [("more_seats", 4)]


def test_any_freed_seat_by_default(seat_cycle):
    seat_cycle({"id": "m"}, ["1_1_1"])
    assert seat_cycle({"id": "m"}, ["1_1_1", "1_4_2"]) == [("more_seats", 1)]


def test_sold_out_reopens_and_is_marked_freed(seat_cycle):
    movie = {"id": "m", "seats_wanted": 2}
    seat_cycle(movie, ["1_1_1"])
    assert seat_cycle(movie, []) == []
    assert seat_cycle.state["seats"]["1"]["closed"] == "sold"
    assert seat_cycle(movie, ["1_1_1", "1_2_1"], now=NOW + timedelta(minutes=2)) == [("more_seats", 2)]
    rec = seat_cycle.state["seats"]["1"]
    assert sorted(rec["freed"]) == ["1_1_1", "1_2_1"]


def test_booking_opening_pushes_but_marks_nothing_freed(seat_cycle):
    movie = {"id": "m", "seats_wanted": 2}
    everything = sorted(PLAN)
    assert seat_cycle(movie, everything, sale_start="2026-10-22T09:00:00") == [("new", None)]
    assert seat_cycle.fetched == []
    assert seat_cycle(movie, everything, now=datetime(2026, 10, 22, 9, 1)) == [("more_seats", 8)]
    assert seat_cycle.state["seats"]["1"]["freed"] == {}


def test_unwatched_showtimes_get_seat_counts_but_no_push(seat_cycle):
    movie = {"id": "m", "seat_alerts": "custom", "watched_dates": ["2026-12-19"]}
    seat_cycle(movie, ["1_1_1"])
    assert seat_cycle(movie, ["1_1_1", "1_2_1"]) == []
    assert seat_cycle.fetched == ["1", "1"]
    movie["watched_event_ids"] = ["1"]
    assert seat_cycle(movie, ["1_1_1", "1_2_1", "1_3_1"]) == [("more_seats", 2)]  # freed since the last push


def test_watched_date_covers_its_showtimes():
    movie = {"seat_alerts": "custom", "watched_dates": ["2026-12-18"], "watched_event_ids": ["9"]}
    assert storage.watches_showtime(movie, {"start_time": "2026-12-18T20:30:00", "event_id": "1"})
    assert storage.watches_showtime(movie, {"start_time": "2026-12-19T20:30:00", "event_id": "9"})
    assert not storage.watches_showtime(movie, {"start_time": "2026-12-19T20:30:00", "event_id": "1"})
    assert storage.watches_showtime({}, {"start_time": "2026-12-19T20:30:00", "event_id": "1"})


EVENINGS = {"days": [1, 2, 3, 4, 5], "from": "17:00", "to": "23:59", "date_from": "", "date_to": ""}


@pytest.mark.parametrize("start, expected", [
    ("2026-12-18T20:30:00", True),   # Friday evening
    ("2026-12-18T16:59:00", False),  # Friday afternoon
    ("2026-12-19T20:30:00", False),  # Saturday
])
def test_watch_rule_days_and_times(start, expected):
    movie = {"seat_alerts": "custom", "watch_rules": [EVENINGS]}
    assert storage.watches_showtime(movie, {"start_time": start, "event_id": "1"}) is expected


def test_watch_rule_date_range_and_bell_off():
    rule = dict(EVENINGS, days=[1, 2, 3, 4, 5, 6, 7], date_from="2026-12-18", date_to="2026-12-20")
    movie = {"seat_alerts": "custom", "watch_rules": [rule], "unwatched_event_ids": ["2"]}
    assert storage.watches_showtime(movie, {"start_time": "2026-12-20T21:00:00", "event_id": "1"})
    assert not storage.watches_showtime(movie, {"start_time": "2026-12-21T21:00:00", "event_id": "1"})
    assert not storage.watches_showtime(movie, {"start_time": "2026-12-20T21:00:00", "event_id": "2"})


def test_bells_only_mode_becomes_custom(data_dir):
    cfg = storage.load_config()
    cfg["movies"] = [{"id": "m", "seat_alerts": "selected"}]
    storage.save_config(cfg)
    assert storage.load_config()["movies"][0]["seat_alerts"] == "custom"


def test_default_areas_until_the_user_saves_some():
    # Centred: Flora IMAX rows are symmetric around grid column 20.5.
    areas = storage.seat_areas({}, "80/1")
    assert areas["preferred"] == {"rows": [5, 8], "cols": [15, 26]}
    assert areas["acceptable"] == {"rows": [4, 12], "cols": [13, 28]}
    for rect in areas.values():
        assert sum(rect["cols"]) / 2 == 20.5
    assert storage.seat_areas({"seat_areas": {"80/1": {}}}, "80/1") == {}
    assert storage.seat_areas({}, "72/1") == {}


def test_without_seat_data_the_ratio_must_rise_by_enough_seats():
    state = {"notified_event_ids": ["1"], "availability_baseline": {"1": 0.1}}
    movie = {"id": "m", "seats_wanted": 3}
    r = dict(show("1", 0.15), movie_id="m", capacity=40)  # +2 seats
    assert scraper.pick_notifications([r], state, {"m": movie}) == []
    r = dict(show("1", 0.175), movie_id="m", capacity=40)  # +3 seats
    [pick] = scraper.pick_notifications([r], state, {"m": movie})
    assert (pick["freed_count"], pick["free_count"]) == (3, 7)


def test_seat_push_waits_when_the_fetch_was_capped(monkeypatch):
    monkeypatch.setattr(scraper, "SEAT_FETCHES_PER_CYCLE", 0)
    state = {"notified_event_ids": ["1"], "availability_baseline": {"1": 0.0}}
    r = seat_show("1", ["1_1_1"])
    scraper.update_seats([r], state, PLANS, {}, NOW, True)
    assert r["seat_pending"] and scraper.pick_notifications([r], state, {}, PLANS) == []


def test_annotate_seats_counts_only_fetched_seats():
    r = seat_show("1", ["1_1_1", "1_2_1"])
    scraper.annotate_seats([r], {}, PLANS, NOW, {})
    assert (r["capacity"], r["free_seats"], r["freed_seats"], r["has_seat_map"]) == (8, None, 0, True)
    r = seat_show("1", [], sale_start="2026-10-22T09:00:00")
    scraper.annotate_seats([r], {}, PLANS, NOW, {})
    assert r["free_seats"] is None


def test_more_seats_summary_names_the_area():
    pick = dict(show("1", 0.2), previous_availability=0.1, freed_count=2, free_count=9, seats_wanted=2,
                area="preferred", group="row 6, seats 20-21")
    _, body = scraper.summarize_more_seats([pick])
    assert body.endswith(": +2 seats in your areas, 9 free now. 2 together in the preferred area: row 6, seats 20-21")


def test_more_seats_summary_with_seat_counts():
    pick = dict(show("1", 0.2), previous_availability=0.1, freed_count=4, free_count=12, seats_wanted=4,
                group="row 7, seats 12-13 + row 8, seats 12-13")
    _, body = scraper.summarize_more_seats([pick])
    assert body == (
        "Dune: Part Three IMAX, Fri 18 Dec 20:30: +4 seats, 12 free now. "
        "4 together: row 7, seats 12-13 + row 8, seats 12-13"
    )


def test_seatplans_cached_and_refetched_when_stale(monkeypatch):
    calls = []
    monkeypatch.setattr(cinemacity, "fetch_seatplan", lambda v, p: calls.append((v, p)) or PLAN)
    plans, state = {}, {}
    rows = [seat_show("1", ["1_1_1"]), seat_show("2", ["1_1_1"])]
    assert scraper.update_seatplans(rows, plans, state, NOW, True)
    assert calls == [(999, 1)] and plans["999/1"]["seats"] == PLAN
    assert not scraper.update_seatplans(rows, plans, state, NOW, True)
    plans["999/1"]["stale"] = True
    assert scraper.update_seatplans(rows, plans, state, NOW, True) and len(calls) == 2


def test_annotate_seats_counts_the_areas_without_wheelchair_spaces():
    plan = dict(PLAN, **{"1_4_1": ["1", "V1"]})
    plans = {"999/1": {"seats": plan}}
    state = {"seats": {"1": {"free": ["1_1_1", "1_2_1", "1_4_1", "1_1_2"], "baseline": [], "ratio": 0.5, "freed": {}}}}
    r = dict(seat_show("1", ["1_1_1"]), availability=0.5)
    areas = {"acceptable": {"rows": [1, 1], "cols": [1, 4]}}
    scraper.annotate_seats([r], state, plans, NOW, {"seat_areas": {"999/1": areas}})
    assert (r["capacity"], r["free_seats"], r["seat_ratio"], r["in_areas"]) == (3, 2, 2 / 3, True)
