"""
Seat groups: where a party of N can sit together, on the grid of
cinemacity.fetch_seatplan (seat keys "{section}_{x}_{y}").

A group is either N free seats next to each other in one row, or, for 4
or more people, two blocks in neighbouring rows: a front block and a back
block of at least 2 seats each, where the smaller block sits directly
behind or in front of part of the larger one (the same columns). So 4 is
2 + 2, 5 is 3 + 2, 6 is 4 + 2 or 3 + 3. Never more than two rows.
Wheelchair spaces never count towards a group.

A hall can have seating areas, each a rectangle on the grid: "preferred"
and "acceptable". With areas set, groups must lie inside them; a hall
without areas allows every seat.
"""

from collections.abc import Iterable

MAX_PARTY_SIZE = 10


def parse_key(key: str) -> tuple[str, int, int]:
    section, x, y = key.split("_")
    return section, int(x), int(y)


def is_wheelchair(seat_name: str) -> bool:
    return seat_name.upper().startswith("V")


def usable(keys: Iterable[str], plan: dict[str, list[str]]) -> set[str]:
    """The keys that are known seats and not wheelchair spaces."""
    return {k for k in keys if k in plan and not is_wheelchair(plan[k][1])}


def splits(n: int) -> list[tuple[int, int]]:
    """(larger block, smaller block) sizes a party of n may use; a smaller
    block of 0 means one row."""
    if n < 4:
        return [(n, 0)]
    return [(n, 0)] + [(a, n - a) for a in range(n - 2, (n - 1) // 2, -1)]


def _runs(xs: set[int], length: int) -> list[range]:
    """Every window of `length` consecutive columns that are all in xs."""
    return [range(x, x + length) for x in sorted(xs) if all(x + i in xs for i in range(length))]


def find_groups(free: Iterable[str], n: int) -> list[list[str]]:
    """Every way a party of n fits into the free seats, as lists of keys,
    one-row groups first."""
    rows: dict[tuple[str, int], set[int]] = {}
    for key in free:
        section, x, y = parse_key(key)
        rows.setdefault((section, y), set()).add(x)

    def keys(section: str, y: int, cols: range) -> list[str]:
        return [f"{section}_{x}_{y}" for x in cols]

    groups = []
    for (section, y), xs in sorted(rows.items()):
        groups += [keys(section, y, cols) for cols in _runs(xs, n)]
    for larger, smaller in splits(n)[1:]:
        for (section, y), xs in sorted(rows.items()):
            for other_y in (y - 1, y + 1):
                other = rows.get((section, other_y))
                if not other:
                    continue
                for big in _runs(xs, larger):
                    for small in _runs(other & set(big), smaller):
                        # 3 + 3 is found from both rows; keep the front-row one.
                        if larger == smaller and other_y < y:
                            continue
                        groups.append(keys(section, y, big) + keys(section, other_y, small))
    return groups


def _seat_sort_key(name: str) -> tuple[int, str]:
    return (int(name), "") if name.isdigit() else (10**6, name)


AREAS = ("preferred", "acceptable")


def in_rect(key: str, rect: dict | None) -> bool:
    """rect is {"rows": [first y, last y], "cols": [first x, last x]}."""
    if not rect:
        return False
    _, x, y = parse_key(key)
    return rect["rows"][0] <= y <= rect["rows"][1] and rect["cols"][0] <= x <= rect["cols"][1]


def area_of(key: str, areas: dict) -> str | None:
    """"preferred", "acceptable", or None for a seat outside every area.
    Without areas every seat is acceptable."""
    if not any(areas.get(a) for a in AREAS):
        return "acceptable"
    return next((a for a in AREAS if in_rect(key, areas.get(a))), None)


def in_areas(keys, areas: dict) -> set[str]:
    return {k for k in keys if area_of(k, areas)}


def best_groups(free: set[str], freed: set[str], n: int, areas: dict) -> list[tuple[str, list[str]]]:
    """Groups of n inside the areas that use at least one freed seat (a
    group of free seats only already existed), as (area, keys), groups
    fully inside the preferred area first."""
    found = []
    for group in find_groups(in_areas(free, areas), n):
        if freed.isdisjoint(group):
            continue
        area = "preferred" if all(area_of(k, areas) == "preferred" for k in group) else "acceptable"
        found.append((area, group))
    return sorted(found, key=lambda ag: ag[0] != "preferred")


def describe(group: Iterable[str], plan: dict[str, list[str]]) -> str:
    """Where a group sits, for a push: "row 7, seats 12-13", or
    "row 7, seats 12-13 + row 8, seats 12-13" over two rows."""
    by_row: dict[str, list[str]] = {}
    order: dict[str, int] = {}
    for key in group:
        row, seat = plan.get(key, ["?", "?"])
        by_row.setdefault(row, []).append(seat)
        order[row] = parse_key(key)[2]
    parts = []
    for row in sorted(by_row, key=order.get):
        names = sorted(by_row[row], key=_seat_sort_key)
        seats = names[0] if len(names) == 1 else f"{names[0]}-{names[-1]}"
        parts.append(f"row {row}, seat{'s' if len(names) != 1 else ''} {seats}")
    return " + ".join(parts)
