import pytest

import cinemacity
import seats
from conftest import load_fixture


def grid(*rows: str) -> list[str]:
    """Free seats from pictures of rows, front row first: "o" is free,
    "x" taken, " " an aisle. Column 1 is the first character."""
    return [f"1_{x}_{y}" for y, row in enumerate(rows, 1) for x, c in enumerate(row, 1) if c == "o"]


@pytest.mark.parametrize("n, expected", [
    (1, [(1, 0)]), (3, [(3, 0)]), (4, [(4, 0), (2, 2)]), (5, [(5, 0), (3, 2)]),
    (6, [(6, 0), (4, 2), (3, 3)]), (7, [(7, 0), (5, 2), (4, 3)]),
])
def test_splits(n, expected):
    assert seats.splits(n) == expected


def test_two_and_three_need_one_row():
    assert seats.find_groups(grid("oo", "oo"), 3) == []
    assert seats.find_groups(grid("xooox"), 3) == [["1_2_1", "1_3_1", "1_4_1"]]


def test_an_aisle_breaks_a_row():
    assert seats.find_groups(grid("oo oo"), 3) == []


def test_four_as_two_behind_two():
    assert seats.find_groups(grid("oox", "oox"), 4) == [["1_1_1", "1_2_1", "1_1_2", "1_2_2"]]


def test_blocks_must_share_columns():
    assert seats.find_groups(grid("oox", "xoo"), 4) == []


def test_five_is_three_and_two_behind_part_of_it():
    assert len(seats.find_groups(grid("ooo", "xoo"), 5)) == 1
    assert seats.find_groups(grid("ooox", "xxoo"), 5) == []


def test_six_as_four_and_two_or_three_and_three():
    assert len(seats.find_groups(grid("oooo", "xoox"), 6)) == 1
    assert seats.find_groups(grid("ooo", "ooo"), 6) == [["1_1_1", "1_2_1", "1_3_1", "1_1_2", "1_2_2", "1_3_2"]]


def test_never_three_rows():
    assert seats.find_groups(grid("oo", "oo", "oo"), 6) == []


def test_rows_must_be_neighbours():
    assert seats.find_groups(grid("oo", "xx", "oo"), 4) == []


def test_wheelchair_spaces_never_count():
    plan = {"1_1_1": ["1", "2"], "1_2_1": ["1", "V1"]}
    assert seats.usable(["1_1_1", "1_2_1", "9_9_9"], plan) == {"1_1_1"}


def test_describe():
    plan = {"1_1_7": ["7", "13"], "1_2_7": ["7", "12"], "1_1_8": ["8", "13"], "1_2_8": ["8", "12"]}
    assert seats.describe(["1_1_8", "1_2_8", "1_1_7", "1_2_7"], plan) == "row 7, seats 12-13 + row 8, seats 12-13"
    assert seats.describe(["1_1_7"], plan) == "row 7, seat 13"


def test_real_imax_hall(monkeypatch):
    monkeypatch.setattr(cinemacity, "get_json", lambda url, what: load_fixture("seatplan_imax.json"))
    plan = cinemacity.fetch_seatplan(80, 1)
    free = seats.usable(load_fixture("seat_status_imax.json")["seats"], plan)
    assert len(free) == 235  # 241 free, 6 of them wheelchair spaces
    for n in range(1, seats.MAX_PARTY_SIZE + 1):
        for group in seats.find_groups(free, n):
            assert len(group) == n and set(group) <= free


AREAS = {"preferred": {"rows": [2, 2], "cols": [2, 3]}, "acceptable": {"rows": [1, 3], "cols": [1, 4]}}


def test_area_of():
    assert seats.area_of("1_2_2", AREAS) == "preferred"
    assert seats.area_of("1_1_2", AREAS) == "acceptable"
    assert seats.area_of("1_5_2", AREAS) is None
    assert seats.area_of("1_5_2", {}) == "acceptable"


def test_best_groups_need_a_freed_seat_and_prefer_the_preferred_area():
    free = set(grid("oooo", "oooo"))
    assert seats.best_groups(free, set(), 2, AREAS) == []
    groups = seats.best_groups(free, {"1_3_2"}, 2, AREAS)
    assert groups[0] == ("preferred", ["1_2_2", "1_3_2"])
    assert {area for area, _ in groups} == {"preferred", "acceptable"}
    assert all("1_3_2" in g for _, g in groups)
