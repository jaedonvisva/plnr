"""Waterloo term-code and course-code parsing.

LEARN's `OrgUnit.Code` for a course offering looks like `MSE232_prof_1265`:
course, instructor username, Waterloo term code. Term codes are `1` + two-digit
year + month-digit (1 = Winter, 5 = Spring, 9 = Fall), so `1265` = Spring 2026.

Adapted from agent2learn's Waterloo school adapter
(https://github.com/ManagementMO/agent2learn, Apache-2.0). Everything here is
*inferred from the code string*, not a LEARN field -- return None rather than
guess when the string doesn't match.
"""

import re

_TERM_RUN = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_COURSE_PREFIX = re.compile(r"^([A-Za-z]{2,8})(\d{1,4}[A-Za-z]?)(?:_|$)")
_SEASONS = {1: "Winter", 5: "Spring", 9: "Fall"}


def term_from_code(code: str | None) -> str | None:
    """The last valid four-digit Waterloo term code in the offering string, e.g. "1265"."""
    if not code:
        return None
    valid = [m.group(1) for m in _TERM_RUN.finditer(code) if 1000 <= int(m.group(1)) <= 1999]
    return valid[-1] if valid else None


def term_label(term: str | None) -> str | None:
    """"1265" -> "Spring 2026". None if the code isn't a known Waterloo term."""
    if not term or not re.fullmatch(r"\d{4}", term):
        return None
    value = int(term)
    if not 1000 <= value <= 1999:
        return None
    season = _SEASONS.get(value % 10)
    if season is None:
        return None
    return f"{season} {1900 + value // 10}"


def display_code(code: str | None) -> str | None:
    """"MSE232_prof_1265" -> "MSE 232". None if the string doesn't start like a course code."""
    if not code:
        return None
    match = _COURSE_PREFIX.match(code)
    if not match:
        return None
    return f"{match.group(1).upper()} {match.group(2).upper()}"
