"""Conservative Indian cash-equity trading calendar helpers."""

from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Set
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday = (32 + 2 * e + 2 * i - h - k) % 7
    month = (a + 11 * h + 22 * weekday) // 451
    return date(year, (h + weekday - 7 * month + 114) // 31, ((h + weekday - 7 * month + 114) % 31) + 1)


def reliable_holidays(year: int) -> Set[date]:
    """Holidays that are fixed or algorithmically stable across NSE and BSE."""
    easter = _easter_sunday(year)
    return {
        date(year, 1, 26),
        easter - timedelta(days=2),
        date(year, 8, 15),
        date(year, 10, 2),
        date(year, 12, 25),
    }


def is_trading_day(value: date) -> bool:
    return value.weekday() < 5 and value not in reliable_holidays(value.year)


def trading_days(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        if is_trading_day(current):
            yield current
        current += timedelta(days=1)


def last_completed_trading_day(now: Optional[datetime] = None) -> date:
    current = now.astimezone(IST) if now else datetime.now(IST)
    candidate = current.date()
    if current.hour < 18:
        candidate -= timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate
