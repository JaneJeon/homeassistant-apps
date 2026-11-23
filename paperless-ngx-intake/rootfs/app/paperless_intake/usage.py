"""Google pages: how many went today and this month, counted once per answer.

"Today" and "this month" are local. The Supervisor gives every app container Home
Assistant's time zone as TZ (supervisor/docker/app.py, environment), and without one the
local time is UTC. Google's free allowance is per month, and the app's cap is per day.

A page is counted when Google's answer for it is first seen, and each answer is counted
once, by the file the Google backend saved it in. Every request Google answers gets a file
of its own, so a job that is resumed after the app was killed, and reads the same answer
again from disk, never counts it twice. A new request is a new file, and is counted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Iterable

from .store import write_atomic


def day_of(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(at))


def month_of(at: float) -> str:
    return time.strftime("%Y-%m", time.localtime(at))


def next_midnight(at: float) -> float:
    """The start of the next local day. mktime folds day 32 into the next month and minds summer time."""
    t = time.localtime(at)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))


class GoogleUsage:
    def __init__(self, path: Path, clock: Callable[[], float]):
        self.path = path
        self.clock = clock

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        if "day" not in data:
            # Written by 1.3.2 or earlier: {"2026-11-23": 12}, one UTC day. Taken as today's count, and
            # as the month's so far: the month's count starts with this release.
            days = {key: int(value) for key, value in data.items() if len(key) == 10}
            if not days:
                return {}
            day = max(days)
            return {"day": day, "today": days[day], "month": day[:7], "this_month": days[day], "counted": {}}
        return data

    def today(self) -> int:
        data = self._read()
        return int(data.get("today", 0)) if data.get("day") == day_of(self.clock()) else 0

    def this_month(self) -> int:
        data = self._read()
        return int(data.get("this_month", 0)) if data.get("month") == month_of(self.clock()) else 0

    def count(self, answers: Iterable[tuple[str, int]]) -> int:
        """Count the pages of the answers not counted before. answers: (the answer's file, its pages). Returns pages added."""
        data = self._read()
        now = self.clock()
        day, month = day_of(now), month_of(now)
        today = int(data.get("today", 0)) if data.get("day") == day else 0
        this_month = int(data.get("this_month", 0)) if data.get("month") == month else 0
        counted = dict(data.get("counted") or {})
        added = 0
        for answer, pages in answers:
            if answer in counted:
                continue
            counted[answer] = month
            added += int(pages)
        if not added:
            return 0
        # An answer is only ever read again by the job that asked for it, within days. Last month's are kept too.
        previous = month_of(time.mktime((int(month[:4]), int(month[5:]), 0, 12, 0, 0, 0, 0, -1)))
        counted = {answer: when for answer, when in counted.items() if when in {month, previous}}
        write_atomic(self.path, json.dumps({"day": day, "today": today + added, "month": month,
                                            "this_month": this_month + added, "counted": counted}).encode())
        return added
