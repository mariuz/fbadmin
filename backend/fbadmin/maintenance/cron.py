"""A small 5-field cron expression parser (minute hour day-of-month month day-of-week).

Supports ``*``, lists (``1,15``), ranges (``1-5``), steps (``*/15``, ``10-50/10``), month and
weekday names (``jan``, ``mon-fri``), ``7`` for Sunday and the macros ``@hourly``, ``@daily``,
``@midnight``, ``@weekly``, ``@monthly``, ``@yearly``/``@annually``. As in Vixie cron, when both
day-of-month and day-of-week are restricted, a day matching either one fires.
"""

from __future__ import annotations

from datetime import datetime, timedelta

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}


class CronError(ValueError):
    pass


def _parse_field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    values: set[int] = set()

    def num(s: str) -> int:
        s = s.strip().lower()
        if names and s in names:
            return names[s]
        if not s.isdigit():
            raise CronError(f"Invalid value {s!r}")
        return int(s)

    for part in text.split(","):
        if not part:
            raise CronError(f"Empty list item in {text!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError(f"Invalid step {step_s!r}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = num(a), num(b)
        else:
            start = num(part)
            end = hi if step > 1 else start
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise CronError(f"Value out of range {lo}-{hi} in {text!r}")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expr: str):
        self.expr = expr.strip()
        text = MACROS.get(self.expr.lower(), self.expr)
        fields = text.split()
        if len(fields) != 5:
            raise CronError("A cron expression needs 5 fields: minute hour day-of-month month day-of-week")
        self.minutes = _parse_field(fields[0], 0, 59)
        self.hours = _parse_field(fields[1], 0, 23)
        self.days = _parse_field(fields[2], 1, 31)
        self.months = _parse_field(fields[3], 1, 12, MONTHS)
        dows = _parse_field(fields[4], 0, 7, DAYS)
        self.dows = {0 if d == 7 else d for d in dows}
        self.dom_any = fields[2] == "*"
        self.dow_any = fields[4] == "*"

    def _day_ok(self, d: datetime) -> bool:
        dow = (d.weekday() + 1) % 7  # Python: Monday=0 -> cron: Sunday=0
        dom_ok = d.day in self.days
        dow_ok = dow in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def matches(self, d: datetime) -> bool:
        return (d.minute in self.minutes and d.hour in self.hours and d.month in self.months
                and self._day_ok(d))

    def next(self, after: datetime) -> datetime:
        """The first matching minute strictly after ``after``."""
        d = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = d + timedelta(days=366 * 5)
        while d <= limit:
            if d.month not in self.months:
                d = (d.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(d):
                d = d.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if d.hour not in self.hours:
                d = d.replace(minute=0) + timedelta(hours=1)
                continue
            if d.minute not in self.minutes:
                d += timedelta(minutes=1)
                continue
            return d
        raise CronError(f"Expression {self.expr!r} never fires")


def validate(expr: str) -> str:
    Cron(expr)
    return expr.strip()
